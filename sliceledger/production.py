"""Desktop printer event sequence and evidence based run completion.

Run exports are kept as append-only revisions.  A later export for the same
run is captured as a new revision when it legitimately extends the earlier
sequence; the previous evidence stays in ``machine_run_revision``.  Maintenance
diagnoses recomputed against an old sequence are left in place as earlier
advice.
"""
import json
from datetime import datetime

from .ingest import capture
from .store import connect, save, decide, issue

TRANSITIONS = {
    'queued': {'running'},
    'running': {'interrupted', 'completed'},
    'interrupted': {'resumed'},
    'resumed': {'interrupted', 'completed'},
    'completed': set(),
}


def parse_events(events):
    """Validate a sequence and return (shape, final_state, error, last_time)."""
    state = None
    previous_time = None
    expected_seq = 1
    shape = []
    for event in events or []:
        try:
            at = datetime.fromisoformat(event['at'])
        except (KeyError, TypeError, ValueError):
            return None, None, 'invalid_event_time', None
        if event.get('seq') != expected_seq or (previous_time and at < previous_time):
            return None, None, 'event_order_conflict', None
        if state is not None and event.get('state') not in TRANSITIONS[state]:
            return None, None, 'invalid_transition', None
        state = event['state']
        previous_time = at
        expected_seq += 1
        shape.append({'seq': event['seq'], 'state': event['state'], 'at': event['at']})
    return shape, state, None, previous_time


def phases(sequence):
    """Running and interrupted intervals derived from a valid sequence.

    ``kind`` is one of 运行中 / 续打运行 / 停机余热.  The latter is the only
    interval in which a high temperature may be residual heat rather than an
    active running anomaly.
    """
    result = []
    for index, event in enumerate(sequence[:-1]):
        end = sequence[index + 1]
        if event['state'] in ('running', 'resumed'):
            result.append({
                'phase': len(result),
                'from': event['at'],
                'to': end['at'],
                'kind': '运行中' if event['state'] == 'running' else '续打运行',
                'interrupted': end['state'] == 'interrupted',
                'interrupt_at': end['at'] if end['state'] == 'interrupted' else None,
            })
        elif event['state'] == 'interrupted':
            result.append({
                'phase': len(result),
                'from': event['at'],
                'to': end['at'],
                'kind': '停机余热',
                'interrupted': False,
                'interrupt_at': event['at'],
            })
    return result


def phase_at(sequence, at):
    """Return the phase containing ``at`` from the latest run evidence."""
    for item in phases(sequence):
        if item['from'] <= at < item['to']:
            return item
    if sequence:
        last = sequence[-1]
        if last['state'] == 'completed' and at >= last['at']:
            return {'phase': 'post', 'from': last['at'], 'to': at,
                    'kind': '停机余热', 'interrupted': False,
                    'interrupt_at': last['at']}
    return None


def _shape_is_prefix(old, new):
    return len(new) >= len(old) and all(a == b for a, b in zip(old, new))


def _seq_state(events):
    return [{'seq': e.get('seq'), 'state': e.get('state')} for e in events]


def _is_late_extension(old_body, events):
    """Is this export a legitimate later revision of the same run?

    Legitimate revisions either append events after an interrupted run or
    correct the timestamps of an interrupted sequence (a late machine clock or
    late-arriving event log).  They are captured as new revisions; the old
    evidence and any plan computed from it remain in the ledger.  A different
    event *shape* is not a revision and is rejected as a conflict.
    """
    old_events = old_body.get('events', [])
    if not _shape_is_prefix(_seq_state(old_events), _seq_state(events)):
        return False
    _, old_state, old_error, _ = parse_events(old_events)
    _, new_state, new_error, _ = parse_events(events)
    if old_error or new_error or old_state != 'interrupted':
        return False
    return new_state in ('interrupted', 'resumed', 'completed')


