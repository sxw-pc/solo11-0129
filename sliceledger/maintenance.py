"""Offline printer sensor maintenance: alerts, controlled tests and adaptive plans.

Deliberate separation of evidence:

``sensor_alert``
    Raw alarm rows exactly as imported.  Later diagnosis never rewrites them.
``sensor_context``
    How a reading is interpreted: run phase, shared slot calibration, level and
    residual-heat flag.  All readings taken at the same time on the same
    machine share one slot and one calibration; a zero point cannot be chosen
    per alarm.
``maintenance_commission``
    The delegated goal (``repair`` or ``root_cause``), fixed before inspection.
``maintenance_diagnosis``
    Append-only diagnosis revisions: surviving candidates, eliminated
    candidates with witness evidence, remaining uncertainty.
``maintenance_plan``
    Append-only suggested test trees.  Every recomputation is a new revision;
    plans for branches that were not taken remain readable.
``test_execution``
    What was actually done.  Executions never overwrite suggested plans.

A plan is advice only.  It never authorises return to service and never proves
a repair.
"""
import json

from .ingest import capture
from .store import connect, save, decide, issue
from . import manual as manual_mod
from . import production

PROBES = ('temperature', 'fan_rpm', 'current')


# ---------------------------------------------------------------------------
# Interpretation of raw sensor rows
# ---------------------------------------------------------------------------

def _unit_for(manual, machine, sensor):
    units = manual['sensor_units'] if manual else {}
    unit = (units.get(str(machine)) or {}).get(sensor) or (units.get('*') or {}).get(sensor)
    return dict(unit) if unit else {'scale': 1.0, 'offset': 0.0}


def _calibrated(value, unit):
    """Apply the slot's shared scale/offset; scale 0 is treated as 1."""
    scale = float(unit.get('scale', 1)) or 1.0
    return (float(value) - float(unit.get('offset', 0))) / scale


def _level(sensor, value, limit):
    if sensor == 'fan_rpm':
        if value <= 0.5 * limit:
            return 'below_limit'
        return 'normal'
    return 'above_limit' if value > limit else 'normal'


def _latest_run_revision(db, machine):
    run = db.execute("SELECT * FROM machine_run WHERE json_extract(body,'$.machine')=? "
                     "ORDER BY rowid DESC LIMIT 1", (str(machine),)).fetchone()
    if not run:
        return None, None
    body = json.loads(run['body'])
    revision = db.execute("SELECT * FROM machine_run_revision "
                          "WHERE json_extract(body,'$.run')=? ORDER BY rowid DESC LIMIT 1",
                          (str(body.get('id')),)).fetchone()
    return run, (json.loads(revision['body']) if revision else None)


def _phase_for(revision, at):
    if not revision or not at:
        return None
    return production.phase_at(revision.get('events', []), at)


def _interpret(db, manual):
    """Rebuild derived sensor contexts from raw alerts and latest run evidence.

    Derived rows may be revised when a legitimate late machine event arrives,
    so the head row is updated directly while ``sensor_alert`` stays untouched.
    """
    alerts = [dict(row) for row in db.execute('SELECT * FROM sensor_alert ORDER BY rowid')]
    slots = {}
    for alert in alerts:
        body = json.loads(alert['body'])
        machine = str(body.get('machine', '*'))
        at = body.get('at')
        slot_key = f"{machine}@{at}"
        slot = slots.setdefault(slot_key, {'calibration': {}, 'run': None,
                                           'revision': None, 'phase': None})
        if not slot['calibration']:
            for sensor in PROBES:
                slot['calibration'][sensor] = _unit_for(manual, machine, sensor)
            run, revision = _latest_run_revision(db, machine)
            slot['run'] = str(json.loads(run['body']).get('id')) if run else None
            slot['revision'] = revision.get('revision') if revision else None
            slot['phase'] = _phase_for(revision, at)
        sensor = str(body.get('sensor'))
        unit = slot['calibration'].get(sensor, {'scale': 1.0, 'offset': 0.0})
        value = _calibrated(body.get('value'), unit)
        # The threshold is a specification in engineering units; the
        # calibration error belongs to the reading, not to the limit.  The
        # same unit is applied to every probe in this machine/slot, so an
        # operator cannot pick a favourable zero point per alarm.
        limit = float(body.get('limit'))
        phase = slot['phase']
        parked = bool(phase and phase.get('kind') == '停机余热')
        # A high temperature sample taken while parked is residual heat, not a
        # running anomaly.  Fan/current readings taken with no load constrain
        # nothing: there is no running load for them to describe.
        residual = bool(parked and sensor == 'temperature')
        level = _level(sensor, value, limit)
        if parked and sensor != 'temperature':
            level = 'parked_ignored'
        context = {'alert': alert['id'], 'slot': slot_key, 'machine': machine,
                   'sensor': sensor, 'at': at,
                   'raw_value': float(body.get('value')), 'raw_limit': float(body.get('limit')),
                   'calibrated_value': value, 'calibrated_limit': limit,
                   'calibration': unit, 'calibration_shared': True,
                   'level': level,
                   'phase': phase, 'phase_known': phase is not None,
                   'residual': residual, 'run': slot['run'],
                   'run_revision': slot['revision']}
        if phase is None:
            status, reason = '待解释', 'run_phase_missing'
        elif residual:
            status, reason = '已解释', 'residual_heat'
        else:
            status, reason = '已解释', 'running_phase'
        encoded = json.dumps(context, sort_keys=True, ensure_ascii=False)
        db.execute('INSERT INTO sensor_context(id,source_id,version,body,status,reason) '
                   'VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                   'source_id=excluded.source_id,version=excluded.version,body=excluded.body,'
                   'status=excluded.status,reason=excluded.reason',
                   (alert['id'], alert['source_id'], alert['version'], encoded, status, reason))
    return len(alerts)


