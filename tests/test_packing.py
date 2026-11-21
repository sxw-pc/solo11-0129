import json
from pathlib import Path
import pytest
from sliceledger import packing, carrier, report
from sliceledger.store import connect


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def part(pid, size, weight=1.0, cushion=0, fragile=(), rotations=None):
    return {'id': pid, 'size': size, 'weight': weight, 'cushion': cushion,
            'fragile_faces': list(fragile),
            'rotations': rotations or [{'name': 'flat', 'axes': [0, 1, 2], 'up': '+z'}]}


def box(btype, inner, max_weight, count, certified=False, tare=0.5, handling=30):
    return {'type': btype, 'inner': inner, 'max_weight': max_weight, 'count': count,
            'certified': certified, 'tare': tare, 'handling_minutes': handling}


def carrier_rate(tiers, divisor=5000, tolerance=0.2):
    return {'dim_divisor': divisor, 'weight_tolerance': tolerance,
            'tiers': [{'up_to': u, 'price': p} for u, p in tiers]}


def order(oid='ORD1', contract='multi', parts=None, boxes=None, rate=None,
          pack_start='2026-11-21T15:00:00', cutoff='2026-11-21T18:00:00'):
    return {'id': oid, 'contract': contract, 'pack_start': pack_start, 'cutoff': cutoff,
            'parts': parts or [], 'boxes': boxes or [],
            'carrier': rate or carrier_rate([(2, 8), (5, 12), (10, 30), (20, 45)])}


# 两件长形易损件：各能放入小箱，同箱只能叠放而叠放压坏易损面；大箱无库存。
def long_fragile_parts():
    rotations = [{'name': 'flat', 'axes': [0, 1, 2], 'up': '+z'},
                 {'name': 'flat-90', 'axes': [1, 0, 2], 'up': '+z'}]
    return [part('P1', [280, 110, 40], weight=1.5, cushion=5,
                 fragile=['+z'], rotations=rotations),
            part('P2', [280, 110, 40], weight=1.5, cushion=5,
                 fragile=['+z'], rotations=rotations)]


def long_part_boxes(large_count=0):
    return [box('S', [300, 200, 120], 8, 4, tare=0.4, handling=20),
            box('L', [450, 180, 300], 30, large_count, certified=True, tare=1.2, handling=40)]


def layouts(db, plan_id):
    conn = connect(db)
    rows = conn.execute('SELECT * FROM packing_layout ORDER BY id').fetchall()
    conn.close()
    return {r['id']: json.loads(r['body']) for r in rows if r['id'].startswith(plan_id + ':')}


def box_layouts(db, plan_id):
    return {k: v for k, v in layouts(db, plan_id).items() if v.get('kind') == 'box'}


def test_multi_contract_two_fragile_parts_get_two_small_boxes(tmp_path):
    db = tmp_path / 'ledger.db'
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=long_fragile_parts(), boxes=long_part_boxes())]})
    result = packing.plan(db, req)
    assert result[0]['status'] == '已装箱' and result[0]['reason'] == 'layout_selected'
    boxes = box_layouts(db, 'ORD1')
    assert sorted(boxes) == ['ORD1:box:S#1', 'ORD1:box:S#2']
    for body in boxes.values():
        assert body['box_type'] == 'S' and len(body['parts']) == 1
        placed = body['parts'][0]
        assert placed['orientation'] == 'flat' and placed['position'] == [0, 0, 0]
        assert placed['occupied'] == [290, 120, 50]
        assert body['gross_weight'] == 1.9 and body['weight_range'] == [1.7, 2.1]
        assert body['chargeable_weight'] == 1.9 and body['tier_up_to'] == 2
    assert layouts(db, 'ORD1')['ORD1:summary']['total_price'] == 16


