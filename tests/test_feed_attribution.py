import json
import pytest
from sliceledger import operations, report, production, inventory, lot_quality, feed
from sliceledger.store import connect
from sliceledger.feed import part_segments, part_lot_usage, latest_feed


def write(root, name, obj):
    p = root / name
    p.write_text(json.dumps(obj), encoding='utf-8')
    return p


def slice_row(id, usage='外观打样', grams=30):
    return dict(id=id, machine='M1', size=[20, 20, 20], build_limit=[200, 200, 200],
                nozzle=0.4, layer=0.2, material='PLA', grams=grams, usage=usage)


def reserve_row(id, job, lot, grams=40, available=200):
    return {'id': id, 'job': job, 'lot': lot, 'material': 'PLA',
            'available_grams': available, 'grams': grams}


def run_events(interrupted=False):
    events = [{'seq': 1, 'state': 'queued', 'at': '2026-01-01T10:00:00'},
              {'seq': 2, 'state': 'running', 'at': '2026-01-01T10:01:00'}]
    if interrupted:
        events += [{'seq': 3, 'state': 'interrupted', 'at': '2026-01-01T10:05:00'},
                   {'seq': 4, 'state': 'resumed', 'at': '2026-01-01T10:20:00'},
                   {'seq': 5, 'state': 'completed', 'at': '2026-01-01T11:00:00'}]
    else:
        events.append({'seq': 3, 'state': 'completed', 'at': '2026-01-01T11:00:00'})
    return events


def dual_lot_feed(fid='F1', part='P1', run='RUN1', job='J1', reservation='A1',
                  swap_position=15, total=24, l1_grams=15, l2_grams=9, bad=None):
    """Three layers of 8g each; the roll swap at swap_position splits layer 3."""
    layers = [{'layer': 1, 'start': 0, 'end': 8},
              {'layer': 2, 'start': 8, 'end': 16},
              {'layer': 3, 'start': 16, 'end': total}]
    changes = [{'at': '2026-01-01T10:40:00', 'lot': 'L2', 'position': swap_position}]
    weighings = [{'lot': 'L1', 'material': 'PLA', 'start_grams': 100, 'end_grams': 100 - l1_grams},
                 {'lot': 'L2', 'material': 'PLA', 'start_grams': 80, 'end_grams': 80 - l2_grams}]
    row = {'id': fid, 'run': run, 'job': job, 'reservation': reservation, 'machine': 'M1',
           'parts': [part], 'extrusions': [0, 8, 16, total], 'layers': layers,
           'spool_changes': changes, 'weighings': weighings}
    if bad == 'reversed_events':
        row['extrusions'] = list(reversed(row['extrusions']))
    elif bad == 'regression':
        row['extrusions'] = [0, 8, 7, total]
    elif bad == 'double_claim':
        row['spool_changes'] = [dict(changes[0], position=10),
                                dict(changes[0], lot='L3', position=12)]
        row['weighings'] = [{'lot': 'L1', 'material': 'PLA', 'start_grams': 100, 'end_grams': 90},
                            {'lot': 'L2', 'material': 'PLA', 'start_grams': 80, 'end_grams': 78},
                            {'lot': 'L3', 'material': 'PLA', 'start_grams': 50, 'end_grams': 38}]
    elif bad == 'gap':
        row['layers'] = [{'layer': 1, 'start': 0, 'end': 8},
                         {'layer': 3, 'start': 9, 'end': 16},
                         {'layer': 4, 'start': 16, 'end': total}]
    elif bad == 'missing_swap_position':
        row['spool_changes'] = [{'at': '2026-01-01T10:40:00', 'lot': 'L2'}]
    elif bad == 'missing_weighing':
        row['weighings'] = row['weighings'][:1]
    elif bad == 'weighing_conflict':
        row['weighings'][1]['end_grams'] = 80 - (l2_grams + 5)
    elif bad == 'machine_mismatch':
        row['machine'] = 'M9'
    elif bad == 'identity_mismatch':
        row['job'] = 'J9'
    elif bad == 'idle_pause':
        # The 15-minute idle pause must not add extrusion; reading stays flat.
        row['extrusions'] = [0, 8, 16, 16, total]
        row['layers'] = [{'layer': 1, 'start': 0, 'end': 8},
                         {'layer': 2, 'start': 8, 'end': 16},
                         {'layer': 3, 'start': 16, 'end': total}]
    return {'version': 'v1', 'rows': [row]}


def part_row(pid, reservation='A1', run='RUN1', job='J1', used=24):
    return {'id': pid, 'job': job, 'reservation': reservation, 'run_id': run,
            'measured': [20, 20, 20], 'nominal': [20, 20, 20], 'tolerance': 0.2, 'used_grams': used}


