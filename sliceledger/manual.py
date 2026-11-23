"""Versioned offline diagnostic manual for the temperature / feed cluster.

The manual is an immutable local JSON artefact recording device structure,
probe placement, single faults and pre-approved dual-fault combinations, each
fault's propagation delay and residual-heat story, the repair action bound to
each fault, and the controlled tests with their permit table.

A hypothesis is a frozenset of fault IDs.  The engine only ever considers
single faults and combinations written in the manual.  Test results are
applied as a permit table: a result is compatible with a hypothesis only when
the hypothesis contains a member the result is allowed to indicate.
"""
import json
import math
from datetime import datetime

from .ingest import capture
from .store import connect, save, decide

PROBES = ('temperature', 'fan_rpm', 'current')
EFFECTS = ('high', 'low', 'same')
EPS = 1e-9
COMMISSIONS = ('repair', 'root_cause')


class ManualError(ValueError):
    """A structurally invalid diagnostic manual."""


def _fid(value):
    return str(value)


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _signature(raw):
    signature = {}
    for probe in PROBES:
        item = raw.get(probe) or {}
        effect = item.get('effect', 'same')
        if effect not in EFFECTS:
            raise ManualError('invalid_effect')
        delay = _float(item.get('delay_minutes', 0))
        if delay < 0:
            raise ManualError('invalid_delay')
        residual = item.get('residual_effect', 'same')
        if residual not in EFFECTS:
            raise ManualError('invalid_residual_effect')
        signature[probe] = {'effect': effect, 'delay_minutes': delay,
                            'residual_effect': residual}
    return signature


def _test(raw):
    outcomes = {}
    for key, members in (raw.get('outcomes') or {}).items():
        values = [str(x) for x in members]
        if '*' in values:
            outcomes[str(key)] = ['*']
        else:
            outcomes[str(key)] = values
    return {
        'id': str(raw['id']),
        'minutes': _float(raw.get('minutes', 0)),
        'heat_minutes': _float(raw.get('heat_minutes', raw.get('minutes', 0))),
        'requires': str(raw.get('requires', '停机隔离')),
        'depends_on': [str(x) for x in (raw.get('depends_on') or [])],
        'min_phase_minutes': _float(raw.get('min_phase_minutes', 0)),
        'max_phase_minutes': _float(raw.get('max_phase_minutes', 10 ** 9)),
        'min_phase_reason': str(raw.get('min_phase_reason',
                                        'phase_too_cool')),
        'produces_heat': bool(raw.get('produces_heat', False)),
        'disposition': str(raw.get('disposition', '未判定')),
        'outcomes': outcomes,
        'risk': _float(raw.get('risk', 0)),
        'description': str(raw.get('description', '')),
    }


