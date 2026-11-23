"""Acceptance tests for the offline fault candidates and adaptive test tree.

Four synthetic single faults.  Today's temperature and current readings are
compatible with all four; the faults differ in their residual heat and in
what the controlled tests reveal:

  F1 fan bearing       -> replace fan module      F2 fan driver board -> fan module
  F3 heater relay      -> replace heater control  F4 chamber seal    -> heater control

T1 is a 2-minute controlled fan drive after stop/isolation; an abnormal result
is compatible only with F1/F2, a normal result only with F3/F4.  T2 is a
5-minute independent signal test that separates within each pair.  A T1 that
leaves the chamber hot invalidates T2's precondition and forces a cooldown.
"""
import json

import pytest

from sliceledger import maintenance, manual, production
from sliceledger.store import connect


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def diagnostic_manual(combinations=None):
    return {
        'version': 'manual-v1',
        'structure': {
            'description': 'M1 切片机温度/送料簇',
            'modules': [
                {'id': 'fan_module', 'repair_minutes': 40, 'description': '风扇模块'},
                {'id': 'heater_control', 'repair_minutes': 55, 'description': '加热控制模块'},
            ],
            'cooling_rate': 1.0,
        },
        'probes': [
            {'id': 'T', 'sensor': 'temperature', 'module': 'heater_control',
             'position': '喷头附近'},
            {'id': 'FAN', 'sensor': 'fan_rpm', 'module': 'fan_module',
             'position': '散热风扇'},
            {'id': 'I', 'sensor': 'current', 'module': 'heater_control',
             'position': '加热回路'},
        ],
        'sensor_units': [
            {'machine': 'M1', 'sensor': 'temperature', 'scale': 1.0, 'offset': 0.0},
            {'machine': 'M1', 'sensor': 'fan_rpm', 'scale': 1.0, 'offset': 0.0},
            {'machine': 'M1', 'sensor': 'current', 'scale': 1.0, 'offset': 0.0},
        ],
        'faults': [
            # All four raise temperature and current alike; the visible
            # readings therefore cannot separate them.  They differ in the
            # fan subsystem and in what the controlled tests reveal.
            {'id': 'F1', 'component': '风扇轴承', 'module': 'fan_module',
             'action': {'action': 'replace', 'module': 'fan_module'},
             'signature': {'temperature': {'effect': 'high', 'delay_minutes': 2,
                                           'residual_effect': 'high'},
                           'fan_rpm': {'effect': 'low', 'delay_minutes': 0},
                           'current': {'effect': 'high', 'delay_minutes': 1}}},
            {'id': 'F2', 'component': '风扇驱动板', 'module': 'fan_module',
             'action': {'action': 'replace', 'module': 'fan_module'},
             'signature': {'temperature': {'effect': 'high', 'delay_minutes': 2,
                                           'residual_effect': 'same'},
                           'fan_rpm': {'effect': 'low', 'delay_minutes': 0},
                           'current': {'effect': 'high', 'delay_minutes': 1}}},
            {'id': 'F3', 'component': '加热继电器', 'module': 'heater_control',
             'action': {'action': 'replace', 'module': 'heater_control'},
             'signature': {'temperature': {'effect': 'high', 'delay_minutes': 3,
                                           'residual_effect': 'high'},
                           'fan_rpm': {'effect': 'same', 'delay_minutes': 0},
                           'current': {'effect': 'high', 'delay_minutes': 1}}},
            {'id': 'F4', 'component': '腔体密封', 'module': 'heater_control',
             'action': {'action': 'replace', 'module': 'heater_control'},
             'signature': {'temperature': {'effect': 'high', 'delay_minutes': 4,
                                           'residual_effect': 'high'},
                           'fan_rpm': {'effect': 'same', 'delay_minutes': 0},
                           'current': {'effect': 'high', 'delay_minutes': 1}}},
        ],
        'combinations': combinations or [],
        'tests': [
            {'id': 'T1', 'minutes': 2, 'produces_heat': True,
             'requires': '停机隔离', 'risk': 1,
             'description': 'fan_drive 两分钟受控驱动测试',
             'outcomes': {'abnormal': ['F1', 'F2'], 'normal': ['F3', 'F4'],
                          '未判定': ['*']}},
            {'id': 'T2', 'minutes': 5, 'produces_heat': False,
             'requires': '停机隔离', 'risk': 0, 'depends_on': ['T1'],
             'min_phase_minutes': 0, 'max_phase_minutes': 2,
             'description': 'signal 独立信号测试',
             'outcomes': {'abnormal': ['F1', 'F3'], 'normal': ['F2', 'F4'],
                          '未判定': ['*']}},
        ],
        'test_trust': {'fan_drive_test': ['停机隔离'], 'signal_test': ['停机隔离']},
    }


