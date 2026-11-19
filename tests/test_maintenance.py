import json
from sliceledger import maintenance


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_alert_to_release(tmp_path):
    db = tmp_path / 'ledger.db'
    sensor = {'version': 'v1', 'rows': [{'id': 'A1', 'machine': 'M1',
              'sensor': 'temperature', 'value': 245, 'limit': 230}]}
    assert maintenance.import_sensors(db, write(tmp_path, 'sensor.json', sensor))[0]['status'] == '待检修'
    release = {'version': 'v1', 'rows': [{'id': 'R1', 'alert': 'A1', 'technician': 'tech1'}]}
    assert maintenance.release(db, write(tmp_path, 'release.json', release))[0]['status'] == '可复机'