def parse_manual(payload):
    """Validate and normalise a manual payload (no database access)."""
    if not isinstance(payload, dict):
        raise ManualError('manual_shape')
    version = payload.get('version')
    if not isinstance(version, str) or not version:
        raise ManualError('version_required')
    structure = payload.get('structure') or {}
    modules = structure.get('modules')
    if not isinstance(modules, list) or not modules:
        raise ManualError('structure_missing')
    module_names = set()
    for module in modules:
        if not isinstance(module, dict) or not module.get('id'):
            raise ManualError('module_invalid')
        module_names.add(str(module['id']))
    placements = []
    for probe in payload.get('probes') or []:
        if str(probe.get('sensor')) not in PROBES:
            raise ManualError('unknown_probe')
        placements.append({'id': str(probe.get('id', probe['sensor'])),
                           'sensor': str(probe['sensor']),
                           'module': str(probe.get('module', 'platform')),
                           'position': str(probe.get('position', ''))})
    if not placements:
        raise ManualError('probes_missing')
    # Readings taken in the same slot share one calibration error: a zero
    # point may not be chosen separately for temperature, fan speed and
    # current.  Machine-wide units are expanded to every probe; conflicting
    # per-probe units are rejected rather than silently preferred.
    units = {}
    for unit in payload.get('sensor_units') or []:
        sensor = str(unit.get('sensor', '*'))
        if sensor != '*' and sensor not in PROBES:
            raise ManualError('unknown_probe')
        machine = str(unit.get('machine', '*'))
        entry = {'scale': _float(unit.get('scale', 1), 1),
                 'offset': _float(unit.get('offset', 0))}
        slot = units.setdefault(machine, {})
        targets = list(PROBES) if sensor == '*' else [sensor]
        for probe in targets:
            if probe in slot and slot[probe] != entry:
                raise ManualError('calibration_not_shared')
            slot[probe] = entry
    for machine, slot in units.items():
        values = {(v['scale'], v['offset']) for v in slot.values()}
        if len(values) > 1:
            raise ManualError('calibration_not_shared')
    faults = {}
    for item in payload.get('faults') or []:
        fault_id = _fid(item['id'])
        if fault_id in faults:
            raise ManualError('duplicate_fault')
        action = item.get('action') or {}
        faults[fault_id] = {
            'id': fault_id,
            'component': str(item.get('component', '')),
            'module': str(item.get('module', '')),
            'signature': _signature(item.get('signature') or {}),
            'action': {'action': str(action.get('action', 'observe')),
                       'module': str(action.get('module', item.get('module', 'none')))},
        }
    single = sorted(faults)
    combinations = []
    combination_actions = {}
    for combo in payload.get('combinations') or []:
        if isinstance(combo, dict):
            members = sorted({_fid(x) for x in combo.get('members', combo.get('faults', []))})
            action = combo.get('action')
        else:
            members = sorted({_fid(x) for x in combo})
            action = None
        if len(members) < 2 or not set(members) <= set(single):
            # A "combination" of one fault (or of unknown faults) is not an
            # approved dual-fault basis.
            raise ManualError('unknown_combination_member')
        if members in combinations:
            raise ManualError('duplicate_combination')
        combinations.append(members)
        if action:
            combination_actions['+'.join(members)] = {
                'action': str(action.get('action', 'replace')),
                'module': str(action.get('module', '')),}
    for key, action in combination_actions.items():
        for module in action['module'].split('+'):
            if module and module not in module_names and module != 'none':
                raise ManualError('unknown_action_module')
    tests = {}
    for item in payload.get('tests') or []:
        test = _test(item)
        if test['id'] in tests:
            raise ManualError('duplicate_test')
        for members in test['outcomes'].values():
            for member in members:
                if member != '*' and member not in faults:
                    raise ManualError('unknown_test_member')
        tests[test['id']] = test
    trust = payload.get('test_trust') or {'fan_drive_test': ['停机隔离'],
                                          'signal_test': ['停机隔离', '运行中']}
    return {
        'version': version,
        'structure': {'modules': [{'id': str(m['id']),
                                   'repair_minutes': _float(m.get('repair_minutes',
                                                                  m.get('minutes', 0))),
                                   'description': str(m.get('description', ''))}
                                  for m in modules],
                      'description': str(structure.get('description', '')),
                      'cooling_rate': _float(structure.get('cooling_rate', 1.0), 1.0)},
        'probes': placements,
        'sensor_units': units,
        'faults': faults,
        'single_faults': single,
        'combinations': combinations,
        'combination_actions': combination_actions,
        'approved': [[f] for f in single] + [list(x) for x in combinations],
        'tests': tests,
        'test_trust': {str(k): [str(x) for x in v] for k, v in trust.items()},
        'commissions': {str(k): str(v) for k, v in (payload.get('commissions') or {}).items()},
    }


def manual_candidates(manual):
    return [tuple(sorted(x)) for x in manual['approved']]


def import_manual(path, file):
    source, payload = capture(path, 'desktop_diagnostic_manual', file)
    manual = parse_manual(payload)
    db = connect(path)
    try:
        with db:
            save(db, 'diagnostic_manual', manual['version'], source, manual['version'],
                 manual, '已采录', 'diagnostic_manual')
            return decide(db, '诊断手册', manual['version'], '已采录', 'diagnostic_manual', source)
    finally:
        db.close()