def setup_case(root, commission='repair', manual_payload=None):
    """A complete upstream chain so machine runs are accepted."""
    db = root / 'ledger.db'
    from sliceledger import operations
    operations.first(db, write(root, 'slice.json', {'version': 'v1', 'rows': [
        {'id': 'J1', 'machine': 'M1', 'size': [20, 20, 20], 'build_limit': [200, 200, 200],
         'nozzle': 0.4, 'layer': 0.2, 'material': 'PLA', 'grams': 15}]}))
    operations.second(db, write(root, 'material.json', {'version': 'v1', 'rows': [
        {'id': 'A1', 'job': 'J1', 'lot': 'L1', 'material': 'PLA',
         'available_grams': 100, 'grams': 20}]}))
    maintenance.import_manual(db, write(root, 'manual.json', manual_payload or diagnostic_manual()))
    maintenance.delegate(db, write(root, 'commission.json', {'version': 'v1', 'rows': [
        {'id': 'C1', 'commission': commission, 'authority': 'shift-lead'}]}))
    return db


def running_then_interrupted(root, db, interrupted_at=6, run='RUN1'):
    events = [
        {'seq': 1, 'state': 'queued', 'at': '2026-01-01T10:00:00'},
        {'seq': 2, 'state': 'running', 'at': '2026-01-01T10:01:00'},
        {'seq': 3, 'state': 'interrupted', 'at': _ts_suffix(interrupted_at)},
    ]
    return production.import_runs(db, write(root, 'run.json', {'version': 'v1', 'rows': [
        {'id': run, 'job': 'J1', 'reservation': 'A1', 'machine': 'M1', 'events': events}]}))


def _ts_suffix(minute_from_run_start):
    # run starts 10:01; interrupted_at is measured from 10:00 wall time
    return f'2026-01-01T10:{minute_from_run_start:02d}:00'


def alarms(root, db, rows):
    return maintenance.import_sensors(db, write(root, 'alarms.json', {'version': 'v1', 'rows': rows}))


def temperature_alarm(at='2026-01-01T10:05:00', value=245, limit=230):
    return {'id': 'A-T', 'machine': 'M1', 'sensor': 'temperature', 'value': value,
            'limit': limit, 'at': at}


def current_alarm(at='2026-01-01T10:05:00', value=12, limit=10):
    return {'id': 'A-I', 'machine': 'M1', 'sensor': 'current', 'value': value,
            'limit': limit, 'at': at}


def fan_alarm(at='2026-01-01T10:05:00', value=900, limit=2000):
    return {'id': 'A-F', 'machine': 'M1', 'sensor': 'fan_rpm', 'value': value,
            'limit': limit, 'at': at}


def latest_plan(db):
    conn = connect(db)
    row = conn.execute("SELECT * FROM maintenance_plan ORDER BY rowid DESC LIMIT 1").fetchone()
    return json.loads(row['body']) if row else None


def latest_diag(db):
    conn = connect(db)
    row = conn.execute("SELECT * FROM maintenance_diagnosis ORDER BY rowid DESC LIMIT 1").fetchone()
    return json.loads(row['body']) if row else None


def execute(root, db, name, test, result, **extra):
    row = {'id': name, 'test': test, 'result': result,
           'machine_state': '停机隔离',
           'isolated_steps': ['断开加热回路', '登记隔离锁具'],
           'at': extra.pop('at', '2026-01-01T12:00:00'),
           'source': extra.pop('source', '受控试验')}
    row.update(extra)
    return maintenance.execute_test(db, write(root, f'{name}.json',
                                              {'version': 'v1', 'rows': [row]}))


# ---------------------------------------------------------------------------
# Commission = repair: T1 alone resolves the module
# ---------------------------------------------------------------------------

def test_repair_commission_stops_after_t1_because_module_is_determined(tmp_path):
    db = setup_case(tmp_path, 'repair')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    diag = latest_diag(db)
    assert diag['goal_met'] is False
    assert sorted(tuple(c) for c in diag['candidates']) == [('F1',), ('F2',), ('F3',), ('F4',)]
    plan = latest_plan(db)
    assert plan['next_test'] == 'T1'
    assert plan['tree']['worst_minutes'] == 2 + 55
    assert plan['authorises_release'] is False

    execute(tmp_path, db, 'E1', 'T1', 'abnormal')
    diag = latest_diag(db)
    assert sorted(tuple(c) for c in diag['candidates']) == [('F1',), ('F2',)]
    assert diag['goal_met'] is True
    plan = latest_plan(db)
    assert plan['next_test'] is None  # repair goal reached, no T2 needed


# ---------------------------------------------------------------------------
# Commission = root_cause: same evidence still requires T2 on both branches
# ---------------------------------------------------------------------------

def test_root_cause_commission_keeps_t2_after_t1_on_both_branches(tmp_path):
    db = setup_case(tmp_path, 'root_cause')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    plan = latest_plan(db)
    assert plan['next_test'] == 'T1'
    tree = plan['tree']
    assert {b['outcome'] for b in tree['branches']} == {'abnormal', 'normal'}
    for branch in tree['branches']:
        assert branch['next']['kind'] == 'test'
        assert branch['next']['test'] == 'T2'

    execute(tmp_path, db, 'E1', 'T1', 'normal')
    diag = latest_diag(db)
    assert sorted(tuple(c) for c in diag['candidates']) == [('F3',), ('F4',)]
    assert diag['goal_met'] is False
    plan = latest_plan(db)
    assert plan['next_test'] == 'T2'

    execute(tmp_path, db, 'E2', 'T2', 'normal')
    diag = latest_diag(db)
    assert [tuple(c) for c in diag['candidates']] == [('F4',)]
    assert diag['goal_met'] is True


