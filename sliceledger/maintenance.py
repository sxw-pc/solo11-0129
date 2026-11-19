"""Capture offline printer sensor alerts and maintenance release."""
from .ingest import capture
from .store import connect, save, decide


def import_sensors(path, file):
    source, payload = capture(path, 'desktop_machine_sensors', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                if float(row['value']) > float(row['limit']):
                    status, reason = '待检修', 'sensor_limit_exceeded'
                else:
                    status, reason = '正常', 'sensor_in_range'
                save(db, 'sensor_alert', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '机台传感器', key, status, reason, source))
        return results
    finally:
        db.close()


def release(path, file):
    source, payload = capture(path, 'desktop_maintenance_release', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                alert = db.execute('SELECT * FROM sensor_alert WHERE id=?',
                                   (str(row['alert']),)).fetchone()
                if not alert:
                    status, reason = '待核', 'alert_missing'
                elif alert['status'] == '待检修' and not row.get('technician'):
                    status, reason = '待核', 'technician_missing'
                else:
                    status, reason = '可复机', 'maintenance_reviewed'
                save(db, 'maintenance_release', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '机台复机', key, status, reason, source))
        return results
    finally:
        db.close()