def setup_dual_lot(root, db, usage='外观打样', interrupted=False, p2=True, p2_lot='L2'):
    """L1 feeds P1 (with a mid-layer swap to L2); P2 uses p2_lot only when requested."""
    jobs = [slice_row('J1', usage=usage)]
    reservations = [reserve_row('A1', 'J1', 'L1', grams=40),
                    reserve_row('A2', 'J2', p2_lot, grams=40),
                    reserve_row('A3', 'J2', 'L2', grams=10)]
    if p2:
        jobs.append(slice_row('J2', usage=usage))
    operations.first(db, write(root, 'slice.json', {'version': 'v1', 'rows': jobs}))
    operations.second(db, write(root, 'material.json', {'version': 'v1', 'rows': reservations}))
    production.import_runs(db, write(root, 'run1.json', {'version': 'v1', 'rows': [
        {'id': 'RUN1', 'job': 'J1', 'reservation': 'A1', 'machine': 'M1',
         'events': run_events(interrupted=interrupted)}]}))
    if p2:
        production.import_runs(db, write(root, 'run2.json', {'version': 'v1', 'rows': [
            {'id': 'RUN2', 'job': 'J2', 'reservation': 'A2', 'machine': 'M1',
             'events': run_events()}]}))


def import_dual_feed(root, db, **kw):
    return feed.import_feed(db, write(root, 'feed1.json', dual_lot_feed(**kw)))


def accept(root, db, pid='P1', used=24, reservation='A1', run='RUN1', job='J1'):
    return operations.third(db, write(root, f'part-{pid}.json',
                                      {'version': 'v1', 'rows': [part_row(pid, reservation, run, job, used)]}))


def test_mid_layer_spool_swap_splits_layer_and_settles_both_rolls(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db)
    result = import_dual_feed(tmp_path, db)
    assert result[0]['status'] == '可信'
    segs = part_segments(connect(db), 'P1')
    seg_bodies = [json.loads(s['body']) for s in segs]
    assert [(s['layer'], s['lot']) for s in seg_bodies] == [
        (1, 'L1'), (2, 'L1'), (2, 'L2'), (3, 'L2')]
    split = [s for s in seg_bodies if s['split']]
    assert len(split) == 2 and sum(s['grams'] for s in split) == 8
    assert {s['lot']: round(s['grams'], 6) for s in seg_bodies
            if s['layer'] == 2} == {'L1': 7.0, 'L2': 1.0}
    usage = json.loads(part_lot_usage(connect(db), 'P1')[-1]['body'])
    assert {x['lot']: x['grams'] for x in usage['usage']} == {'L1': 15.0, 'L2': 9.0}
    assert accept(tmp_path, db)[0]['reason'] == 'part_accepted'
    movements = [json.loads(x['body']) for x in connect(db).execute('SELECT * FROM material_movement')]
    by_lot = {m['lot']: m['used_grams'] for m in movements}
    assert by_lot == {'L1': 15.0, 'L2': 9.0}
    assert inventory.balance(db, 'L1')['consumed'] == 15
    assert inventory.balance(db, 'L2')['consumed'] == 9


def test_single_lot_trace_order_rejects_multi_lot_part(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db, usage='单批次追溯')
    import_dual_feed(tmp_path, db)
    result = accept(tmp_path, db)
    assert result[0]['status'] == '拒收' and result[0]['reason'] == 'multi_lot_trace_violation'
    conn = connect(db)
    assert conn.execute("SELECT status FROM print_part WHERE id='P1'").fetchone()[0] == '拒收'
    assert conn.execute('SELECT count(*) FROM material_movement').fetchone()[0] == 0


def test_appearance_order_allows_same_material_qualified_rolls(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db, usage='外观打样')
    import_dual_feed(tmp_path, db)
    assert accept(tmp_path, db)[0]['reason'] == 'part_accepted'


def test_quarantine_recalls_only_parts_proven_to_contain_lot(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db, p2_lot='L1')
    import_dual_feed(tmp_path, db)
    feed.import_feed(db, write(tmp_path, 'feed2.json', {
        'version': 'v1', 'rows': [{
            'id': 'F2', 'run': 'RUN2', 'job': 'J2', 'reservation': 'A2', 'machine': 'M1', 'parts': ['P2'],
            'extrusions': [0, 8, 16, 24],
            'layers': [{'layer': 1, 'start': 0, 'end': 8}, {'layer': 2, 'start': 8, 'end': 16},
                       {'layer': 3, 'start': 16, 'end': 24}],
            'spool_changes': [],
            'weighings': [{'lot': 'L1', 'material': 'PLA', 'start_grams': 90, 'end_grams': 66}]}]}))
    accept(tmp_path, db)
    accept(tmp_path, db, pid='P2', reservation='A2', run='RUN2', job='J2')
    inspection = {'id': 'Q1', 'lot': 'L2', 'nominal_diameter': 1.75, 'diameter_tolerance': 0.05,
                  'diameter_samples': [1.9, 1.91], 'moisture': 0.02, 'max_moisture': 0.05}
    assert lot_quality.assess(db, write(tmp_path, 'inspect-l2.json',
                                        {'version': 'v1', 'rows': [inspection]}))[0]['status'] == '隔离'
    conn = connect(db)
    assert conn.execute("SELECT status FROM print_part WHERE id='P1'").fetchone()[0] == '撤回'
    assert conn.execute("SELECT status FROM print_part WHERE id='P2'").fetchone()[0] == '已验收'
    recalls = [json.loads(x['body'])['part'] for x in conn.execute('SELECT * FROM part_recall')]
    assert recalls == ['P1']
    data = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert [x['part'] for x in data['impact']['certain_recalls']] == ['P1']
    assert data['impact']['possible_impact'] == []