def test_repair_goal_is_not_reported_as_root_cause(tmp_path):
    db = setup_case(tmp_path, 'repair')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    execute(tmp_path, db, 'E1', 'T1', 'abnormal')
    diag = latest_diag(db)
    # Same module is enough for repair, but F1 and F2 are still distinct roots.
    assert diag['goal_met'] is True
    assert len(diag['candidates']) == 2
    # The replacement module is determined, but the two roots are still
    # indistinguishable and the report must say so.
    assert sorted(tuple(c) for c in diag['retained_uncertainty']) == [('F1',), ('F2',)]
    assert diag['commission_kind'] == 'repair'


# ---------------------------------------------------------------------------
# Heat from T1 invalidates T2's precondition; plan adds a cooldown
# ---------------------------------------------------------------------------

def test_heat_from_t1_forces_cooldown_or_alternate_before_t2(tmp_path):
    payload = diagnostic_manual()
    payload['tests'][0]['heat_minutes'] = 20   # T1 leaves the chamber hot
    db = setup_case(tmp_path, 'root_cause', payload)
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    plan = latest_plan(db)
    tree = plan['tree']
    normal = next(b for b in tree['branches'] if b['outcome'] == 'normal')
    t2 = normal['next']
    # T2's precondition is violated by T1 heat, so the tree must not pretend
    # the original 2+5 minute sequence still applies.
    assert t2['kind'] == 'test'
    assert t2['cooldown_minutes'] >= 18
    assert t2['precondition']['cooldown_reason'] == 'phase_too_hot'
    # 2 min T1 + cooldown + 5 min T2 + 55 min worst repair
    assert plan['worst_minutes'] == 2 + t2['cooldown_minutes'] + 5 + 55


# ---------------------------------------------------------------------------
# Late interruption changes residual-heat interpretation
# ---------------------------------------------------------------------------

def test_late_interruption_time_changes_residual_heat_and_keeps_old_advice(tmp_path):
    db = setup_case(tmp_path, 'repair')
    running_then_interrupted(tmp_path, db, interrupted_at=6)
    # High reading taken at 10:05 while still running.
    alarms(tmp_path, db, [temperature_alarm(at='2026-01-01T10:05:00')])
    first = latest_diag(db)
    assert sorted(tuple(c) for c in first['candidates']) == [('F1',), ('F2',), ('F3',), ('F4',)]
    old_plan_count = len(plans(db))

    # A late, legitimate event says the machine was interrupted at 10:02;
    # the 10:05 high reading is residual heat, not a running anomaly.
    events = [
        {'seq': 1, 'state': 'queued', 'at': '2026-01-01T10:00:00'},
        {'seq': 2, 'state': 'running', 'at': '2026-01-01T10:01:00'},
        {'seq': 3, 'state': 'interrupted', 'at': '2026-01-01T10:02:00'},
        {'seq': 4, 'state': 'resumed', 'at': '2026-01-01T10:07:00'},
        {'seq': 5, 'state': 'interrupted', 'at': '2026-01-01T10:09:00'},
    ]
    production.import_runs(db, write(tmp_path, 'late.json', {'version': 'v1', 'rows': [
        {'id': 'RUN1', 'job': 'J1', 'reservation': 'A1', 'machine': 'M1', 'events': events}]}))

    conn = connect(db)
    context = json.loads(conn.execute("SELECT body FROM sensor_context WHERE id='A-T'").fetchone()[0])
    assert context['phase']['kind'] == '停机余热'
    assert context['residual'] is True
    diag = latest_diag(db)
    # Residual high temperature is compatible with F1/F3/F4 but not F2.
    assert ('F2',) not in [tuple(c) for c in diag['candidates']]
    # The old advice is still on file; a new revision was appended.
    assert len(plans(db)) > old_plan_count
    assert conn.execute("SELECT count(*) FROM issue WHERE code='late_machine_event'").fetchone()[0] == 1


def plans(db):
    from sliceledger.maintenance import plans_for
    conn = connect(db)
    return plans_for(conn, 'C1')


# ---------------------------------------------------------------------------
# Two faults coexisting; shared probe offset
# ---------------------------------------------------------------------------

def test_dual_fault_only_when_preapproved_and_shared_offset(tmp_path):
    payload = diagnostic_manual(combinations=[{'members': ['F1', 'F3'],
                                                  'action': {'action': 'replace',
                                                             'module': 'fan_module+heater_control'}}])
    db = setup_case(tmp_path, 'repair', payload)
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    diag = latest_diag(db)
    names = sorted('+'.join(c) for c in diag['candidates'])
    assert 'F1+F3' in names
    # A single-fault result can never be stitched into an unapproved pair,
    # and an approved pair is only a candidate when it appears verbatim.
    assert manual.outcome_permits(['F1', 'F2'], {'F1', 'F3'}) is False
    assert manual.outcome_permits(['F1+F3'], {'F1', 'F3'}) is True
    assert manual.outcome_permits(['F1'], {'F1', 'F3'}) is False
    # A combination with no evidence story is not invented: F2+F4 is not
    # approved and must never appear.
    assert 'F2+F4' not in names


