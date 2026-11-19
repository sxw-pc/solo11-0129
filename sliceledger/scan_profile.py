"""Capture dimensional scans of printed calibration coupons."""
from .ingest import capture
from .store import connect, decide, save


def import_scan(path, file):
    source, payload = capture(path, 'coupon_scan', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                points = row['measured_points_mm']
                bounds = [max(p[i] for p in points) - min(p[i] for p in points)
                          for i in range(3)]
                body = {**row, 'bounds_mm': bounds}
                save(db, 'coupon_scan', key, source, payload['version'], body,
                     '已测量', 'bounding_box_only')
                results.append(decide(db, '试片扫描', key, '已测量',
                                      'bounding_box_only', source))
        return results
    finally:
        db.close()