def _store_revision(db, source, version, run_key, row, events, status, reason, revision_no):
    shape, state, error, _ = parse_events(events)
    valid = shape is not None and bool(events) and events[0].get('state') == 'queued'
    revision = {
        'run': run_key,
        'machine': row.get('machine'),
        'events': shape if shape is not None else [],
        'phases': phases(shape or []),
        'interrupt_count': sum(1 for p in phases(shape or []) if p['kind'] == '停机余热'),
        'valid': valid,
        'validation_error': error or (None if valid else 'missing_queue_event'),
        'revision': revision_no,
    }
    save(db, 'machine_run_revision', f'{run_key}:r{revision_no}', source, version,
         revision, '已记录' if valid else '待核', 'run_revision')
    # Event rows are versioned by revision so a legitimate late correction
    # appends new evidence instead of overwriting (or conflicting with) the
    # event as originally captured.
    for event in events:
        save(db, 'machine_event', f"{run_key}:r{revision_no}:{event['seq']}", source, version,
             event, status, reason)
    return revision if valid else None


def _update_head(db, key, source, version, row, status, reason):
    encoded = json.dumps(row, sort_keys=True, ensure_ascii=False)
    db.execute(
        'INSERT INTO machine_run(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,version=excluded.version,'
        'body=excluded.body,status=excluded.status,reason=excluded.reason',
        (key, source, version, encoded, status, reason))


def import_runs(path, file):
    source, payload = capture(path, 'desktop_machine_queue', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                job = db.execute('SELECT * FROM slice_export WHERE id=?', (str(row['job']),)).fetchone()
                reservation = db.execute('SELECT * FROM reservation WHERE id=?',
                                         (str(row['reservation']),)).fetchone()
                from .lot_quality import latest
                inspected = latest(db, json.loads(reservation['body'])['lot']) if reservation else None
                events = row.get('events', [])
                existing = db.execute('SELECT * FROM machine_run WHERE id=?', (key,)).fetchone()
                if existing and json.loads(existing['body']) == row:
                    results.append(decide(db, '机台执行', key, existing['status'], existing['reason'], source))
                    continue
                late_extension = bool(existing and _is_late_extension(json.loads(existing['body']), events))
                if existing and not late_extension:
                    # Same run identity with a non-append-only change is a
                    # conflicting re-export, not new evidence.
                    encoded = json.dumps(row, sort_keys=True, ensure_ascii=False)
                    if existing['body'] != encoded:
                        issue(db, source, 'identity_conflict', f'machine_run:{key}')
                        raise ValueError('identity_conflict')
                previous_status = existing['status'] if existing else None
                if not job or job['status'] != '可开机候选' or not reservation or reservation['status'] != '已预留':
                    status, reason = '待核', 'upstream_not_ready'
                elif inspected and inspected['status'] != '合格':
                    status, reason = '待核', 'lot_quarantined'
                elif row.get('machine') != json.loads(job['body'])['machine']:
                    status, reason = '待核', 'machine_mismatch'
                else:
                    shape, state, error, _ = parse_events(events)
                    if not events or events[0].get('state') != 'queued':
                        status, reason = '待核', 'missing_queue_event'
                    elif error:
                        status, reason = '待核', error
                    elif state != 'completed':
                        status, reason = '中断待续', 'run_not_completed'
                    else:
                        status, reason = '已完成', 'run_completed'
                if late_extension:
                    issue(db, source, 'late_machine_event', f'run:{key}')
                if previous_status == '已完成' and status != '已完成':
                    issue(db, source, 'machine_revision_regressed', f'run:{key}')
                prior = db.execute(
                    "SELECT count(*) FROM machine_run_revision WHERE json_extract(body,'$.run')=?",
                    (key,)).fetchone()[0]
                revision = _store_revision(db, source, payload['version'], key, row, events,
                                           status, reason, prior + 1)
                _update_head(db, key, source, payload['version'], row, status, reason)
                if revision is not None:
                    from .maintenance import after_run_revision
                    after_run_revision(db, source, revision)
                results.append(decide(db, '机台执行', key, status, reason, source))
        return results
    finally:
        db.close()