def import_sensors(path, file):
    """Keep every raw alarm row; derive phase/calibration context beside it."""
    source, payload = capture(path, 'desktop_machine_sensors', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                # Original rule: one row, one value-vs-limit verdict.  It is
                # evidence only and does not by itself decide a fault.
                if float(row['value']) > float(row['limit']):
                    status, reason = '待检修', 'sensor_limit_exceeded'
                else:
                    status, reason = '正常', 'sensor_in_range'
                save(db, 'sensor_alert', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '机台传感器', key, status, reason, source))
            _, manual = _manual_or_none(db)
            _interpret(db, manual)
            _recompute(db, source, reason='sensor_import')
        return results
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Manual and commission
# ---------------------------------------------------------------------------

def _manual_or_none(db):
    try:
        return manual_mod.latest(db)
    except ValueError:
        return None, None


def import_manual(path, file):
    """Capture a versioned diagnostic manual and re-plan with it.

    A revised manual is a new version; earlier diagnosis and plan revisions
    stay on file so the change of basis is auditable.
    """
    result = manual_mod.import_manual(path, file)
    db = connect(path)
    try:
        with db:
            _recompute(db, None, reason='manual_import')
        return result
    finally:
        db.close()


def delegate(path, file):
    """Fix the delegated goal before inspection begins."""
    source, payload = capture(path, 'desktop_maintenance_commission', file)
    db = connect(path)
    results = []
    try:
        with db:
            tested = db.execute("SELECT 1 FROM test_execution WHERE status IN ('已登记','失败','待核') "
                                "LIMIT 1").fetchone()
            fixed = db.execute("SELECT * FROM maintenance_commission WHERE status='已委托' "
                               "ORDER BY rowid DESC LIMIT 1").fetchone()
            fixed_kind = json.loads(fixed['body']).get('commission') if fixed else None
            for row in payload.get('rows', []):
                key = str(row['id'])
                commission = str(row.get('commission', ''))
                if commission not in manual_mod.COMMISSIONS:
                    status, reason = '待核', 'unknown_commission'
                elif not row.get('authority'):
                    status, reason = '待核', 'authority_missing'
                elif tested:
                    status, reason = '待核', 'commission_locked_after_test'
                elif fixed_kind and commission != fixed_kind:
                    # Both delegated goals are fixed before inspection; a
                    # later commission may not switch the goal because the
                    # evidence now looks convenient for the other one.
                    status, reason = '待核', 'commission_already_fixed'
                else:
                    status, reason = '已委托', 'commission_fixed'
                save(db, 'maintenance_commission', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '检修委托', key, status, reason, source))
            if any(x['status'] == '已委托' for x in results):
                _recompute(db, source, reason='commission')
        return results
    finally:
        db.close()


def _latest_commission(db):
    row = db.execute("SELECT * FROM maintenance_commission WHERE status='已委托' "
                     "ORDER BY rowid DESC LIMIT 1").fetchone()
    return (row, json.loads(row['body'])) if row else (None, None)


# ---------------------------------------------------------------------------
# Evidence assembly
# ---------------------------------------------------------------------------