# ---------------------------------------------------------------------------
# Conflicting test result vs. preset table
# ---------------------------------------------------------------------------

def test_result_contradicting_preset_table_is_not_silently_accepted(tmp_path):
    db = setup_case(tmp_path, 'root_cause')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    execute(tmp_path, db, 'E1', 'T1', 'abnormal')  # leaves {F1,F2}
    # T2 'normal' would mean F2/F4; F4 was eliminated, F2 survives -> {F2}.
    execute(tmp_path, db, 'E2', 'T2', 'normal')
    diag = latest_diag(db)
    assert [tuple(c) for c in diag['candidates']] == [('F2',)]
    # A result that permits no surviving candidate is recorded as contradictory.
    execute(tmp_path, db, 'E3', 'T2', 'abnormal')  # permits F1/F3, both gone
    diag = latest_diag(db)
    assert diag['state'] in ('无法解释', '有建议树')
    conn = connect(db)
    row = conn.execute("SELECT * FROM test_execution WHERE id='E3'").fetchone()
    assert row['status'] == '已登记'
    diag = latest_diag(db)
    assert diag['candidates'] == [] or all(
        manual.test_compatible(manual.parse_manual(diagnostic_manual()), set(c),
                               [{'test': 'T1', 'result': 'abnormal'},
                                {'test': 'T2', 'result': 'abnormal'}])
        for c in diag['candidates'])
    assert diag['state'] == '无法解释'


# ---------------------------------------------------------------------------
# Failed test replay + execution/advice separation
# ---------------------------------------------------------------------------

def test_failed_test_heat_is_kept_and_replay_is_idempotent(tmp_path):
    db = setup_case(tmp_path, 'root_cause')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    before = [p['id'] for p in plans(db)]
    file = write(tmp_path, 'failed.json', {'version': 'v1', 'rows': [
        {'id': 'E9', 'test': 'T1', 'result': 'abnormal', 'failure': True,
         'machine_state': '停机隔离', 'isolated_steps': ['断开加热回路'],
         'at': '2026-01-01T13:00:00', 'source': '受控试验', 'heat_minutes': 2}]})
    first = maintenance.execute_test(db, file)
    second = maintenance.execute_test(db, file)
    assert first == second
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM test_execution WHERE id='E9'").fetchone()[0] == 1
    diag = latest_diag(db)
    # A failed test still heats the machine and must appear in the next
    # precondition, not vanish.
    assert diag['evidence']['phase_state']['phase_minutes'] == 2
    # Suggested plans for untaken branches are not erased by execution.
    for plan_id in before:
        assert conn.execute('SELECT 1 FROM maintenance_plan WHERE id=?', (plan_id,)).fetchone()


def test_execution_without_isolation_is_rejected(tmp_path):
    db = setup_case(tmp_path, 'root_cause')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    result = maintenance.execute_test(db, write(tmp_path, 'bad.json', {'version': 'v1', 'rows': [
        {'id': 'E-BAD', 'test': 'T1', 'result': 'abnormal', 'machine_state': '运行中',
         'isolated_steps': [], 'at': '2026-01-01T12:00:00', 'source': '运行负载'}]}))
    assert result[0]['status'] == '待核'
    diag = latest_diag(db)
    assert len(diag['candidates']) == 4


# ---------------------------------------------------------------------------
# Same source, different content
# ---------------------------------------------------------------------------

def test_same_source_different_content_is_an_identity_conflict(tmp_path):
    db = setup_case(tmp_path, 'repair')
    running_then_interrupted(tmp_path, db)
    first = write(tmp_path, 'alarms.json', {'version': 'v1', 'rows': [temperature_alarm()]})
    maintenance.import_sensors(db, first)
    changed = write(tmp_path, 'alarms.json', {'version': 'v1', 'rows': [
        temperature_alarm(value=250)]})
    with pytest.raises(ValueError, match='identity_conflict'):
        maintenance.import_sensors(db, changed)
    conn = connect(db)
    raw = json.loads(conn.execute("SELECT body FROM sensor_alert WHERE id='A-T'").fetchone()[0])
    assert raw['value'] == 245


# ---------------------------------------------------------------------------
# Manual validation and versioning
# ---------------------------------------------------------------------------

def test_manual_is_versioned_and_rejects_unapproved_combination(tmp_path):
    db = tmp_path / 'ledger.db'
    maintenance.import_manual(db, write(tmp_path, 'manual.json', diagnostic_manual()))
    bad = diagnostic_manual()
    bad['version'] = 'manual-v2'
    bad['combinations'] = [['F1', 'F9']]
    with pytest.raises(manual.ManualError):
        maintenance.import_manual(db, write(tmp_path, 'bad.json', bad))
    conn = connect(db)
    versions = [r[0] for r in conn.execute('SELECT id FROM diagnostic_manual').fetchall()]
    assert versions == ['manual-v1']