def test_single_contract_same_geometry_waits_for_coordination(tmp_path):
    db = tmp_path / 'ledger.db'
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(contract='single', parts=long_fragile_parts(), boxes=long_part_boxes())]})
    result = packing.plan(db, req)
    assert (result[0]['status'], result[0]['reason']) == ('待协调', 'certified_box_unavailable')
    diagnosis = layouts(db, 'ORD1')['ORD1:diagnosis']
    assert diagnosis['causes'][0]['certified_types'] == ['L']
    assert diagnosis['causes'][0]['available_types'] == ['S']


def test_single_contract_diagnosis_names_minimal_fragile_combination(tmp_path):
    db = tmp_path / 'ledger.db'
    # 有认证大箱但宽度不够两件平放，叠放又压坏易损面：最小不可装组合是 P1+P2。
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(contract='single', parts=long_fragile_parts(),
              boxes=long_part_boxes(large_count=1))]})
    result = packing.plan(db, req)
    assert (result[0]['status'], result[0]['reason']) == ('待协调', 'layout_infeasible')
    causes = layouts(db, 'ORD1')['ORD1:diagnosis']['causes']
    assert causes[0]['parts'] == ['P1', 'P2']
    assert causes[0]['boxes'] == {'L': 'fragile_face'}


def test_explicit_rotation_is_used_when_identity_does_not_fit(tmp_path):
    db = tmp_path / 'ledger.db'
    rotations = [{'name': 'upright', 'axes': [0, 1, 2], 'up': '+z'},
                 {'name': 'side', 'axes': [1, 0, 2], 'up': '+z'}]
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=[part('R1', [180, 90, 40], rotations=rotations)],
              boxes=[box('S', [100, 200, 60], 5, 1)])]})
    assert packing.plan(db, req)[0]['status'] == '已装箱'
    placed = box_layouts(db, 'ORD1')['ORD1:box:S#1']['parts'][0]
    assert placed['orientation'] == 'side' and placed['occupied'] == [90, 180, 40]


def test_orientation_restriction_is_diagnosed(tmp_path):
    db = tmp_path / 'ledger.db'
    # 只有旋转 90° 才能放入，但允许旋转清单未列出该方向。
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=[part('R1', [180, 90, 40])],
              boxes=[box('S', [100, 200, 60], 5, 1)])]})
    result = packing.plan(db, req)
    assert (result[0]['status'], result[0]['reason']) == ('待协调', 'part_exceeds_boxes')
    causes = layouts(db, 'ORD1')['ORD1:diagnosis']['causes']
    assert causes[0]['parts'] == ['R1'] and causes[0]['cause'] == 'orientation_restriction'


def test_boundary_tangency_is_accepted(tmp_path):
    db = tmp_path / 'ledger.db'
    # 两件 100mm 立方体贴合在 200mm 箱内，边缘刚好相切是确定可行。
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=[part('A', [100, 100, 100]), part('B', [100, 100, 100])],
              boxes=[box('T', [200, 100, 100], 10, 1)])]})
    assert packing.plan(db, req)[0]['status'] == '已装箱'
    placed = box_layouts(db, 'ORD1')['ORD1:box:T#1']['parts']
    assert {p['part']: p['position'] for p in placed} == {'A': [0, 0, 0], 'B': [100, 0, 0]}


def test_cushion_makes_tangent_parts_incompatible(tmp_path):
    db = tmp_path / 'ledger.db'
    # 缓冲垫各 5mm：两件 110mm 包络超过 200mm 内长，缓冲垫互相穿透即不可同箱。
    parts = [part('A', [100, 100, 100], cushion=5), part('B', [100, 100, 100], cushion=5)]
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=parts, boxes=[box('T', [200, 110, 110], 10, 2)])]})
    assert packing.plan(db, req)[0]['status'] == '已装箱'
    assert sorted(box_layouts(db, 'ORD1')) == ['ORD1:box:T#1', 'ORD1:box:T#2']
    db2 = tmp_path / 'ledger2.db'
    req2 = write(tmp_path, 'req2.json', {'version': 'v1', 'rows': [
        order(parts=parts, boxes=[box('T', [200, 110, 110], 10, 1)])]})
    result = packing.plan(db2, req2)
    assert (result[0]['status'], result[0]['reason']) == ('待协调', 'box_inventory_insufficient')