def _samples(db):
    samples = []
    for row in db.execute("SELECT * FROM sensor_context WHERE status='已解释' ORDER BY rowid"):
        body = json.loads(row['body'])
        if body.get('sensor') in PROBES and body.get('phase_known') \
                and body.get('level') != 'parked_ignored':
            sample = dict(body)
            sample['probe'] = body['sensor']
            samples.append(sample)
    return samples


def _test_results(db):
    results = []
    for row in db.execute("SELECT * FROM test_execution WHERE status='已登记' ORDER BY rowid"):
        body = json.loads(row['body'])
        results.append({'test': str(body['test']), 'result': str(body['result']),
                        'execution': row['id'], 'at': body.get('at')})
    return results


def _pool(manual, samples, results):
    return [tuple(h) for h in manual_mod.manual_candidates(manual)
            if manual_mod.compatible(manual, h, samples, results)]


def _machine_state(db):
    """Current machine state from the latest legitimate run evidence.

    A run whose latest event is ``interrupted`` is stopped but not isolated;
    the operator still has to register the isolation step before any controlled
    test.  ``completed``/``queued`` also count as stopped.
    """
    row = db.execute('SELECT * FROM machine_run ORDER BY rowid DESC LIMIT 1').fetchone()
    if not row:
        return '停机隔离'
    events = json.loads(row['body']).get('events', [])
    if not events:
        return '停机隔离'
    last = events[-1].get('state')
    if last in ('running', 'resumed'):
        return '运行中'
    if last in ('interrupted', 'completed'):
        return '已停机待隔离'
    return '停机隔离'


def _plan_state(db, manual):
    """Physical state for the next test.

    Every already executed test contributes its heat; a failed test still
    heated the machine and must appear in later preconditions.  Replaying the
    same execution file is idempotent because the row is keyed by execution id.
    Reaching the next test requires an explicit isolation record: a run merely
    stopped by an interruption is not yet isolated.
    """
    phase = 0.0
    used = 0.0
    completed = []
    for row in db.execute('SELECT * FROM test_execution ORDER BY rowid'):
        body = json.loads(row['body'])
        test = manual['tests'].get(str(body.get('test')))
        if not test:
            continue
        if row['status'] == '已登记' and body.get('test') not in completed:
            completed.append(str(body.get('test')))
        if test.get('produces_heat'):
            phase += float(body.get('heat_minutes', manual_mod.test_heat(manual, test)))
        used += float(test['minutes'])
    observed = _machine_state(db)
    isolated_record = db.execute(
        "SELECT 1 FROM test_execution WHERE status IN ('已登记','失败') LIMIT 1").fetchone()
    # A controlled test is always preceded by stopping and isolating the
    # machine.  Until isolation is recorded, the observed state may still be
    # 运行中; the plan's first precondition step is that isolation.
    return {'machine_state': '停机隔离',
            'isolated': bool(isolated_record),
            'observed_machine_state': observed,
            'isolation_required': not bool(isolated_record),
            'phase_minutes': phase, 'available_minutes': 10 ** 9 - used,
            'completed_tests': tuple(completed)}


# ---------------------------------------------------------------------------
# Diagnosis and plan revisions
# ---------------------------------------------------------------------------