# ---------------------------------------------------------------------------
# Fan drive on a parked machine cannot stand in for running load
# ---------------------------------------------------------------------------

def test_parked_fan_drive_cannot_claim_running_load_result(tmp_path):
    db = setup_case(tmp_path, 'root_cause')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    result = maintenance.execute_test(db, write(tmp_path, 'fake.json', {'version': 'v1', 'rows': [
        {'id': 'E-FAKE', 'test': 'T1', 'result': 'abnormal', 'machine_state': '停机隔离',
         'isolated_steps': ['按驱动测试按钮'], 'source': '运行负载',
         'at': '2026-01-01T12:00:00'}]}))
    assert result[0]['status'] == '待核'
    assert result[0]['reason'] == 'result_source_missing'
    # The parked fan test is not in the ledger as a diagnostic result, so no
    # candidate is removed by it.
    assert len(latest_diag(db)['candidates']) == 4


def test_running_load_source_is_not_accepted_for_parked_test(tmp_path):
    # A test declared to require running load must record the running state;
    # a parked machine's result cannot satisfy it.
    payload = diagnostic_manual()
    payload['tests'][0]['requires'] = '运行中'
    db = setup_case(tmp_path, 'root_cause', payload)
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    result = maintenance.execute_test(db, write(tmp_path, 'wrong_state.json', {'version': 'v1', 'rows': [
        {'id': 'E-RUN', 'test': 'T1', 'result': 'abnormal', 'machine_state': '停机隔离',
         'isolated_steps': ['登记隔离锁具'], 'source': '受控试验',
         'at': '2026-01-01T12:00:00'}]}))
    assert result[0]['status'] == '待核'
    assert result[0]['reason'] == 'machine_not_running'


# ---------------------------------------------------------------------------
# Shared calibration is required; per-probe zero points are rejected
# ---------------------------------------------------------------------------

def test_calibration_must_be_shared_across_probes(tmp_path):
    payload = diagnostic_manual()
    payload['sensor_units'] = [
        {'machine': 'M1', 'sensor': 'temperature', 'scale': 1.0, 'offset': 3.0},
        {'machine': 'M1', 'sensor': 'fan_rpm', 'scale': 1.0, 'offset': -120.0},
        {'machine': 'M1', 'sensor': 'current', 'scale': 1.0, 'offset': 0.0},
    ]
    with pytest.raises(manual.ManualError):
        manual.parse_manual(payload)
    db = tmp_path / 'ledger.db'
    with pytest.raises(manual.ManualError):
        maintenance.import_manual(db, write(tmp_path, 'bad_units.json', payload))


def test_machine_wide_calibration_applies_to_every_probe(tmp_path):
    payload = diagnostic_manual()
    payload['sensor_units'] = [{'machine': 'M1', 'sensor': '*', 'scale': 1.0,
                                'offset': 5.0}]
    parsed = manual.parse_manual(payload)
    assert parsed['sensor_units']['M1']['temperature']['offset'] == 5.0
    assert parsed['sensor_units']['M1']['fan_rpm']['offset'] == 5.0
    assert parsed['sensor_units']['M1']['current']['offset'] == 5.0


# ---------------------------------------------------------------------------
# Cost ties keep both bases; cost is worst shutdown then risk
# ---------------------------------------------------------------------------

def test_equal_cost_trees_are_both_kept(tmp_path):
    payload = diagnostic_manual()
    # Two alternative 2-minute split tests with the same outcome table and
    # the same risk: neither may win by insertion order.
    alpha = {'id': 'TA', 'minutes': 2, 'produces_heat': True, 'requires': '停机隔离',
             'risk': 1, 'description': 'fan_drive alpha',
             'outcomes': {'abnormal': ['F1', 'F2'], 'normal': ['F3', 'F4'], '未判定': ['*']}}
    beta = {'id': 'TB', 'minutes': 2, 'produces_heat': True, 'requires': '停机隔离',
            'risk': 1, 'description': 'fan_drive beta',
            'outcomes': {'abnormal': ['F1', 'F2'], 'normal': ['F3', 'F4'], '未判定': ['*']}}
    payload['tests'] = [alpha, beta,
                        {'id': 'T2', 'minutes': 5, 'requires': '停机隔离', 'risk': 0,
                         'description': 'signal',
                         'outcomes': {'abnormal': ['F1', 'F3'], 'normal': ['F2', 'F4'],
                                      '未判定': ['*']}}]
    db = setup_case(tmp_path, 'root_cause', payload)
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    conn = connect(db)
    rows = conn.execute("SELECT * FROM maintenance_plan ORDER BY rowid").fetchall()
    bodies = [json.loads(r['body']) for r in rows]
    # T2 (5 min) and TA/TB (2 min) all reach the goal in two tests, so the
    # worst-case shutdown time is identical (62 min) and risk is identical.
    # Every tied tree must be stored, including both TA-first and TB-first.
    assert sorted({b['next_test'] for b in bodies}) == ['T2', 'TA', 'TB']
    assert all(b['worst_minutes'] == 62 for b in bodies)
    assert all(b['risk'] == 1 for b in bodies)
    assert all(b['variant_count'] == len(bodies) for b in bodies)
    # Both equal-cost alternative tests survive as separate bases.
    assert len(rows) >= 2


