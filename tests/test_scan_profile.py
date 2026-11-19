import json

from sliceledger import scan_profile
from sliceledger.store import connect


def test_scan_bounds(tmp_path):
    db = tmp_path / 'ledger.db'
    p = tmp_path / 'scan.json'
    p.write_text(json.dumps({'version': 'v1', 'rows': [{
        'id': 'C1', 'measured_points_mm': [[0, 0, 0], [9.8, 10.1, 9.9]]
    }]}), encoding='utf-8')
    assert scan_profile.import_scan(db, p)[0]['status'] == '已测量'
    conn = connect(db)
    try:
        row = conn.execute("SELECT body FROM coupon_scan WHERE id='C1'").fetchone()
        assert json.loads(row['body'])['bounds_mm'] == [9.8, 10.1, 9.9]
    finally:
        conn.close()