def latest(db):
    row = db.execute('SELECT * FROM diagnostic_manual ORDER BY rowid DESC LIMIT 1').fetchone()
    if not row:
        raise ValueError('manual_missing')
    manual = json.loads(row['body'])
    return row, manual


# ---------------------------------------------------------------------------
# Sample compatibility
# ---------------------------------------------------------------------------

def _minutes(a, b):
    return (datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds() / 60.0


def _phase(sample):
    return sample.get('phase') or {'kind': '运行中', 'from': sample['at'], 'to': sample['at']}


def _out_of_limit(level):
    level = str(level)
    return level.startswith('above') or level.startswith('below')


def _effect_matches(effect, level):
    """Could a fault signature with ``effect`` produce ``level``?

    Out-of-limit readings are directional.  ``same`` reproduces only normal
    samples.  A low fan reading is admitted by a low fault; a high fan by a
    high fault.  Normal samples are compatible with anything (they simply do
    not eliminate the fault).
    """
    if not _out_of_limit(level):
        return True
    if level.startswith('above'):
        return effect == 'high'
    return effect == 'low'


def fault_explains(manual, fault_id, sample):
    """Whether one fault can account for one reading.

    Out-of-limit readings are directional.  Normal readings do not eliminate
    anything.  A residual high taken while parked is only explained by a fault
    whose manual entry says its heat persists.

    The probe's propagation delay is evidence about *when* the fault started:
    the delayed rise must land inside the observed phase.  Faults starting in
    the same phase therefore share the same timing class, which is the
    realistic situation for two faults present at once.
    """
    signature = manual['faults'][fault_id]['signature']
    probe = sample.get('probe', sample.get('sensor'))
    level = str(sample.get('level', 'normal'))
    if sample.get('residual'):
        # Residual heat is a property of the fault: only a fault whose manual
        # entry says its heat persists explains a converged parked high.
        return signature[probe]['residual_effect'] == 'high'
    effect = signature[probe]['effect']
    if not _effect_matches(effect, level):
        return False
    if not _out_of_limit(level):
        return True
    phase = _phase(sample)
    if phase['kind'] == '停机余热' and effect == 'high':
        return True
    # Propagation delay is evidence about *when* the rise starts: the sample
    # must be taken at or after the delayed rise.  Faults that start in the
    # same phase therefore share a timing class, which is the realistic case
    # for two faults present at once.
    observed = _minutes(sample['at'], phase['from'])
    delay = signature[probe]['delay_minutes']
    return observed + EPS >= delay


def _explains_all(manual, hypothesis, samples, results):
    positive = False
    for sample in samples:
        if not any(fault_explains(manual, f, sample) for f in hypothesis):
            return False
        if _out_of_limit(sample.get('level', 'normal')):
            positive = True
    if results:
        positive = True
    return positive and test_compatible(manual, hypothesis, results)


def compatible(manual, hypothesis, samples, results=()):
    """Whether ``hypothesis`` explains all samples and recorded test results.

    Every out-of-limit reading must be accounted for.  In addition a
    hypothesis needs at least one positive witness (an out-of-limit reading
    or a recorded test result); a fault set supported only by normal readings
    is not a candidate at all.  Multi-fault sets are limited to the manual's
    pre-approved combination list, so no explanation is invented on the fly.
    """
    return _explains_all(manual, hypothesis, samples, results)


def outcome_permits(allowed, hypothesis):
    """Does a test outcome permit exactly this fault set?

    ``*`` is non-diagnostic.  A bare token ``F1`` names only the single-fault
    hypothesis ``{F1}``.  An approved multi-fault explanation must be named
    explicitly (``F1+F3``).  This is what prevents several individually
    feasible single-fault results from being stitched into a multi-fault
    explanation nobody approved.
    """
    if '*' in allowed:
        return True
    target = tuple(sorted(set(hypothesis)))
    for token in allowed:
        members = tuple(sorted(set(str(token).split('+'))))
        if members == target:
            return True
    return False


def test_compatible(manual, hypothesis, results):
    for result in results:
        test = manual['tests'].get(result['test'])
        if test is None:
            return False
        allowed = test['outcomes'].get(str(result['result']))
        if allowed is None:
            return False
        if not outcome_permits(allowed, hypothesis):
            return False
    return True


def elimination_evidence(manual, candidate, samples, results, pool):
    """For one removed candidate, the observation or result that removed it.

    ``pool`` is the approved candidate list; it is accepted for signature
    symmetry but the witness is computed from the candidate itself so a
    removed combination reports its own failing evidence.
    """
    witness = None
    for sample in samples:
        if not any(fault_explains(manual, f, sample) for f in candidate):
            witness = {'kind': 'sample',
                       'probe': sample.get('probe', sample.get('sensor')),
                       'level': sample.get('level'), 'at': sample['at'],
                       'residual': bool(sample.get('residual')),
                       'explanation': 'signature_or_delay_incompatible'}
            break
    if witness is None:
        for result in results:
            if not test_compatible(manual, candidate, [result]):
                witness = {'kind': 'test', 'test': result['test'],
                           'result': result['result']}
                break
    if witness is None:
        witness = {'kind': 'combination', 'detail': 'not_approved'}
    return [{'candidate': list(candidate), 'witness': [witness]}]


def repair_action(manual, hypothesis):
    key = '+'.join(sorted(set(hypothesis)))
    explicit = (manual.get('combination_actions') or {}).get(key)
    if explicit:
        return dict(explicit)
    actions = {(manual['faults'][f]['action']['action'], manual['faults'][f]['action']['module'])
               for f in hypothesis}
    if len(actions) != 1:
        modules = sorted({manual['faults'][f]['action']['module'] for f in hypothesis})
        return {'action': 'mixed', 'module': '+'.join(modules)}
    action, module = sorted(actions)[0]
    return {'action': action, 'module': module}


def module_repair_minutes(manual, module):
    for item in manual['structure']['modules']:
        if item['id'] == module:
            return item['repair_minutes']
    return 0.0


def action_cost_minutes(manual, hypothesis):
    action = repair_action(manual, hypothesis)
    if action['action'] in ('observe', 'none'):
        return 0.0
    return max((module_repair_minutes(manual, m) for m in action['module'].split('+')), default=0.0)


def goal_met(manual, commission, pool):
    """Whether a *candidate set* satisfies the delegated goal.

    ``repair`` needs one repair action *and* one module across the whole
    surviving set.
    ``root_cause`` needs a single approved hypothesis: two faults repaired
    with the same module are still two different root causes, so "replace the
    fan module" must never be reported as "root cause determined".
    """
    if commission not in COMMISSIONS:
        raise ManualError('unknown_commission')
    pool = [tuple(sorted(h)) for h in pool]
    if not pool:
        return False
    if commission == 'repair':
        actions = [repair_action(manual, h) for h in pool]
        return len({(a['action'], a['module']) for a in actions}) == 1
    return len(set(pool)) == 1


# ---------------------------------------------------------------------------
# Adaptive test-tree search
# ---------------------------------------------------------------------------

def test_heat(manual, test):
    """Heat left in the machine by a test; non-heating tests add none."""
    if not test.get('produces_heat'):
        return 0.0
    return _float(test.get('heat_minutes', test['minutes']))


def test_feasible(manual, test, state):
    """Is ``test`` safe and physically valid right now?

    ``state`` keys: ``machine_state`` (停机隔离 / 运行中), ``isolated``,
    ``phase_minutes`` (heat accumulated by earlier tests) and
    ``available_minutes``.  ``phase_minutes`` is the machine's continuous
    heat history; a test that produces heat raises it for the next test.
    """
    required = test['requires']
    if required == '停机隔离':
        if state.get('machine_state') != '停机隔离':
            return False, 'machine_not_isolated'

    elif required == '运行中':
        if state.get('machine_state') != '运行中':
            return False, 'machine_not_running'
    else:
        if state.get('machine_state') not in ('停机隔离', '运行中'):
            return False, 'machine_state_unknown'
    allowed = manual['test_trust'].get(_test_kind(test), ['停机隔离'])
    if required not in allowed:
        return False, 'test_not_trusted'
    for dependency in test.get('depends_on', []):
        if dependency not in state.get('completed_tests', ()):
            return False, 'dependency_not_run'
    if test['minutes'] > _float(state.get('available_minutes', 10 ** 9), 10 ** 9):
        return False, 'insufficient_window'
    phase = _float(state.get('phase_minutes', 0))
    heat = test_heat(manual, test)
    if phase + heat < test['min_phase_minutes']:
        return False, test.get('min_phase_reason', 'phase_too_cool')
    if phase + heat > test['max_phase_minutes']:
        return False, 'phase_too_hot'
    return True, None


def _test_kind(test):
    if 'drive' in test['id'] or 'drive' in test.get('description', ''):
        return 'fan_drive_test'
    return 'signal_test'


def cooldown_minutes(manual, test, state):
    """Wall minutes needed so ``test`` fits its maximum heat precondition.

    Returns ``None`` when no cooling model exists; a zero result means no wait
    is needed.  The wait is derived from the manual's cooling rate and must be
    recorded in the plan so the precondition stays auditable.
    """
    heat = test_heat(manual, test)
    phase = _float(state.get('phase_minutes', 0))
    excess = phase + heat - test['max_phase_minutes']
    if excess <= EPS:
        return 0
    rate = _float(manual['structure'].get('cooling_rate', 1.0), 1.0)
    if rate <= 0:
        return None
    return int(math.ceil(excess / rate - EPS))


def branch_table(manual, test, pool):
    """Split a candidate pool by each permitted diagnostic test outcome.

    A candidate belongs to a branch only when the preset table permits that
    outcome for exactly that fault set (see :func:`outcome_permits`).  Outcomes
    whose permit list is ``*`` and the ``disposition`` outcome are
    non-diagnostic and are not branched on.  There is deliberately no
    catch-all "cannot distinguish" branch: every possible execution result is
    one of the preset outcomes, and a candidate permitted by none of them is
    eliminated whichever listed result occurs.  A result outside the preset
    table is rejected at execution time.
    """
    table = {}
    for outcome, members in test['outcomes'].items():
        if outcome == test['disposition'] or '*' in members:
            continue
        table[outcome] = {h for h in pool if outcome_permits(members, h)}
    if not table:
        return {}
    # Only retain branches whose fault sets are compatible with the *state*
    # after the test; an unrelated state filter is applied by search.
    # Candidates permitted by no listed outcome are eliminated whichever
    # listed result occurs.  A result outside the table is rejected at
    # execution time, so it is not a branch of the plan.
    return {name: sorted(branch) for name, branch in table.items() if branch}


def _cost(meta):
    return (round(meta['worst_minutes'], 6), round(meta['risk'], 6))


def _dedupe_variants(variants):
    seen = set()
    result = []
    for variant in variants:
        key = json.dumps(variant, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        result.append(variant)
    return result


def search(manual, commission, pool, state=None, depth=0, limit=8, used=()):
    """Enumerate every minimum-cost adaptive tree for ``pool``.

    Cost is ``(worst shutdown minutes, worst test risk)``.  A tree is only
    accepted when every branch can be driven to the delegated goal; the
    search never stops at a partially resolved branch.  All trees attaining
    the minimum pair are returned so equal-cost bases survive side by side.
    """
    state = dict(state or {})
    state.setdefault('machine_state', '停机隔离')
    state.setdefault('isolated', True)
    state.setdefault('phase_minutes', 0.0)
    state.setdefault('available_minutes', 10 ** 9)
    state.setdefault('completed_tests', ())
    pool = [tuple(sorted(h)) for h in sorted({tuple(sorted(h)) for h in pool})]
    if not pool:
        return []
    if goal_met(manual, commission, pool):
        return [{'kind': 'leaf',
                 'candidates': [list(h) for h in pool],
                 'worst_minutes': max((action_cost_minutes(manual, h) for h in pool), default=0.0),
                 'risk': 0.0,
                 'repair': sorted({repair_action(manual, h)['module'] for h in pool})}]
    if depth > limit:
        return []
    # A test bearing no information about this pool cannot improve any metric;
    # it must not be preferred (or kept as a tied base) merely because it is
    # cheap.  Removing it never increases the honest worst-case cost.
    pool_set = set(pool)
    informative = {}
    for test_id, test in manual['tests'].items():
        table = branch_table(manual, test, pool)
        if len(table) >= 2 and all(set(branch) != pool_set for branch in table.values()):
            informative[test_id] = table
    if not informative:
        return []
    best_cost = None
    best_variants = []
    for test_id in sorted(informative):
        test = manual['tests'][test_id]
        if test_id in used:
            continue
        feasible, why = test_feasible(manual, test, state)
        wait = 0
        if not feasible:
            if why != 'phase_too_hot':
                continue
            wait = cooldown_minutes(manual, test, state)
            if wait is None:
                continue
        table = informative[test_id]
        phase_after_wait = _float(state['phase_minutes']) - \
            wait * _float(manual['structure'].get('cooling_rate', 1.0), 1.0)
        heat = test_heat(manual, test)
        branch_children = []
        branch_ok = True
        for outcome in sorted(table):
            child_state = dict(state)
            child_state['phase_minutes'] = phase_after_wait + heat
            child_state['completed_tests'] = tuple(state.get('completed_tests', ())) + (test_id,)
            child_state['available_minutes'] = _float(state['available_minutes']) - wait - test['minutes']
            if child_state['available_minutes'] < 0:
                branch_ok = False
                break
            children = search(manual, commission, table[outcome], child_state,
                              depth + 1, limit, used + (test_id,))
            children = [c for c in children if set(tuple(sorted(h)) for h in c.get('candidates', []))
                        <= set(tuple(sorted(h)) for h in table[outcome])] or children
            if not children:
                branch_ok = False
                break
            best_child = min(children, key=_cost)
            tied = [c for c in children if _cost(c) == _cost(best_child)]
            branch_children.append({'outcome': outcome,
                                    'candidates': [list(h) for h in table[outcome]],
                                    'variants': tied})
        if not branch_ok:
            continue
        # Cartesian product over tied child variants, capped; keep only
        # variants that attain the branch minima.
        # One tree must present the same next test in every branch of this
        # subtree; mixing different children per branch would describe a plan
        # no single machine follows.  Worst case is taken across branches.
        # Every tied child combination is a distinct basis.
        chosen = [branch['variants'] for branch in branch_children]
        import itertools
        for pick in itertools.product(*chosen):
            meta = {'kind': 'test', 'test': test_id,
                    'minutes': test['minutes'], 'cooldown_minutes': wait,
                    'worst_minutes': wait + test['minutes'] + max(
                        child['worst_minutes'] for child in pick),
                    'risk': test['risk'] + max(child['risk'] for child in pick),
                    'precondition': {
                        'machine_state': state['machine_state'],
                        'isolated': state['isolated'],
                        'phase_minutes': phase_after_wait,
                        'cooldown_minutes': wait,
                        'cooldown_reason': 'phase_too_hot' if wait else None,
                        'min_phase_minutes': test['min_phase_minutes'],
                        'max_phase_minutes': test['max_phase_minutes'],
                        'available_minutes': _float(state['available_minutes']),
                        'test_minutes': test['minutes'],
                        'test_heat_minutes': heat},
                    'branches': [{'outcome': branch['outcome'],
                                  'candidates': branch['candidates'],
                                  'next': child}
                                 for branch, child in zip(branch_children, pick)]}
            cost = _cost(meta)
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_variants = [meta]
            elif cost == best_cost:
                best_variants.append(meta)
    return _dedupe_variants(best_variants)