def test_fragile_face_never_bears_load(tmp_path):
    db = tmp_path / 'ledger.db'
    # 只能上下叠放的箱型：重件 B 在下、易损件 A 在上才合法。
    parts = [part('A', [100, 100, 50], fragile=['+z']),
             part('B', [100, 100, 50], weight=3.0)]
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=parts, boxes=[box('T', [100, 100, 200], 10, 1)])]})
    assert packing.plan(db, req)[0]['status'] == '已装箱'
    placed = box_layouts(db, 'ORD1')['ORD1:box:T#1']['parts']
    assert {p['part']: p['position'][2] for p in placed} == {'A': 50, 'B': 0}
    # 两件都标易损顶面则任何叠放都压坏易损面：只能分箱。
    db2 = tmp_path / 'ledger2.db'
    both_fragile = [part('A', [100, 100, 50], fragile=['+z']),
                    part('B', [100, 100, 50], weight=3.0, fragile=['+z'])]
    req2 = write(tmp_path, 'req2.json', {'version': 'v1', 'rows': [
        order(parts=both_fragile, boxes=[box('T', [100, 100, 200], 10, 2)])]})
    assert packing.plan(db2, req2)[0]['status'] == '已装箱'
    assert sorted(box_layouts(db2, 'ORD1')) == ['ORD1:box:T#1', 'ORD1:box:T#2']


def cost_parts():
    return [part(f'P{i}', [120, 90, 60], weight=2.0, cushion=5) for i in range(1, 5)]


def cost_boxes():
    return [box('S', [280, 110, 80], 10, 4, tare=0.3),
            box('L', [280, 220, 160], 30, 1, tare=1.2)]


def test_two_small_boxes_beat_big_box_crossing_price_tier(tmp_path):
    db = tmp_path / 'ledger.db'
    # 两只小箱各 4.3kg 落入 5kg 价阶(12)，一只大箱 9kg 跨入 10kg 价阶(30)。
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=cost_parts(), boxes=cost_boxes(),
              rate=carrier_rate([(5, 12), (10, 30), (20, 45)]))]})
    assert packing.plan(db, req)[0]['status'] == '已装箱'
    boxes = box_layouts(db, 'ORD1')
    assert sorted(boxes) == ['ORD1:box:S#1', 'ORD1:box:S#2']
    assert all(b['tier_up_to'] == 5 and b['price'] == 12 for b in boxes.values())
    assert layouts(db, 'ORD1')['ORD1:summary']['total_price'] == 24


def test_single_big_box_wins_when_splitting_is_pricier(tmp_path):
    db = tmp_path / 'ledger.db'
    # 同一几何不同价阶：大箱 20 低于两只小箱 24。
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=cost_parts(), boxes=cost_boxes(),
              rate=carrier_rate([(5, 12), (10, 20), (20, 45)]))]})
    assert packing.plan(db, req)[0]['status'] == '已装箱'
    boxes = box_layouts(db, 'ORD1')
    assert list(boxes) == ['ORD1:box:L#1']
    assert layouts(db, 'ORD1')['ORD1:summary']['total_price'] == 20


def test_cutoff_overrides_cheapest_and_can_block_everything(tmp_path):
    db = tmp_path / 'ledger.db'
    # 两箱方案 60 分钟赶不上 15:45 截单，一只大箱 30 分钟可以。
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=cost_parts(), boxes=cost_boxes(), cutoff='2026-11-21T15:45:00',
              rate=carrier_rate([(5, 12), (10, 30), (20, 45)]))]})
    assert packing.plan(db, req)[0]['status'] == '已装箱'
    assert list(box_layouts(db, 'ORD1')) == ['ORD1:box:L#1']
    db2 = tmp_path / 'ledger2.db'
    req2 = write(tmp_path, 'req2.json', {'version': 'v1', 'rows': [
        order(parts=cost_parts(), boxes=cost_boxes(), cutoff='2026-11-21T15:20:00',
              rate=carrier_rate([(5, 12), (10, 30), (20, 45)]))]})
    result = packing.plan(db2, req2)
    assert (result[0]['status'], result[0]['reason']) == ('待协调', 'cutoff_infeasible')


