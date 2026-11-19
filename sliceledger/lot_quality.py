"""Independent inspection of desktop material batch measurements."""
import json
from .ingest import capture
from .store import connect, save, decide


def latest(db, lot):
    return db.execute("SELECT * FROM lot_inspection WHERE json_extract(body,'$.lot')=? ORDER BY rowid DESC LIMIT 1",
                      (str(lot),)).fetchone()


def latest_usage(db, part_id):
    row = db.execute("SELECT * FROM lot_usage WHERE json_extract(body,'$.part')=? ORDER BY rowid DESC LIMIT 1",
                     (str(part_id),)).fetchone()
    if not row or row['status'] != '可信':
        return None
    return json.loads(row['body'])


def _untrusted_feed_mentions_lot(db, run_id, lot):
    row = db.execute("SELECT * FROM feed_evidence WHERE json_extract(body,'$.run')=? ORDER BY rowid DESC LIMIT 1",
                     (str(run_id),)).fetchone()
    if not row or row['status'] == '可信':
        return False
    body = json.loads(row['body'])
    if any(str(w.get('lot')) == str(lot) for w in body.get('weighings', [])):
        return True
    return any(str(c.get('lot')) == str(lot) for c in body.get('spool_changes', []))


def parts_exposed_to_lot(db, lot):
    """Parts whose evidence chain could contain the lot: reservation, trusted
    attribution, an as-yet-untrusted feed export that names the lot, or a part id
    listed in such an export even before its completion record arrives."""
    ids = []

    def add(pid):
        pid = str(pid)
        if pid not in ids:
            ids.append(pid)

    for part in db.execute('SELECT * FROM print_part ORDER BY id'):
        body = json.loads(part['body'])
        reserve = db.execute('SELECT body FROM reservation WHERE id=?',
                             (str(body.get('reservation')),)).fetchone()
        reserved_lot = str(json.loads(reserve['body'])['lot']) if reserve else None
        usage = latest_usage(db, part['id'])
        usage_lots = set(usage['lots']) if usage else set()
        if str(lot) == reserved_lot or str(lot) in usage_lots or \
                _untrusted_feed_mentions_lot(db, body.get('run_id', ''), lot):
            add(part['id'])
    for feed_row in db.execute("SELECT * FROM feed_evidence WHERE status!='可信' ORDER BY id"):
        body = json.loads(feed_row['body'])
        if _untrusted_feed_mentions_lot(db, body.get('run'), lot):
            for pid in body.get('parts', []):
                add(pid)
    return ids


def recall_part(db, inspection_id, lot, part_id, source, version):
    """Definite recall: trusted attribution proves the part contains the lot."""
    db.execute("UPDATE print_part SET status='撤回',reason='lot_quarantined' WHERE id=? AND status='已验收'",
               (str(part_id),))
    save(db, 'part_recall', f'{inspection_id}:{part_id}', source, version,
         {'inspection': inspection_id, 'lot': str(lot), 'part': str(part_id)}, '待通知', 'accepted_part_recalled')


def review_part(db, inspection_id, lot, part_id, source, version):
    """Possible exposure only: the cross-roll boundary is not yet locatable."""
    save(db, 'part_review', f'{inspection_id}:{part_id}', source, version,
         {'inspection': inspection_id, 'lot': str(lot), 'part': str(part_id)},
         '待复核', 'attribution_boundary_unconfirmed')


def reconcile_quarantine(db, lot, inspection_id, source, version):
    """Apply a lot isolation against actual feed attribution.

    Trusted attribution containing the lot -> the accepted part is recalled for real.
    Trusted attribution excluding the lot -> the part stays valid and any review flag
    is lifted.  No locatable boundary -> review list only, never a definite recall.
    """
    for part_id in parts_exposed_to_lot(db, lot):
        usage = latest_usage(db, part_id)
        if not usage:
            review_part(db, inspection_id, lot, part_id, source, version)
            continue
        if str(lot) in set(usage['lots']):
            part = db.execute('SELECT status FROM print_part WHERE id=?', (part_id,)).fetchone()
            if part and part['status'] == '已验收':
                recall_part(db, inspection_id, lot, part_id, source, version)
        else:
            for row in db.execute("SELECT * FROM part_review WHERE json_extract(body,'$.part')=? AND status='待复核'",
                                  (part_id,)).fetchall():
                db.execute("UPDATE part_review SET status='已澄清',reason='attribution_confirmed' WHERE id=?",
                           (row['id'],))


def reapply_after_feed(db, part_id, source, version):
    """Late trusted feed: re-evaluate every lot quarantine against the new boundary."""
    usage = latest_usage(db, part_id)
    if not usage:
        return
    for lot in usage['lots']:
        inspection = latest(db, lot)
        if inspection and inspection['status'] == '隔离':
            part = db.execute('SELECT status FROM print_part WHERE id=?', (str(part_id),)).fetchone()
            if not part:
                continue
            insp_body = json.loads(inspection['body'])
            if part['status'] == '已验收':
                recall_part(db, insp_body.get('id', inspection['id']), lot, part_id, source, version)
            elif part['status'] == '拒收':
                db.execute("UPDATE print_part SET status='撤回',reason='lot_quarantined_confirmed' WHERE id=?",
                           (str(part_id),))
                save(db, 'part_recall', f"{insp_body.get('id', inspection['id'])}:{part_id}", source, version,
                     {'inspection': insp_body.get('id', inspection['id']), 'lot': str(lot),
                      'part': str(part_id)}, '待通知', 'rejected_part_exposure_confirmed')


def assess(path, file):
    source, payload = capture(path, 'desktop_lot_inspection', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                lot = str(row['lot'])
                old = db.execute('SELECT * FROM lot_inspection WHERE id=?', (key,)).fetchone()
                if old:
                    if old['body'] != json.dumps(row, sort_keys=True, ensure_ascii=False):
                        raise ValueError('inspection_identity_conflict')
                    results.append(decide(db, '材料批次质检', key, old['status'], old['reason'], source))
                    continue
                previous = latest(db, lot)
                stock = db.execute('SELECT 1 FROM material_lot WHERE id=?', (lot,)).fetchone()
                diameter = row.get('diameter_samples', [])
                if not stock: status, reason = '待核', 'lot_missing'
                elif not diameter: status, reason = '待核', 'diameter_samples_missing'
                elif max(abs(float(x) - float(row['nominal_diameter'])) for x in diameter) > float(row['diameter_tolerance']):
                    status, reason = '隔离', 'diameter_out_of_tolerance'
                elif float(row['moisture']) > float(row['max_moisture']):
                    status, reason = '隔离', 'moisture_exceeded'
                else: status, reason = '合格', 'lot_qualified'
                if previous and previous['status'] == '隔离' and status == '合格' and not row.get('release_authority'):
                    status, reason = '隔离', 'release_authority_missing'
                save(db, 'lot_inspection', key, source, payload['version'], row, status, reason)
                if status == '隔离':
                    reconcile_quarantine(db, lot, key, source, payload['version'])
                results.append(decide(db, '材料批次质检', key, status, reason, source))
        return results
    finally:
        db.close()
