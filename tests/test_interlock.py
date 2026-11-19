import json
from sliceledger import interlock, machine_start


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_static_interlock_start(tmp_path):
    db = tmp_path / 'ledger.db'
    check = {'version': 'v1', 'rows': [{'id': 'C1', 'machine': 'M1',
             'door_closed': True, 'relay_closed': True}]}
    assert interlock.check(db, write(tmp_path, 'check.json', check))[0]['status'] == '可启动'
    start = {'version': 'v1', 'rows': [{'id': 'S1', 'machine': 'M1', 'check': 'C1'}]}
    assert machine_start.authorize(db, write(tmp_path, 'start.json', start))[0]['status'] == '已授权'
