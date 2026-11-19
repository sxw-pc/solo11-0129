import json

from sliceledger import load_test
from sliceledger.store import connect


def test_force_peak(tmp_path):
    db = tmp_path / 'ledger.db'
    file = tmp_path / 'trace.json'
    file.write_text(json.dumps({'version': 'v1', 'rows': [{
        'id': 'T1', 'force_n': [0, 100, 90], 'cycle': [0, 1, 2]
    }]}), encoding='utf-8')
    assert load_test.import_trace(db, file)[0]['status'] == '已采录'
    conn = connect(db)
    try:
        row = conn.execute("SELECT body FROM force_trace WHERE id='T1'").fetchone()
        assert json.loads(row['body'])['peak_force_n'] == 100
    finally:
        conn.close()
