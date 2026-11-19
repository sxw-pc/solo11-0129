"""Machine feed evidence: cumulative extrusion, layer spans, spool swaps and roll weighings.

Rebuilds the actual per-layer material lot attribution for every part of a machine
run.  A feed export row for a run carries four independently measured evidence kinds:

* ``extrusions``     cumulative extrusion counter readings (monotonic, idle pauses do not move it);
* ``layers``         per-layer extrusion start/end positions;
* ``spool_changes``  the extrusion position (and time) at which the operator swapped rolls;
* ``weighings``      physical before/after grams of every roll that fed the run.

Any ordering, regression, double-claim, gap or missing boundary invalidates the
reconstruction (status 待核); nothing is guessed.  A spool change inside a layer splits
that layer into two segments by extrusion position, never by wall-clock time.
"""
import json
from .ingest import capture
from .store import connect, save, decide

EPS = 1e-6
# Physical scales and the machine's extrusion counter are independent instruments;
# only gross deviations are treated as evidence conflicts.
WEIGH_TOLERANCE = 0.05

USAGE_APPEARANCE = '外观打样'
USAGE_SINGLE_LOT = '单批次追溯'
USAGES = (USAGE_APPEARANCE, USAGE_SINGLE_LOT)


def latest_feed(db, run_id):
    """Newest feed evidence row for a run, trusted or not."""
    return db.execute('SELECT * FROM feed_evidence WHERE json_extract(body,\'$.run\')=? ORDER BY rowid DESC LIMIT 1',
                      (str(run_id),)).fetchone()


def trusted_feed(db, run_id):
    row = latest_feed(db, run_id)
    return row if row and row['status'] == '可信' else None


def part_segments(db, part_id):
    return [dict(x) for x in db.execute(
        "SELECT * FROM material_segment WHERE json_extract(body,'$.part')=? ORDER BY rowid", (str(part_id),))]


def part_lot_usage(db, part_id):
    return [dict(x) for x in db.execute(
        "SELECT * FROM lot_usage WHERE json_extract(body,'$.part')=? ORDER BY rowid", (str(part_id),))]


def _untrusted(db, key, source, version, row, reason):
    save(db, 'feed_evidence', key, source, version, row, '待核', reason)
    # If any roll named by this evidence is already quarantined, every listed part
    # joins the review list: the cross-roll boundary cannot be located yet.
    from .lot_quality import latest
    mentioned = {str(w.get('lot')) for w in row.get('weighings', [])}
    mentioned |= {str(c.get('lot')) for c in row.get('spool_changes', [])}
    for lot in mentioned:
        inspection = latest(db, lot)
        if inspection and inspection['status'] == '隔离':
            insp_body = json.loads(inspection['body'])
            for part_id in row.get('parts', []):
                from .lot_quality import review_part
                review_part(db, insp_body.get('id', inspection['id']), lot, part_id, source, version)
    return {'subject': key, 'status': '待核', 'reason': reason}


