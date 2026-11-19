"""Authorize a simulated machine start from a captured interlock check."""
from .ingest import capture
from .store import connect, save, decide


def authorize(path, file):
    source, payload = capture(path, 'desktop_machine_start', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                check = db.execute('SELECT * FROM interlock_check WHERE id=?',
                                   (str(row['check']),)).fetchone()
                if not check or check['status'] != '可启动':
                    status, reason = '拒绝', 'interlock_not_ready'
                else:
                    status, reason = '已授权', 'static_check_passed'
                save(db, 'start_authorization', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '机台启动', key, status, reason, source))
        return results
    finally:
        db.close()