def test_quarantine_before_late_feed_converges_to_same_final_state(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db)
    # Inspection and review list arrive first; the swap evidence is still missing.
    inspection = {'id': 'Q1', 'lot': 'L2', 'nominal_diameter': 1.75, 'diameter_tolerance': 0.05,
                  'diameter_samples': [1.9, 1.91], 'moisture': 0.02, 'max_moisture': 0.05}
    lot_quality.assess(db, write(tmp_path, 'inspect-early.json',
                                 {'version': 'v1', 'rows': [inspection]}))
    import_dual_feed(tmp_path, db)
    accept(tmp_path, db)
    conn = connect(db)
    assert conn.execute("SELECT status FROM print_part WHERE id='P1'").fetchone()[0] == '撤回'
    assert conn.execute('SELECT count(*) FROM part_recall').fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM part_review WHERE status='待复核'").fetchone()[0] == 0
    assert inventory.balance(db, 'L2')['consumed'] == 9


def test_acceptance_before_late_quarantine_converges_to_same_final_state(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db)
    import_dual_feed(tmp_path, db)
    accept(tmp_path, db)
    inspection = {'id': 'Q1', 'lot': 'L2', 'nominal_diameter': 1.75, 'diameter_tolerance': 0.05,
                  'diameter_samples': [1.9, 1.91], 'moisture': 0.02, 'max_moisture': 0.05}
    lot_quality.assess(db, write(tmp_path, 'inspect-late.json',
                                 {'version': 'v1', 'rows': [inspection]}))
    conn = connect(db)
    assert conn.execute("SELECT status FROM print_part WHERE id='P1'").fetchone()[0] == '撤回'
    assert conn.execute('SELECT count(*) FROM part_recall').fetchone()[0] == 1
    assert inventory.balance(db, 'L2')['consumed'] == 9


def test_feed_replay_and_part_replay_do_not_double_consume(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db)
    f = write(tmp_path, 'feed1.json', dual_lot_feed())
    assert feed.import_feed(db, f) == feed.import_feed(db, f)
    p = write(tmp_path, 'part-P1.json', {'version': 'v1', 'rows': [part_row('P1')]})
    assert operations.third(db, p) == operations.third(db, p)
    conn = connect(db)
    assert conn.execute('SELECT count(*) FROM material_movement').fetchone()[0] == 2
    assert inventory.balance(db, 'L1')['consumed'] == 15
    assert inventory.balance(db, 'L2')['consumed'] == 9


def test_superseding_feed_resettles_without_double_consumption(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db)
    import_dual_feed(tmp_path, db)
    accept(tmp_path, db)
    # A corrected feed moves the swap boundary from 15 to 12: L1 takes 12g, L2 takes 12g.
    corrected = dual_lot_feed(fid='F1B', swap_position=12, l1_grams=12, l2_grams=12)
    feed.import_feed(db, write(tmp_path, 'feed1b.json', corrected))
    conn = connect(db)
    movements = [json.loads(x['body']) for x in conn.execute('SELECT * FROM material_movement')]
    assert {m['lot']: m['used_grams'] for m in movements} == {'L1': 12.0, 'L2': 12.0}
    assert inventory.balance(db, 'L1')['consumed'] == 12
    assert inventory.balance(db, 'L2')['consumed'] == 12
    assert latest_feed(conn, 'RUN1')['id'] == 'F1B'


def test_missing_swap_position_blocks_attribution_and_acceptance(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db)
    result = import_dual_feed(tmp_path, db, bad='missing_swap_position')
    assert result[0]['reason'] == 'spool_change_position_missing'
    assert part_lot_usage(connect(db), 'P1') == []
    assert accept(tmp_path, db)[0]['reason'] == 'feed_attribution_missing'
    assert connect(db).execute('SELECT count(*) FROM material_movement').fetchone()[0] == 0