def test_risk_breaks_a_tie_before_insertion_order(tmp_path):
    payload = diagnostic_manual()
    payload['tests'] = [
        {'id': 'TA', 'minutes': 2, 'produces_heat': True, 'requires': '停机隔离',
         'risk': 3, 'description': 'fan_drive',
         'outcomes': {'abnormal': ['F1', 'F2'], 'normal': ['F3', 'F4'], '未判定': ['*']}},
        {'id': 'TB', 'minutes': 2, 'produces_heat': True, 'requires': '停机隔离',
         'risk': 1, 'description': 'fan_drive',
         'outcomes': {'abnormal': ['F1', 'F2'], 'normal': ['F3', 'F4'], '未判定': ['*']}},
    ]
    db = setup_case(tmp_path, 'repair', payload)
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    plan = latest_plan(db)
    assert plan['next_test'] == 'TB'
    assert plan['variant_count'] == 1


# ---------------------------------------------------------------------------
# Cannot stop early: an unresolved branch is retained, not dropped
# ---------------------------------------------------------------------------

def test_unresolvable_branch_is_retained_not_silently_stopped(tmp_path):
    payload = diagnostic_manual()
    # T1 distinguishes groups but T2 is removed, so root cause is unreachable.
    payload['tests'] = [payload['tests'][0]]
    db = setup_case(tmp_path, 'root_cause', payload)
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    diag = latest_diag(db)
    assert diag['state'] == '委托不可达'
    assert diag['goal_met'] is False
    assert sorted(tuple(c) for c in diag['retained_uncertainty']) == [
        ('F1',), ('F2',), ('F3',), ('F4',)]
    assert latest_plan(db) is None


# ---------------------------------------------------------------------------
# Report view for the duty officer
# ---------------------------------------------------------------------------