def test_weight_capacity_is_diagnosed(tmp_path):
    db = tmp_path / 'ledger.db'
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=[part('W1', [50, 50, 50], weight=9.0)],
              boxes=[box('S', [300, 200, 120], 8, 2)])]})
    result = packing.plan(db, req)
    assert (result[0]['status'], result[0]['reason']) == ('待协调', 'part_exceeds_boxes')
    causes = layouts(db, 'ORD1')['ORD1:diagnosis']['causes']
    assert causes[0]['cause'] == 'weight_capacity'


def test_single_contract_packs_whole_set_in_one_certified_box(tmp_path):
    db = tmp_path / 'ledger.db'
    parts = [part('A', [100, 100, 100]), part('B', [100, 100, 100])]
    boxes = [box('S', [200, 100, 100], 10, 2),
             box('L', [300, 200, 150], 30, 1, certified=True, tare=1.0)]
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(contract='single', parts=parts, boxes=boxes)]})
    assert packing.plan(db, req)[0]['status'] == '已装箱'
    boxes_out = box_layouts(db, 'ORD1')
    assert list(boxes_out) == ['ORD1:box:L#1']  # 单包裹合同不得拆箱
    assert len(boxes_out['ORD1:box:L#1']['parts']) == 2


def test_volumetric_weight_dominates_light_parcel(tmp_path):
    db = tmp_path / 'ledger.db'
    # 实重 0.5kg 低于体积重 1.44kg：按体积重落入 2kg 价阶。
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=[part('P1', [280, 110, 40], weight=0.1, cushion=5)],
              boxes=[box('S', [300, 200, 120], 8, 2, tare=0.4)])]})
    assert packing.plan(db, req)[0]['status'] == '已装箱'
    body = box_layouts(db, 'ORD1')['ORD1:box:S#1']
    assert body['gross_weight'] == 0.5 and body['volumetric_weight'] == 1.44
    assert body['chargeable_weight'] == 1.44 and body['tier_up_to'] == 2


def test_spec_errors_and_legacy_rows_are_flagged(tmp_path):
    db = tmp_path / 'ledger.db'
    no_rotation = part('P', [10, 10, 10])
    no_rotation['rotations'] = []
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        {'id': 'B1', 'part': 'P1', 'box': 'X1'},
        order('C1', contract='custody', parts=[part('P', [10, 10, 10])],
              boxes=[box('S', [50, 50, 50], 5, 1)]),
        order('C2', parts=[no_rotation], boxes=[box('S', [50, 50, 50], 5, 1)])]})
    result = packing.plan(db, req)
    assert [r['reason'] for r in result] == [
        'parts_or_boxes_missing', 'contract_unknown', 'rotations_missing']
    assert all(r['status'] == '待核' for r in result)


def plan_long_parts(db, tmp_path):
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=long_fragile_parts(), boxes=long_part_boxes())]})
    assert packing.plan(db, req)[0]['status'] == '已装箱'


def manifest_rows(*boxes, weighed=1.9, scanned='2026-11-21T16:00:00'):
    return [{'id': f'M{i}', 'packing': 'ORD1', 'box_no': box_no,
             'tracking': f'TRK-{box_no}', 'seal': f'SEAL-{box_no}',
             'weighed': weighed, 'scanned_at': scanned}
            for i, box_no in enumerate(boxes, 1)]


