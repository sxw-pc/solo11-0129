"""Capture force test traces for printed specimens."""
from .ingest import capture
from .store import connect, decide, save


def import_trace(path, file):
    source, payload = capture(path, 'force_test', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                readings = row['force_n']
                body = {**row, 'peak_force_n': max(readings)}
                save(db, 'force_trace', key, source, payload['version'], body,
                     '已采录', 'peak_recorded')
                results.append(decide(db, '载荷试验', key, '已采录',
                                      'peak_recorded', source))
        return results
    finally:
        db.close()