def _validate(row, run, run_body, reservation_body, job_body):
    """Return (segments_by_part, per_lot_grams, reason). reason None means evidence is trusted."""
    machine = run_body.get('machine')
    if row.get('machine') != machine:
        return None, None, 'feed_machine_mismatch'
    if row.get('job') != run_body.get('job') or str(row.get('reservation')) != str(run_body.get('reservation')):
        return None, None, 'feed_identity_mismatch'
    if row.get('version') is not None and str(row.get('version')) != str(run_body.get('version', '')) and \
            str(row.get('version')) != str(job_body.get('version', '')):
        return None, None, 'feed_version_mismatch'
    if run['status'] != '已完成':
        return None, None, 'run_not_completed'

    extrusions = [float(x) for x in row.get('extrusions', [])]
    layers = row.get('layers', [])
    changes = row.get('spool_changes', [])
    weighings = {str(w['lot']): w for w in row.get('weighings', [])}

    if not extrusions or len(extrusions) < 2:
        return None, None, 'extrusion_reading_missing'
    if any(b < a - EPS for a, b in zip(extrusions, extrusions[1:])):
        return None, None, 'extrusion_regression'
    if not layers:
        return None, None, 'layer_span_missing'

    # Layers must be consecutive, non-empty and contiguous over the extrusion range.
    cursor = extrusions[0]
    norm_layers = []
    expected_layer = 1
    total_layer = 0.0
    for layer in layers:
        start = float(layer['start']); end = float(layer['end'])
        if int(layer['layer']) != expected_layer or end < start - EPS:
            return None, None, 'layer_span_conflict'
        if abs(start - cursor) > EPS or end > extrusions[-1] + EPS or start < extrusions[0] - EPS:
            return None, None, 'extrusion_gap'
        norm_layers.append((int(layer['layer']), start, end))
        cursor = end
        total_layer += max(0.0, end - start)
        expected_layer += 1
    if abs(cursor - extrusions[-1]) > EPS:
        return None, None, 'extrusion_gap'

    # Spool changes: every boundary needs an extrusion position and a time; positions
    # must be strictly ordered and fall inside the printed range. Two boundaries inside
    # the same layer would let two rolls claim one extrusion interval.
    norm_changes = []
    last_pos = extrusions[0]
    for change in changes:
        if change.get('position') is None:
            return None, None, 'spool_change_position_missing'
        if not change.get('at'):
            return None, None, 'spool_change_time_missing'
        pos = float(change['position'])
        if pos <= last_pos + EPS or pos >= extrusions[-1] - EPS:
            return None, None, 'spool_change_boundary_conflict'
        norm_changes.append((pos, str(change['lot']), change['at']))
        last_pos = pos
    for (p1, _, _), (p2, _, _) in zip(norm_changes, norm_changes[1:]):
        inside_same_layer = any(s < p1 - EPS and e > p2 + EPS for _, s, e in norm_layers)
        if inside_same_layer:
            return None, None, 'spool_change_double_claim'

    # Build the lot chain: the roll on the extruder at first extrusion must be the
    # reserved lot; each change hands over at its measured position.
    reserved_lot = str(reservation_body['lot'])
    chain = [(extrusions[0], reserved_lot, None)]
    for pos, lot, at in norm_changes:
        if lot == chain[-1][1]:
            return None, None, 'spool_change_same_lot'
        chain.append((pos, lot, at))
    active_lots = []
    for _, lot, _ in chain:
        if lot not in active_lots:
            active_lots.append(lot)

    # Every active roll needs a weighing record; material must match the job.
    material = job_body.get('material')
    for lot in active_lots:
        w = weighings.get(lot)
        if not w:
            return None, None, 'roll_weighing_missing'
        if material is not None and w.get('material') != material:
            return None, None, 'roll_material_mismatch'

    # Split layers at every change position. Boundaries at layer edges simply close
    # the layer; a boundary strictly inside a layer splits it by position.
    parts = [str(p) for p in row.get('parts', [])] or [str(reservation_body.get('job', ''))]
    segments = {p: [] for p in parts}
    per_lot = {lot: 0.0 for lot in active_lots}
    for layer_no, start, end in norm_layers:
        cuts = [start] + [pos for pos, _, _ in norm_changes if start + EPS < pos < end - EPS] + [end]
        for seg_start, seg_end in zip(cuts, cuts[1:]):
            if seg_end <= seg_start + EPS:
                continue
            lot, changed_at = reserved_lot, None
            for pos, lot_id, at in norm_changes:
                if pos <= seg_start + EPS:
                    lot, changed_at = lot_id, at
                else:
                    break
            grams = round(seg_end - seg_start, 9)
            per_lot[lot] += grams
            for p in parts:
                segments[p].append({'part': p, 'layer': layer_no, 'start': seg_start, 'end': seg_end,
                                    'grams': grams, 'lot': lot, 'changed_at': changed_at,
                                    'split': seg_start > start + EPS or seg_end < end - EPS})

    # Each roll's extrusion-derived consumption must agree with its physical weighing.
    for lot in active_lots:
        w = weighings[lot]
        weighed = float(w['start_grams']) - float(w['end_grams'])
        if weighed < -EPS or abs(weighed - per_lot[lot]) > WEIGH_TOLERANCE:
            return None, None, 'roll_weighing_conflict'

    # Layer deltas must reconstruct the full extrusion range (idle extrusion excluded).
    if abs(sum(per_lot.values()) - (extrusions[-1] - extrusions[0])) > EPS:
        return None, None, 'extrusion_gap'
    return segments, per_lot, None


