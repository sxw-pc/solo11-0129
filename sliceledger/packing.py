"""Plan simple one-part-per-box outbound packing."""
from .ingest import capture
from .store import connect, save, decide


def plan(path, file):
    source, payload = capture(path, 'desktop_packing_request', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                if not row.get('part') or not row.get('box'):
                    status, reason = '待核', 'part_or_box_missing'
                else:
                    status, reason = '已装箱', 'one_part_per_box'
                save(db, 'packing_plan', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '制件装箱', key, status, reason, source))
        return results
    finally:
        db.close()
