"""Capture a simple offline machine door and relay check."""
from .ingest import capture
from .store import connect, save, decide


def check(path, file):
    source, payload = capture(path, 'desktop_interlock_check', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                if row.get('door_closed') and row.get('relay_closed'):
                    status, reason = '可启动', 'static_contacts_closed'
                else:
                    status, reason = '停机', 'contact_open'
                save(db, 'interlock_check', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '机台联锁', key, status, reason, source))
        return results
    finally:
        db.close()
