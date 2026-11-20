import json

from sliceledger.store import connect

IDENTITY = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
ROT90Z = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
PRICING = {'volumetric_divisor': 5000, 'currency': 'CNY',
           'tiers': [{'up_to': 3, 'price': 10}, {'up_to': 6, 'price': 16},
                     {'up_to': 12, 'price': 24}, {'up_to': 20, 'price': 34}]}


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def part(pid, size, weight, cushion=0, fragile=None, rotations=None):
    return {'id': pid, 'size': size, 'weight': weight, 'cushion': cushion,
            'fragile_faces': fragile or [],
            'rotations': rotations if rotations is not None else [IDENTITY]}


def box(tid, inner, tare, max_weight, qty, certified=False, pack=10):
    return {'id': tid, 'inner': inner, 'tare': tare, 'max_weight': max_weight,
            'quantity': qty, 'certified': certified, 'pack_minutes': pack}


def order(oid='ORD-1', contract='multi', cutoff='2026-11-20T18:00:00',
          start='2026-11-20T15:00:00'):
    return {'id': oid, 'contract': contract, 'cutoff': cutoff, 'pack_start': start}


def request(payload_order, parts, box_types, pricing=None, **extra):
    payload = {'version': 'v1', 'order': payload_order, 'parts': parts,
               'box_types': box_types, 'pricing': pricing or PRICING}
    payload.update(extra)
    return payload


def plan_body(db, plan_id):
    conn = connect(db)
    try:
        row = conn.execute('SELECT body FROM packing_plan WHERE id=?', (plan_id,)).fetchone()
        return json.loads(row['body'])
    finally:
        conn.close()


def verify_layout(box):
    """独立复核存储的箱内坐标:边界、互不穿透、坐标与缓冲自洽。"""
    inner = box['inner']
    effs = []
    for item in box['parts']:
        mn, mx = item['eff_min'], item['eff_max']
        assert all(mn[i] >= 0 and mx[i] <= inner[i] for i in range(3))
        assert all(abs(mn[i] - (item['position'][i] - item['cushion'])) < 1e-9 for i in range(3))
        assert all(abs(mx[i] - (item['position'][i] + item['dims'][i] + item['cushion'])) < 1e-9
                   for i in range(3))
        for omn, omx in effs:
            assert not all(mn[i] < omx[i] and omn[i] < mx[i] for i in range(3))
        effs.append((mn, mx))