def test_report_shows_candidates_evidence_preconditions_and_costs(tmp_path):
    from sliceledger import report
    db = setup_case(tmp_path, 'root_cause')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    out = report.build(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    m = data['maintenance']
    assert m['commission']['kind'] == 'root_cause'
    assert m['diagnosis']['candidates'] == [['F1'], ['F2'], ['F3'], ['F4']]
    assert m['diagnosis']['eliminated'] == []
    assert m['plans'][0]['next_test'] == 'T1'
    assert m['plans'][0]['precondition']['machine_state'] == '停机隔离'
    assert m['plans'][0]['worst_minutes'] == 62
    assert all(b['precondition'] for b in m['plans'][0]['branches'])
    assert m['plans'][0]['authorises_release'] is False
    # Every raw alarm row is still present in the entity section.
    assert any('A-T' in row['id'] for row in data['entities']['sensor_alert'])


def test_report_keeps_tied_bases_and_uncertainty_after_contradiction(tmp_path):
    from sliceledger import report
    db = setup_case(tmp_path, 'root_cause')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    execute(tmp_path, db, 'E1', 'T1', 'abnormal')
    execute(tmp_path, db, 'E2', 'T2', 'normal')
    execute(tmp_path, db, 'E3', 'T2', 'abnormal')  # contradicts the table
    data = json.loads(report.build(db, tmp_path / 'report.json').read_text(encoding='utf-8'))
    m = data['maintenance']
    assert m['diagnosis']['state'] == '无法解释'
    assert m['diagnosis']['candidates'] == []
    # Earlier plan revisions remain on file, not overwritten.
    revisions = {p['revision'] for p in m['plans']}
    assert len(revisions) >= 1
    assert len(data['entities']['maintenance_plan']) >= len(m['plans'])


# ---------------------------------------------------------------------------
# Late machine event reinterprets a reading and re-plans without erasing advice
# ---------------------------------------------------------------------------

def test_late_event_that_moves_reading_into_cooldown_changes_candidates(tmp_path):
    db = setup_case(tmp_path, 'repair')
    running_then_interrupted(tmp_path, db, interrupted_at=6)
    alarms(tmp_path, db, [temperature_alarm(at='2026-01-01T10:05:00')])
    before = latest_diag(db)
    assert sorted(tuple(c) for c in before['candidates']) == [
        ('F1',), ('F2',), ('F3',), ('F4',)]
    # A corrected interruption time puts the sample in residual heat, where a
    # fault that leaves no persistent heat (F2) explains less.
    events = [
        {'seq': 1, 'state': 'queued', 'at': '2026-01-01T10:00:00'},
        {'seq': 2, 'state': 'running', 'at': '2026-01-01T10:01:00'},
        {'seq': 3, 'state': 'interrupted', 'at': '2026-01-01T10:02:00'},
    ]
    production.import_runs(db, write(tmp_path, 'late2.json', {'version': 'v1', 'rows': [
        {'id': 'RUN1', 'job': 'J1', 'reservation': 'A1', 'machine': 'M1', 'events': events}]}))
    after = latest_diag(db)
    assert ('F2',) not in [tuple(c) for c in after['candidates']]
    # Settlement advice is not rewritten: old diagnosis ids remain.
    conn = connect(db)
    ids = [r[0] for r in conn.execute('SELECT id FROM maintenance_diagnosis').fetchall()]
    assert before['id'] in ids and after['id'] != before['id']


def test_raw_alert_body_never_changes_after_late_event(tmp_path):
    db = setup_case(tmp_path, 'repair')
    running_then_interrupted(tmp_path, db, interrupted_at=6)
    alarms(tmp_path, db, [temperature_alarm(at='2026-01-01T10:05:00')])
    conn = connect(db)
    raw_before = conn.execute("SELECT body FROM sensor_alert WHERE id='A-T'").fetchone()[0]
    events = [
        {'seq': 1, 'state': 'queued', 'at': '2026-01-01T10:00:00'},
        {'seq': 2, 'state': 'running', 'at': '2026-01-01T10:01:00'},
        {'seq': 3, 'state': 'interrupted', 'at': '2026-01-01T10:02:00'},
    ]
    production.import_runs(db, write(tmp_path, 'late3.json', {'version': 'v1', 'rows': [
        {'id': 'RUN1', 'job': 'J1', 'reservation': 'A1', 'machine': 'M1', 'events': events}]}))
    raw_after = conn.execute("SELECT body FROM sensor_alert WHERE id='A-T'").fetchone()[0]
    assert raw_before == raw_after


# ---------------------------------------------------------------------------
# Risk chooses among otherwise equal shutdown times; both minimum tests kept
# ---------------------------------------------------------------------------

def test_worst_shutdown_minutes_is_the_primary_cost(tmp_path):
    payload = diagnostic_manual()
    # T2 is cheap in minutes but does not split the pool into the needed
    # groups for the *repair* goal on its own (it splits {F1,F3}/{F2,F4},
    # which are cross-module).  A repair plan must therefore not stop on it.
    db = setup_case(tmp_path, 'repair', payload)
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    plan = latest_plan(db)
    assert plan['next_test'] == 'T1'      # 2 min test beats the 5 min T2
    assert plan['worst_minutes'] == 2 + 55


def test_repair_commission_does_not_stop_at_cross_module_branch(tmp_path):
    db = setup_case(tmp_path, 'repair')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    # T2 first leaves {F1,F2} or {F3,F4}; repair still needs one module.
    execute(tmp_path, db, 'E1', 'T1', 'abnormal')
    diag = latest_diag(db)
    assert {tuple(c) for c in diag['candidates']} == {('F1',), ('F2',)}
    assert diag['goal_met'] is True
    plan = latest_plan(db)
    assert plan['next_test'] is None
    assert plan['worst_minutes'] == 40     # fan module replacement only


# ---------------------------------------------------------------------------
# Eliminated candidate carries a concrete witness in the report
# ---------------------------------------------------------------------------

def test_report_names_the_eliminating_evidence(tmp_path):
    from sliceledger import report
    db = setup_case(tmp_path, 'repair')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    execute(tmp_path, db, 'E1', 'T1', 'abnormal')   # eliminates F3, F4
    data = json.loads(report.build(db, tmp_path / 'r.json').read_text(encoding='utf-8'))
    eliminated = {tuple(e['candidate']): e['witness'][0]
                  for e in data['maintenance']['diagnosis']['eliminated']}
    assert ('F3',) in eliminated and ('F4',) in eliminated
    for candidate in (('F3',), ('F4',)):
        w = eliminated[candidate]
        assert w['kind'] == 'test' and w['test'] == 'T1' and w['result'] == 'abnormal'
    # The retained uncertainty is still the two fan faults.
    assert data['maintenance']['diagnosis']['retained_uncertainty'] == [['F1'], ['F2']]


# ---------------------------------------------------------------------------
# Commission is fixed before inspection and cannot be switched later
# ---------------------------------------------------------------------------

def test_commission_cannot_be_switched_after_tests(tmp_path):
    db = setup_case(tmp_path, 'repair')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    switched = maintenance.delegate(db, write(tmp_path, 'switch.json', {'version': 'v1', 'rows': [
        {'id': 'C2', 'commission': 'root_cause', 'authority': 'shift-lead'}]}))
    assert switched[0]['status'] == '待核'
    assert switched[0]['reason'] == 'commission_already_fixed'
    execute(tmp_path, db, 'E1', 'T1', 'abnormal')
    late = maintenance.delegate(db, write(tmp_path, 'late.json', {'version': 'v1', 'rows': [
        {'id': 'C3', 'commission': 'root_cause', 'authority': 'shift-lead'}]}))
    assert late[0]['reason'] == 'commission_locked_after_test'
    assert latest_diag(db)['commission_kind'] == 'repair'


def test_neither_commission_ever_authorises_release(tmp_path):
    for kind in ('repair', 'root_cause'):
        root = tmp_path / kind
        root.mkdir()
        db = setup_case(root, kind)
        running_then_interrupted(root, db)
        alarms(root, db, [temperature_alarm(), current_alarm()])
        diag = latest_diag(db)
        assert diag['authorises_release'] is False
        assert diag['note'] == 'diagnosis_is_evidence_not_repair_proof'
        plan = latest_plan(db)
        assert plan['authorises_release'] is False
        # Release still requires the technician receipt path, unchanged.
        assert maintenance.release(db, write(root, 'release.json', {'version': 'v1', 'rows': [
            {'id': 'R1', 'alert': 'A-T', 'technician': 'tech1'}]}))[0]['status'] == '可复机'


# ---------------------------------------------------------------------------
# Heat precondition is enforced at execution, not just in the plan
# ---------------------------------------------------------------------------

def test_t2_is_rejected_while_chamber_still_hot_and_allowed_after_cooldown(tmp_path):
    payload = diagnostic_manual()
    payload['tests'][0]['heat_minutes'] = 20
    db = setup_case(tmp_path, 'root_cause', payload)
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    execute(tmp_path, db, 'E1', 'T1', 'normal')   # leaves 20 min of heat

    hot = maintenance.execute_test(db, write(tmp_path, 'hot.json', {'version': 'v1', 'rows': [
        {'id': 'E2', 'test': 'T2', 'result': 'normal', 'machine_state': '停机隔离',
         'isolated_steps': ['断开加热回路'], 'source': '受控试验',
         'at': '2026-01-01T12:10:00'}]}))
    assert hot[0]['status'] == '待核'
    assert hot[0]['reason'] == 'phase_too_hot'
    diag = latest_diag(db)
    # The rejected execution does not eliminate candidates.
    assert sorted(tuple(c) for c in diag['candidates']) == [('F3',), ('F4',)]

    # A recorded 18-minute cooldown makes T2 admissible again.
    cooled = maintenance.execute_test(db, write(tmp_path, 'cooled.json', {'version': 'v1', 'rows': [
        {'id': 'E3', 'test': 'T2', 'result': 'normal', 'machine_state': '停机隔离',
         'isolated_steps': ['断开加热回路', '冷却等待 18 分钟'],
         'cooldown_minutes': 18, 'source': '受控试验',
         'at': '2026-01-01T12:40:00'}]}))
    assert cooled[0]['status'] == '已登记'
    assert [tuple(c) for c in latest_diag(db)['candidates']] == [('F4',)]


def test_heat_from_failed_test_raises_next_precondition(tmp_path):
    payload = diagnostic_manual()
    payload['tests'][0]['heat_minutes'] = 20
    db = setup_case(tmp_path, 'root_cause', payload)
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    failed = maintenance.execute_test(db, write(tmp_path, 'fail.json', {'version': 'v1', 'rows': [
        {'id': 'E1', 'test': 'T1', 'result': 'abnormal', 'failure': True,
         'machine_state': '停机隔离', 'isolated_steps': ['断开加热回路'],
         'source': '受控试验', 'at': '2026-01-01T12:00:00'}]}))
    assert failed[0]['status'] == '失败'
    state = latest_diag(db)['evidence']['phase_state']
    assert state['phase_minutes'] == 20


def test_unexpected_result_token_is_not_registered(tmp_path):
    db = setup_case(tmp_path, 'root_cause')
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(), current_alarm()])
    result = maintenance.execute_test(db, write(tmp_path, 'odd.json', {'version': 'v1', 'rows': [
        {'id': 'E-ODD', 'test': 'T1', 'result': 'smells_wrong',
         'machine_state': '停机隔离', 'isolated_steps': ['断开加热回路'],
         'source': '受控试验', 'at': '2026-01-01T12:00:00'}]}))
    assert result[0]['status'] == '待核'
    assert result[0]['reason'] == 'unexpected_result'
    assert len(latest_diag(db)['candidates']) == 4


def test_shared_offset_is_applied_once_to_the_reading_not_the_limit(tmp_path):
    payload = diagnostic_manual()
    payload['sensor_units'] = [{'machine': 'M1', 'sensor': '*', 'scale': 1.0,
                                'offset': 10.0}]
    db = setup_case(tmp_path, 'repair', payload)
    running_then_interrupted(tmp_path, db)
    alarms(tmp_path, db, [temperature_alarm(value=235, limit=230),
                          current_alarm(value=12.5, limit=10)])
    conn = connect(db)
    t = json.loads(conn.execute("SELECT body FROM sensor_context WHERE id='A-T'").fetchone()[0])
    i = json.loads(conn.execute("SELECT body FROM sensor_context WHERE id='A-I'").fetchone()[0])
    # One shared offset: 235 - 10 = 225 <= 230 is not a temperature high,
    # while 12.5 - 10 = 2.5 <= 10 is not a current high either.
    assert t['calibrated_value'] == 225 and t['level'] == 'normal'
    assert i['calibrated_value'] == 2.5 and i['level'] == 'normal'
    assert t['calibration']['offset'] == i['calibration']['offset'] == 10.0
