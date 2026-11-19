import json
from sliceledger import packing, carrier


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_packing_to_carrier_manifest(tmp_path):
    db = tmp_path / 'ledger.db'
    request = {'version': 'v1', 'rows': [{'id': 'B1', 'part': 'P1', 'box': 'X1'}]}
    assert packing.plan(db, write(tmp_path, 'request.json', request))[0]['status'] == '已装箱'
    manifest = {'version': 'v1', 'rows': [{'id': 'M1', 'packing': 'B1', 'tracking': 'LOCAL1'}]}
    assert carrier.manifest(db, write(tmp_path, 'manifest.json', manifest))[0]['status'] == '已交运'