def import_feed(path, file):
    source, payload = capture(path, 'desktop_machine_feed', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                run = db.execute('SELECT * FROM machine_run WHERE id=?', (str(row.get('run')),)).fetchone()
                if not run:
                    results.append(_untrusted(db, key, source, payload['version'], row, 'run_missing'))
                    continue
                run_body = json.loads(run['body'])
                reservation = db.execute('SELECT * FROM reservation WHERE id=?',
                                         (str(run_body.get('reservation')),)).fetchone()
                job = db.execute('SELECT * FROM slice_export WHERE id=?', (str(run_body.get('job')),)).fetchone()
                if not reservation or not job:
                    results.append(_untrusted(db, key, source, payload['version'], row, 'upstream_not_ready'))
                    continue
                segments, per_lot, reason = _validate(
                    row, run, run_body, json.loads(reservation['body']), json.loads(job['body']))
                if reason:
                    results.append(_untrusted(db, key, source, payload['version'], row, reason))
                    continue

                previous = latest_feed(db, str(row['run']))
                save(db, 'feed_evidence', key, source, payload['version'], row, '可信', 'feed_attribution_trusted')
                if previous:
                    db.execute("UPDATE feed_evidence SET reason=? WHERE id=?",
                               (f'superseded_by:{key}', previous['id']))

                for part_id, part_segs in segments.items():
                    for idx, seg in enumerate(part_segs, 1):
                        save(db, 'material_segment', f'{key}:{part_id}:{idx}', source, payload['version'],
                             {**seg, 'feed': key, 'run': str(row['run'])}, '可信', 'layer_lot_segment')
                    total = round(sum(s['grams'] for s in part_segs), 9)
                    lots = sorted({s['lot'] for s in part_segs})
                    save(db, 'lot_usage', f'{key}:{part_id}', source, payload['version'],
                         {'part': part_id, 'feed': key, 'run': str(row['run']),
                          'lots': lots, 'total_grams': total,
                          'usage': [{'lot': lot, 'grams': round(
                              round(sum(s['grams'] for s in part_segs if s['lot'] == lot), 9), 9)}
                                    for lot in lots]}, '可信', 'lot_consumption_attributed')

                # Accepted parts whose movements were settled against an earlier feed
                # are resettled to the new attribution; balances stay idempotent.
                from .inventory import settle_attribution
                from .lot_quality import reapply_after_feed
                for part_id in segments:
                    part = db.execute('SELECT * FROM print_part WHERE id=?', (part_id,)).fetchone()
                    if part and part['status'] in ('已验收', '撤回'):
                        settle_attribution(db, part_id, key, source, payload['version'])
                    reconcile_review(db, part_id, source, payload['version'])
                    reapply_after_feed(db, part_id, source, payload['version'])

                results.append(decide(db, '进料归属', key, '可信', 'feed_attribution_trusted', source))
        return results
    finally:
        db.close()


def reconcile_review(db, part_id, source, version):
    """A trusted attribution resolves any earlier uncertainty list entry for the part."""
    rows = db.execute("SELECT * FROM part_review WHERE json_extract(body,'$.part')=? AND status='待复核'",
                      (str(part_id),)).fetchall()
    for row in rows:
        db.execute("UPDATE part_review SET status='已澄清',reason='attribution_confirmed' WHERE id=?",
                   (row['id'],))