def _recompute(db, source, reason='recompute'):
    manual_row, manual = _manual_or_none(db)
    if manual is None:
        return None
    _interpret(db, manual)
    commission_row, commission_body = _latest_commission(db)
    if not commission_row:
        return None
    commission = str(commission_body['commission'])
    samples = _samples(db)
    results = _test_results(db)
    all_candidates = [tuple(h) for h in manual_mod.manual_candidates(manual)]
    pool = _pool(manual, samples, results)
    revision = db.execute("SELECT count(*) FROM maintenance_diagnosis WHERE "
                          "json_extract(body,'$.commission')=?",
                          (commission_row['id'],)).fetchone()[0] + 1
    diagnosis_id = f"{commission_row['id']}:r{revision}"
    if not pool:
        diagnosis = {'id': diagnosis_id, 'commission': commission_row['id'],
                     'goal_met': False,
                     'commission_kind': commission, 'revision': revision,
                     'manual_version': manual['version'],
                     'candidates': [], 'eliminated': [],
                     'retained_uncertainty': [],
                     'state': '无法解释', 'tests_used': [r['execution'] for r in results],
                     'note': 'evidence_contradicts_manual_or_test_table',
                     'authorises_release': False}
        save(db, 'maintenance_diagnosis', diagnosis_id, source, 'v1', diagnosis,
             '待核', 'no_compatible_candidate')
        decision = decide(db, '机台诊断', diagnosis_id, '无法解释', f'diagnosis_{reason}', source)
        return diagnosis, decision, []
    eliminated = []
    for h in all_candidates:
        if h not in pool:
            eliminated.extend(manual_mod.elimination_evidence(manual, h, samples, results, all_candidates))
    # de-duplicate while keeping first witness order deterministic
    seen_elim = set()
    deduped = []
    for item in eliminated:
        key = tuple(item['candidate'])
        if key in seen_elim:
            continue
        seen_elim.add(key)
        deduped.append(item)
    eliminated = deduped
    state = _plan_state(db, manual)
    trees = manual_mod.search(manual, commission, pool, state)
    goal_met = manual_mod.goal_met(manual, commission, pool)
    diagnosis = {
        'id': diagnosis_id, 'commission': commission_row['id'],
        'commission_kind': commission, 'revision': revision,
        'manual_version': manual['version'],
        'candidates': [list(h) for h in pool],
        'eliminated': eliminated,
        'evidence': {'sample_count': len(samples), 'tests': results,
                     'phase_state': state},
        'goal_met': goal_met,
        'state': '有建议树' if trees else '委托不可达',
        # Remaining candidates the evidence cannot yet distinguish.  For a
        # repair commission this may be non-empty even after the goal is met:
        # the module is determined while the root cause is not.
        'retained_uncertainty': [list(h) for h in pool] if len(pool) > 1 else [],
        'machine_structure': manual['structure'],
        'authorises_release': False,
        'note': 'diagnosis_is_evidence_not_repair_proof',
    }
    if trees:
        diagnosis['plan_cost'] = {'worst_minutes': trees[0]['worst_minutes'],
                                  'risk': trees[0]['risk'],
                                  'tied_variants': len(trees)}
    previous = db.execute('SELECT * FROM maintenance_diagnosis ORDER BY rowid DESC LIMIT 1').fetchone()
    if previous:
        prev_body = json.loads(previous['body'])
        comparable = {k: v for k, v in prev_body.items() if k not in ('id', 'revision', 'source')}
        current = {k: v for k, v in diagnosis.items() if k not in ('id', 'revision', 'source')}
        if comparable == current:
            return prev_body, None, []
    save(db, 'maintenance_diagnosis', diagnosis_id, source, 'v1', diagnosis,
         '已记录' if trees else '待核', f'diagnosis_{reason}')
    decision = decide(db, '机台诊断', diagnosis_id, diagnosis['state'],
                      f'diagnosis_{reason}', source)
    plans_written = []
    if trees:
        plan_revision = db.execute("SELECT count(*) FROM maintenance_plan WHERE "
                                   "json_extract(body,'$.commission')=?",
                                   (commission_row['id'],)).fetchone()[0] + 1
        for index, tree in enumerate(trees, 1):
            plan = {'id': f"{commission_row['id']}:r{plan_revision}:v{index}",
                    'commission': commission_row['id'], 'commission_kind': commission,
                    'revision': plan_revision, 'variant': index,
                    'variant_count': len(trees),
                    'candidates': [list(h) for h in pool],
                    'tree': tree,
                    'worst_minutes': tree['worst_minutes'], 'risk': tree['risk'],
                    'next_test': tree.get('test'),
                    'next_precondition': tree.get('precondition'),
                    'goal': commission,
                    'authorises_release': False,
                    'note': 'advice_only_not_release_or_repair_proof'}
            save(db, 'maintenance_plan',
                 f"{commission_row['id']}:r{plan_revision}:v{index}", source, 'v1', plan,
                 '建议', 'adaptive_test_plan')
            plans_written.append(plan)
        decide(db, '检修建议树', f"{commission_row['id']}:r{plan_revision}",
               '已生成', f'{len(trees)}_minimum_cost_variants', source)
    return diagnosis, decision, plans_written


def after_run_revision(db, source, revision):
    """Late machine evidence changes residual-heat reading and re-plans."""
    if not revision or not revision.get('valid'):
        return None
    return _recompute(db, source, reason='late_run_revision')


def diagnose(path, file=None):
    """Recompute diagnosis and plan from currently captured evidence."""
    db = connect(path)
    try:
        with db:
            return _recompute(db, None, reason='manual')
    finally:
        db.close()


def latest_diagnosis(db):
    row = db.execute('SELECT * FROM maintenance_diagnosis ORDER BY rowid DESC LIMIT 1').fetchone()
    return (row, json.loads(row['body'])) if row else (None, None)


