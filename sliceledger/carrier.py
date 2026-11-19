"""Capture a local carrier manifest for a planned box."""
from .ingest import capture
from .store import connect, save, decide


def manifest(path, file):
    source, payload = capture(path, 'desktop_carrier_manifest', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                packing = db.execute('SELECT * FROM packing_plan WHERE id=?',
                                     (str(row['packing']),)).fetchone()
                if not packing or packing['status'] != '已装箱':
                    status, reason = '待核', 'packing_missing'
                elif not row.get('tracking'):
                    status, reason = '待核', 'tracking_missing'
                else:
                    status, reason = '已交运', 'tracking_recorded'
                save(db, 'carrier_manifest', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '承运清单', key, status, reason, source))
        return results
    finally:
        db.close()