@pytest.mark.parametrize('bad,reason', [
    ('reversed_events', 'extrusion_regression'),
    ('regression', 'extrusion_regression'),
    ('double_claim', 'spool_change_double_claim'),
    ('gap', 'layer_span_conflict'),
    ('missing_weighing', 'roll_weighing_missing'),
    ('weighing_conflict', 'roll_weighing_conflict'),
    ('machine_mismatch', 'feed_machine_mismatch'),
    ('identity_mismatch', 'feed_identity_mismatch'),
])
def test_untrusted_feed_evidence_codes(tmp_path, bad, reason):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db)
    result = import_dual_feed(tmp_path, db, bad=bad)
    assert result[0] == {'subject': 'F1', 'status': '待核', 'reason': reason}
    assert latest_feed(connect(db), 'RUN1')['status'] == '待核'


def test_untrusted_feed_under_quarantine_goes_to_review_not_recall(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db)
    inspection = {'id': 'Q1', 'lot': 'L2', 'nominal_diameter': 1.75, 'diameter_tolerance': 0.05,
                  'diameter_samples': [1.9, 1.91], 'moisture': 0.02, 'max_moisture': 0.05}
    lot_quality.assess(db, write(tmp_path, 'inspect.json', {'version': 'v1', 'rows': [inspection]}))
    import_dual_feed(tmp_path, db, bad='missing_swap_position')
    data = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert [x['part'] for x in data['impact']['possible_impact']] == ['P1']
    assert data['impact']['certain_recalls'] == []
    assert [x['reason'] for x in data['impact']['untrusted_feed']] == ['spool_change_position_missing']
    # Once the boundary arrives, the review entry is resolved and turns into a definite recall.
    accept_feed = dual_lot_feed(fid='F2')
    feed.import_feed(db, write(tmp_path, 'feed-good.json', accept_feed))
    accept(tmp_path, db)
    conn = connect(db)
    assert conn.execute("SELECT status FROM print_part WHERE id='P1'").fetchone()[0] == '撤回'
    assert conn.execute("SELECT count(*) FROM part_review WHERE status='待复核'").fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM part_recall').fetchone()[0] == 1


def test_idle_pause_does_not_count_as_extrusion(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db, interrupted=True)
    result = feed.import_feed(db, write(tmp_path, 'feed.json', dual_lot_feed(bad='idle_pause')))
    assert result[0]['status'] == '可信'
    usage = json.loads(part_lot_usage(connect(db), 'P1')[-1]['body'])
    assert usage['total_grams'] == 24


def test_trusted_feed_total_must_match_part_used_grams(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db)
    import_dual_feed(tmp_path, db)
    result = accept(tmp_path, db, used=20)
    assert result[0]['reason'] == 'attribution_consumption_mismatch'
    assert connect(db).execute('SELECT count(*) FROM material_movement').fetchone()[0] == 0


def test_quarantined_second_roll_blocks_acceptance(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db)
    inspection = {'id': 'Q1', 'lot': 'L2', 'nominal_diameter': 1.75, 'diameter_tolerance': 0.05,
                  'diameter_samples': [1.9, 1.91], 'moisture': 0.02, 'max_moisture': 0.05}
    lot_quality.assess(db, write(tmp_path, 'inspect.json', {'version': 'v1', 'rows': [inspection]}))
    import_dual_feed(tmp_path, db)
    result = accept(tmp_path, db)
    assert result[0]['status'] == '拒收' and result[0]['reason'] == 'lot_quarantined'


def test_report_reads_back_segments_and_order_usage(tmp_path):
    db = tmp_path / 'db'
    setup_dual_lot(tmp_path, db, usage='单批次追溯')
    import_dual_feed(tmp_path, db)
    out = report.build(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    segments = [json.loads(x['body']) for x in data['entities']['material_segment']]
    assert any(s['layer'] == 2 and s['lot'] == 'L2' and s['split'] for s in segments)
    assert any(s['source_id'] is not None for s in data['entities']['material_segment'])
    job = json.loads(data['entities']['slice_export'][0]['body'])
    assert job['usage'] == '单批次追溯'


def test_feed_requires_completed_run_and_matching_export_identity(tmp_path):
    db = tmp_path / 'db'
    operations.first(db, write(tmp_path, 'slice.json', {'version': 'v1', 'rows': [slice_row('J1')]}))
    operations.second(db, write(tmp_path, 'material.json', {'version': 'v1', 'rows': [
        reserve_row('A1', 'J1', 'L1')]}))
    production.import_runs(db, write(tmp_path, 'run.json', {'version': 'v1', 'rows': [
        {'id': 'RUN1', 'job': 'J1', 'reservation': 'A1', 'machine': 'M1',
         'events': run_events(interrupted=True)[:-1]}]}))
    result = feed.import_feed(db, write(tmp_path, 'feed.json', dual_lot_feed()))
    assert result[0]['reason'] == 'run_not_completed'