def test_manifest_ships_each_box_independently(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_long_parts(db, tmp_path)
    m = write(tmp_path, 'm.json', {'version': 'v1', 'rows': manifest_rows('S#1', 'S#2')})
    result = carrier.manifest(db, m)
    assert [r['status'] for r in result] == ['已交运', '已交运']
    data = json.loads(report.build(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    entry = data['packing'][0]
    assert entry['shipped'] is True
    assert {b['box_no']: b['shipment']['status'] for b in entry['boxes']} == {
        'S#1': '已交运', 'S#2': '已交运'}
    assert all(b['shipment']['seal'] and b['shipment']['tracking'] for b in entry['boxes'])


def test_partial_scan_never_marks_whole_order_shipped(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_long_parts(db, tmp_path)
    m = write(tmp_path, 'm.json', {'version': 'v1', 'rows': manifest_rows('S#1')})
    assert carrier.manifest(db, m)[0]['status'] == '已交运'
    data = json.loads(report.build(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    entry = data['packing'][0]
    assert entry['shipped'] is False
    assert {b['box_no']: b['shipment']['status'] for b in entry['boxes']} == {
        'S#1': '已交运', 'S#2': '待交运'}


def test_one_receipt_cannot_cover_two_boxes(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_long_parts(db, tmp_path)
    # 一张回执只带 tracking、不指明箱号：不能交运任何箱。
    m = write(tmp_path, 'm.json', {'version': 'v1', 'rows': [
        {'id': 'M1', 'packing': 'ORD1', 'tracking': 'TRK-X', 'seal': 'SEAL-X',
         'weighed': 1.9, 'scanned_at': '2026-11-21T16:00:00'}]})
    assert carrier.manifest(db, m)[0]['reason'] == 'box_unknown'
    data = json.loads(report.build(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    assert data['packing'][0]['shipped'] is False


def test_weight_mismatch_isolates_one_box_only(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_long_parts(db, tmp_path)
    rows = manifest_rows('S#1') + [
        {'id': 'M2', 'packing': 'ORD1', 'box_no': 'S#2', 'tracking': 'TRK-S#2',
         'seal': 'SEAL-S#2', 'weighed': 5.0, 'scanned_at': '2026-11-21T16:05:00'}]
    m = write(tmp_path, 'm.json', {'version': 'v1', 'rows': rows})
    result = carrier.manifest(db, m)
    assert [(r['status'], r['reason']) for r in result] == [
        ('已交运', 'box_shipped'), ('待核', 'weight_out_of_range')]
    conn = connect(db)
    shipped = [r['id'] for r in conn.execute('SELECT * FROM carrier_box')]
    conn.close()
    assert shipped == ['ORD1:S#1']  # 另一箱的已签回执保留
    data = json.loads(report.build(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    entry = data['packing'][0]
    assert entry['shipped'] is False
    assert {b['box_no']: b['shipment']['status'] for b in entry['boxes']} == {
        'S#1': '已交运', 'S#2': '待交运'}


def test_replay_and_duplicate_scan_add_no_packages(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_long_parts(db, tmp_path)
    m = write(tmp_path, 'm.json', {'version': 'v1', 'rows': manifest_rows('S#1', 'S#2')})
    first = carrier.manifest(db, m)
    assert carrier.manifest(db, m) == first  # 重放同一文件
    dup = write(tmp_path, 'dup.json', {'version': 'v1', 'rows': [
        dict(row, id='M9') for row in manifest_rows('S#1')]})
    result = carrier.manifest(db, dup)
    assert (result[0]['status'], result[0]['reason']) == ('已交运', 'duplicate_scan')
    conn = connect(db)
    assert conn.execute('SELECT count(*) FROM carrier_box').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM carrier_manifest').fetchone()[0] == 3
    conn.close()


def test_signed_waybill_is_immutable(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_long_parts(db, tmp_path)
    m = write(tmp_path, 'm.json', {'version': 'v1', 'rows': manifest_rows('S#1')})
    assert carrier.manifest(db, m)[0]['status'] == '已交运'
    altered = write(tmp_path, 'alt.json', {'version': 'v1', 'rows': [
        {'id': 'M2', 'packing': 'ORD1', 'box_no': 'S#1', 'tracking': 'TRK-OTHER',
         'seal': 'SEAL-OTHER', 'weighed': 1.9, 'scanned_at': '2026-11-21T16:00:00'}]})
    result = carrier.manifest(db, altered)
    assert (result[0]['status'], result[0]['reason']) == ('待核', 'waybill_conflict')
    conn = connect(db)
    body = json.loads(conn.execute(
        "SELECT body FROM carrier_box WHERE id='ORD1:S#1'").fetchone()[0])
    conn.close()
    assert body['tracking'] == 'TRK-S#1' and body['seal'] == 'SEAL-S#1'


def test_moved_part_needs_new_plan_version_and_fresh_seal(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_long_parts(db, tmp_path)
    m = write(tmp_path, 'm.json', {'version': 'v1', 'rows': manifest_rows('S#1', 'S#2')})
    assert all(r['status'] == '已交运' for r in carrier.manifest(db, m))
    # 签发后制件移动：新版本装箱图（新计划号）+ 新封箱证据，旧运单原样保留。
    req2 = write(tmp_path, 'req2.json', {'version': 'v2', 'rows': [
        order('ORD1-r2', parts=long_fragile_parts(), boxes=long_part_boxes())]})
    assert packing.plan(db, req2)[0]['status'] == '已装箱'
    assert sorted(box_layouts(db, 'ORD1-r2')) == ['ORD1-r2:box:S#1', 'ORD1-r2:box:S#2']
    rows = [dict(row, id=f'N{i}', packing='ORD1-r2', seal=f'SEAL2-{row["box_no"]}')
            for i, row in enumerate(manifest_rows('S#1', 'S#2'), 1)]
    m2 = write(tmp_path, 'm2.json', {'version': 'v2', 'rows': rows})
    assert all(r['status'] == '已交运' for r in carrier.manifest(db, m2))
    conn = connect(db)
    old = json.loads(conn.execute(
        "SELECT body FROM carrier_box WHERE id='ORD1:S#1'").fetchone()[0])
    assert old['seal'] == 'SEAL-S#1'
    assert conn.execute('SELECT count(*) FROM carrier_box').fetchone()[0] == 4
    old_manifest = conn.execute(
        "SELECT status, reason FROM carrier_manifest WHERE id='M1'").fetchone()
    assert (old_manifest['status'], old_manifest['reason']) == ('已交运', 'box_shipped')
    conn.close()


def test_manifest_checks_cutoff_and_plan_state(tmp_path):
    db = tmp_path / 'ledger.db'
    plan_long_parts(db, tmp_path)
    late = write(tmp_path, 'late.json', {'version': 'v1', 'rows': [
        dict(row, id='M-late', scanned_at='2026-11-21T19:00:00')
        for row in manifest_rows('S#1')]})
    assert carrier.manifest(db, late)[0]['reason'] == 'cutoff_missed'
    missing = write(tmp_path, 'miss.json', {'version': 'v1', 'rows': [
        {'id': 'M-plan', 'packing': 'ORD9', 'box_no': 'S#1', 'tracking': 'T', 'seal': 'S',
         'weighed': 1.9, 'scanned_at': '2026-11-21T16:00:00'}]})
    assert carrier.manifest(db, missing)[0]['reason'] == 'packing_missing'
    noseal = write(tmp_path, 'noseal.json', {'version': 'v1', 'rows': [
        dict({k: v for k, v in row.items() if k != 'seal'}, id='M-noseal')
        for row in manifest_rows('S#1')]})
    assert carrier.manifest(db, noseal)[0]['reason'] == 'seal_missing'


def test_report_reads_back_diagnosis_and_shipments(tmp_path):
    db = tmp_path / 'ledger.db'
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order('ORD-S', contract='single', parts=long_fragile_parts(), boxes=long_part_boxes()),
        order('ORD-M', parts=long_fragile_parts(), boxes=long_part_boxes())]})
    result = packing.plan(db, req)
    assert [r['status'] for r in result] == ['待协调', '已装箱']
    m = write(tmp_path, 'm.json', {'version': 'v1', 'rows': [
        dict(row, packing='ORD-M') for row in manifest_rows('S#1')]})
    carrier.manifest(db, m)
    data = json.loads(report.build(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    plans = {p['plan']: p for p in data['packing']}
    assert plans['ORD-S']['reason'] == 'certified_box_unavailable'
    assert plans['ORD-S']['diagnosis'][0]['cause'] == 'certified_box_unavailable'
    assert plans['ORD-M']['shipped'] is False
    assert len(data['entities']['packing_layout']) > 0
    assert len(data['entities']['carrier_box']) == 1
    assert all(s['verified'] for s in data['sources'])


def test_plan_replay_is_idempotent(tmp_path):
    db = tmp_path / 'ledger.db'
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=long_fragile_parts(), boxes=long_part_boxes())]})
    first = packing.plan(db, req)
    assert packing.plan(db, req) == first
    conn = connect(db)
    assert conn.execute('SELECT count(*) FROM packing_layout').fetchone()[0] == 3
    conn.close()


def test_plan_identity_conflict_on_changed_order(tmp_path):
    db = tmp_path / 'ledger.db'
    req = write(tmp_path, 'req.json', {'version': 'v1', 'rows': [
        order(parts=long_fragile_parts(), boxes=long_part_boxes())]})
    packing.plan(db, req)
    changed = write(tmp_path, 'changed.json', {'version': 'v2', 'rows': [
        order(parts=long_fragile_parts()[:1], boxes=long_part_boxes())]})
    with pytest.raises(ValueError, match='identity_conflict'):
        packing.plan(db, changed)


SAMPLES = Path(__file__).resolve().parent.parent / 'samples'


def test_offline_samples_run_end_to_end(tmp_path):
    db = tmp_path / 'ledger.db'
    multi = packing.plan(db, SAMPLES / 'packing_multi_long_parts.json')
    single = packing.plan(db, SAMPLES / 'packing_single_long_parts.json')
    cost = packing.plan(db, SAMPLES / 'packing_cost_tiers.json')
    assert (multi[0]['status'], multi[0]['reason']) == ('已装箱', 'layout_selected')
    assert (single[0]['status'], single[0]['reason']) == ('待协调', 'certified_box_unavailable')
    assert (cost[0]['status'], cost[0]['reason']) == ('已装箱', 'layout_selected')
    assert sorted(box_layouts(db, 'ORD-MULTI')) == ['ORD-MULTI:box:S#1', 'ORD-MULTI:box:S#2']
    assert layouts(db, 'ORD-COST')['ORD-COST:summary']['total_price'] == 24

    full = carrier.manifest(db, SAMPLES / 'carrier_manifest_full.json')
    assert [r['status'] for r in full] == ['已交运', '已交运']
    data = json.loads(report.build(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    plans = {p['plan']: p for p in data['packing']}
    assert plans['ORD-MULTI']['shipped'] is True
    assert plans['ORD-SINGLE']['diagnosis'][0]['cause'] == 'certified_box_unavailable'

    db2 = tmp_path / 'ledger2.db'
    packing.plan(db2, SAMPLES / 'packing_multi_long_parts.json')
    partial = carrier.manifest(db2, SAMPLES / 'carrier_manifest_partial.json')
    assert partial[0]['status'] == '已交运'
    data = json.loads(report.build(db2, tmp_path / 'r2.json').read_text(encoding='utf-8'))
    assert data['packing'][0]['shipped'] is False

    db3 = tmp_path / 'ledger3.db'
    packing.plan(db3, SAMPLES / 'packing_multi_long_parts.json')
    mismatch = carrier.manifest(db3, SAMPLES / 'carrier_manifest_weight_mismatch.json')
    assert [(r['status'], r['reason']) for r in mismatch] == [
        ('已交运', 'box_shipped'), ('待核', 'weight_out_of_range')]