def plans_for(db, commission):
    rows = db.execute("SELECT * FROM maintenance_plan WHERE "
                      "json_extract(body,'$.commission')=? ORDER BY rowid", (str(commission),))
    return [json.loads(row['body']) for row in rows]


# ---------------------------------------------------------------------------
# Controlled test execution
# ---------------------------------------------------------------------------

def _effective_phase(manual, row, state):
    """Heat at the moment the test starts, after any recorded cooling wait."""
    cooling = float(row.get('cooldown_minutes', 0) or 0)
    rate = float(manual['structure'].get('cooling_rate', 1.0) or 0)
    return max(0.0, float(state.get('phase_minutes', 0)) - cooling * rate)


def _phase_reason(manual, test, row, state):
    effective = _effective_phase(manual, row, state)
    heat = manual_mod.test_heat(manual, test)
    if effective + heat < test['min_phase_minutes']:
        return test.get('min_phase_reason', 'phase_too_cool')
    if effective + heat > test['max_phase_minutes']:
        return 'phase_too_hot'
    return None


def _phase_admissible(manual, test, row, state):
    return _phase_reason(manual, test, row, state) is None


def execute_test(path, file):
    """Record what a controlled test actually did and produced.

    Suggested plans are not edited.  The execution is appended; the diagnosis
    and plan for the branch the result actually selects are appended as new
    revisions, while plans for branches not taken stay in the ledger.
    """
    source, payload = capture(path, 'desktop_test_execution', file)
    db = connect(path)
    results = []
    try:
        with db:
            _, manual = _manual_or_none(db)
            commission_row, _ = _latest_commission(db)
            for row in payload.get('rows', []):
                key = str(row['id'])
                test = manual['tests'].get(str(row.get('test'))) if manual else None
                if not commission_row:
                    status, reason = '待核', 'commission_missing'
                elif not test:
                    status, reason = '待核', 'unknown_test'
                elif str(row.get('machine_state')) != test['requires']:
                    status, reason = '待核', ('machine_not_isolated'
                                              if test['requires'] == '停机隔离'
                                              else 'machine_not_running')
                elif test['requires'] == '停机隔离' and not row.get('isolated_steps'):
                    status, reason = '待核', 'isolation_steps_missing'
                elif str(row.get('source', '')) not in (('受控试验', '停机隔离')
                                                         if test['requires'] == '停机隔离'
                                                         else ('受控试验', '运行中')):
                    status, reason = '待核', 'result_source_missing'
                elif not row.get('at'):
                    status, reason = '待核', 'executed_at_missing'
                elif any(dep not in _plan_state(db, manual)['completed_tests']
                         for dep in test.get('depends_on', [])):
                    status, reason = '待核', 'dependency_not_run'
                elif not _phase_admissible(manual, test, row, _plan_state(db, manual)):
                    status, reason = '待核', _phase_reason(manual, test, row, _plan_state(db, manual))
                elif str(row.get('result', '')) not in test['outcomes'] and not row.get('failure'):
                    status, reason = '待核', 'unexpected_result'
                elif row.get('failure'):
                    status, reason = '失败', 'test_failed'
                else:
                    status, reason = '已登记', 'test_recorded'
                body = dict(row)
                body['test'] = str(row.get('test', ''))
                body.setdefault('heat_minutes',
                                test['heat_minutes'] if test and test.get('produces_heat') else 0)
                body['test_duration_minutes'] = test['minutes'] if test else 0
                body['machine_state'] = str(row.get('machine_state'))
                body['isolated_steps'] = list(row.get('isolated_steps') or [])
                body['source'] = str(row.get('source', ''))
                save(db, 'test_execution', key, source, payload['version'], body, status, reason)
                if status == '待核':
                    issue(db, source, 'test_not_applied', f'{key}:{reason}')
                results.append(decide(db, '受控试验', key, status, reason, source))
            _recompute(db, source, reason='test_execution')
        return results
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Release receipt (unchanged behaviour; diagnosis never grants it)
# ---------------------------------------------------------------------------

def release(path, file):
    source, payload = capture(path, 'desktop_maintenance_release', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                alert = db.execute('SELECT * FROM sensor_alert WHERE id=?',
                                   (str(row['alert']),)).fetchone()
                if not alert:
                    status, reason = '待核', 'alert_missing'
                elif alert['status'] == '待检修' and not row.get('technician'):
                    status, reason = '待核', 'technician_missing'
                else:
                    status, reason = '可复机', 'maintenance_reviewed'
                save(db, 'maintenance_release', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '机台复机', key, status, reason, source))
        return results
    finally:
        db.close()
