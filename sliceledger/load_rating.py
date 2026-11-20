"""Rated working load from cyclic force/displacement evidence.

The rating is a promise about one printed structure (model), not a pass/fail
label on an individual specimen.  The pipeline:

1. Enumerate every force/displacement alignment compatible with the two-clock
   synchronisation pulses and the signed drift envelope.
2. On every compatible alignment, find the first failure cycle under each
   contract's pre-fixed failure definition.  If a one-sample shift flips a
   specimen between elastic recovery and permanent deformation it is listed as
   待校准 (pending calibration); the alignment favourable to the promise is not
   chosen.
3. Aggregate specimens by independent build plate: repeats on one plate share
   nozzle and ambient variation and never count as independent batches.
4. For each candidate load level compute a sourced one-sided upper bound on
   failure risk (Clopper-Pearson beta-binomial over plates; right-censored
   plates are counted conservatively).  Certify only the largest level whose
   bound is below the contract risk limit under every compatible alignment.
5. Corrected/late calibrations re-open affected specimens; evidence under
   different calibration versions is never pooled.  Superseded promises are
   withdrawn with a reason while the issued JSON stays readable.
"""
import json
import math
from pathlib import Path

from .ingest import capture
from .store import body_of, connect, decide

# ---------------------------------------------------------------------------
# Two-clock synchronisation
# ---------------------------------------------------------------------------

def compatible_shifts(specimen):
    """All sample offsets k (force index = displacement index + k) compatible
    with the synchronisation pulses.

    Two clocks are reconciled with two independent bounds:
    ``drift_ppm`` is the signed rate envelope (us of offset change per second)
    and ``offset_tolerance_us`` bounds the absolute inter-clock offset, which
    a rate bound alone cannot express on short traces.
    """
    sync = specimen['sync']
    fp = [float(x) for x in sync['force_pulse_t_ms']]
    dp = [float(x) for x in sync['disp_pulse_t_ms']]
    lo, hi = (float(x) for x in sync['drift_ppm'])
    offset_tol_us = float(sync.get('offset_tolerance_us', 1000.0))
    force_t = [float(x) for x in specimen['channels']['force']['t_ms']]
    disp_channel = specimen['channels'].get('displacement')
    if not disp_channel:
        # Late displacement channel: no alignment path can be evaluated yet.
        return []
    disp_t = [float(x) for x in disp_channel['t_ms']]
    shifts = []
    for k in range(-(len(disp_t) - 1), len(force_t)):
        ok = True
        for a, b in zip(fp, dp):
            force_index = force_t.index(a)
            aligned_index = force_index - k
            if not (0 <= aligned_index < len(disp_t)):
                ok = False
                break
            pulse_offset_us = (b - a) * 1000.0
            aligned_offset_us = (disp_t[aligned_index] - a) * 1000.0
            delta_us = aligned_offset_us - pulse_offset_us
            if abs(delta_us) > offset_tol_us:
                ok = False
                break
            if b > 0.0:
                drift = delta_us / (b / 1000.0)
                if not (lo <= drift <= hi):
                    ok = False
                    break
        if ok:
            shifts.append(k)
    if not shifts and disp_channel:
        raise ValueError('no_compatible_alignment:' + specimen['id'])
    return sorted(set(shifts))


def _aligned_series(specimen, shift, calibration=None):
    """Return paired (force, displacement, cycle, temperature) samples for one
    offset: force sample i pairs with displacement sample i-shift.

    The effective calibration's scale/bias correction is applied here; the raw
    force series in the ledger is never rewritten.
    """
    force = specimen['channels']['force']
    disp = specimen['channels']['displacement']
    scale = float(calibration.get('scale_correction', 1.0)) if calibration else 1.0
    bias = float(calibration.get('bias_n', 0.0)) if calibration else 0.0
    pairs = []
    nf, nd = len(force['t_ms']), len(disp['t_ms'])
    for i in range(nf):
        j = i - shift
        if 0 <= j < nd:
            pairs.append({
                'force_n': float(force['force_n'][i]) * scale + bias,
                'disp_mm': float(disp['disp_mm'][j]),
                'cycle': int(specimen['cycle'][i]),
                'temperature_c': float(specimen['temperature_c'][i]),
            })
    return pairs


def _cycle_features(pairs, zero_band_n=1.0):
    cycles = {}
    for p in pairs:
        c = p['cycle']
        bucket = cycles.setdefault(c, {'force_n': [], 'disp_mm': [], 'temperature_c': []})
        bucket['force_n'].append(p['force_n'])
        bucket['disp_mm'].append(p['disp_mm'])
        bucket['temperature_c'].append(p['temperature_c'])
    out = {}
    for c, b in cycles.items():
        near_zero = [d for f, d in zip(b['force_n'], b['disp_mm'])
                     if abs(f) <= zero_band_n]
        if not near_zero:
            # The calibration's zero band excludes every sample: the unloaded
            # anchor cannot be located, so neither residual deformation nor
            # loop width are confirmable for this cycle.
            out[c] = {
                'peak_force_n': max(b['force_n']),
                'residual_disp_mm': None,
                'hysteresis_width_mm': None,
                'max_temperature_c': max(b['temperature_c']),
                'samples': len(b['force_n']),
            }
            continue
        anchored = [d for d in b['disp_mm'] if d >= min(near_zero)]
        out[c] = {
            'peak_force_n': max(b['force_n']),
            'residual_disp_mm': min(near_zero),
            'hysteresis_width_mm': max(anchored) - min(near_zero),
            'max_temperature_c': max(b['temperature_c']),
            'samples': len(b['force_n']),
        }
    return out

# ---------------------------------------------------------------------------
# First failure cycle on one alignment path
# ---------------------------------------------------------------------------

def first_failure(specimen, pairs, contract, zero_band_n=1.0):
    """Earliest failing cycle under the contract failure definition."""
    features = _cycle_features(pairs, zero_band_n)
    markers = specimen.get('failure_markers', {})
    set_limit = float(contract['permanent_set_mm'])
    hysteresis_growth = float(contract.get('hysteresis_growth_mm', 0.05))
    ordered = sorted(features)
    anchored_cycles = [c for c in ordered
                       if features[c]['hysteresis_width_mm'] is not None]
    baseline = features[anchored_cycles[0]]['hysteresis_width_mm'] \
        if anchored_cycles else 0.0
    for c in ordered:
        f = features[c]
        if f['residual_disp_mm'] is None:
            # No near-zero anchor under this calibration: elastic recovery
            # cannot be confirmed, so the cycle is treated conservatively as
            # an unconfirmed (hysteresis) failure rather than silently passed.
            return c, 'unconfirmed_recovery', f
        if f['residual_disp_mm'] >= set_limit:
            return c, 'permanent_set', f
        if (anchored_cycles and c != anchored_cycles[0]
                and f['hysteresis_width_mm'] is not None
                and f['hysteresis_width_mm'] - baseline >= hysteresis_growth):
            return c, 'hysteresis_growth', f
        if str(c) in markers.get('acoustic_crack_cycle', []) or c in markers.get('acoustic_crack_cycle', []):
            return c, 'acoustic_crack', f
        if str(c) in markers.get('fracture_cycle', []) or c in markers.get('fracture_cycle', []):
            return c, 'fracture', f
        if specimen['stop'].get('reason') == 'fracture' and c == ordered[-1]:
            return c, 'fracture', f
    return None, None, None


def analyse_specimen(specimen, contract, calibration=None):
    """Run the failure definition over every compatible alignment.

    Returns a per-specimen verdict plus the alignment paths it is based on.
    A missing displacement channel is a pending reason, not a pass.
    """
    shifts = compatible_shifts(specimen)
    zero_band_n = float(calibration.get('zero_band_n', 1.0)) if calibration else 1.0
    paths = []
    for shift in shifts:
        pairs = _aligned_series(specimen, shift, calibration)
        cycle, mode, feat = first_failure(specimen, pairs, contract, zero_band_n)
        paths.append({'shift': shift, 'first_failure_cycle': cycle,
                      'failure_mode': mode,
                      'residual_disp_mm': None if feat is None else feat['residual_disp_mm']})
    failure_cycles = {p['first_failure_cycle'] for p in paths}
    failure_modes = {p['failure_mode'] for p in paths}
    set_flip = any(p['failure_mode'] == 'permanent_set' for p in paths) and \
        any(p['failure_mode'] != 'permanent_set' for p in paths)
    observed = int(max(specimen['cycle']))
    censored = specimen['stop']['reason'] in ('right_censored',)
    completed = specimen['stop']['reason'] == 'completed'
    # A one-sample shift that changes elastic recovery vs residual deformation,
    # the first failure cycle or the failure mode cannot be resolved in favour
    # of the promise: the specimen waits for recalibration.
    ambiguous = bool(shifts) and (set_flip or len(failure_cycles) > 1 or
                                  len(failure_modes) > 1)
    pending_reason = None
    if not shifts:
        pending_reason = 'displacement_channel_pending'
    elif ambiguous:
        pending_reason = 'alignment_ambiguous'
    return {
        'specimen_id': specimen['id'],
        'build_plate': specimen['build_plate'],
        'load_level_n': float(specimen['load_level_n']),
        'calibration_id': specimen['calibration_id'],
        'observed_cycles': observed,
        'right_censored': censored,
        'completed': completed,
        'stop_reason': specimen['stop']['reason'],
        'max_temperature_c': max(float(x) for x in specimen['temperature_c']),
        'compatible_shifts': shifts,
        'alignment_paths': paths,
        'ambiguous': ambiguous,
        'pending_reason': pending_reason,
    }

# ---------------------------------------------------------------------------
# Beta-binomial one-sided upper bound (Clopper-Pearson), stdlib only
# ---------------------------------------------------------------------------

def _betacf(a, b, x):
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return h


def _betainc(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def risk_upper_bound(failures, units, confidence=0.9):
    """One-sided Clopper-Pearson upper bound on the failure probability.

    Upper bound = 1 - B^{-1}(1-confidence; units-failures, failures+1),
    with the zero-failure special case 1-(1-confidence)^(1/units).
    """
    if units == 0:
        return None
    if failures == 0:
        return 1.0 - (1.0 - confidence) ** (1.0 / units)
    if failures == units:
        return 1.0
    # Find q = B^{-1}(target; units-failures, failures+1), then return 1-q.
    target = 1.0 - confidence
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if _betainc(units - failures, failures + 1, mid) < target:
            lo = mid
        else:
            hi = mid
    return 1.0 - (lo + hi) / 2.0

# ---------------------------------------------------------------------------
# Calibration lineage
# ---------------------------------------------------------------------------

def calibration_lineage(db):
    """Map calibration_id -> effective (latest non-superseded) id and status."""
    rows = [body_of(r) for r in db.execute('SELECT * FROM load_calibration')]
    by_id = {str(r['calibration_id']): r for r in rows}
    effective, superseded_by = {}, {}
    for cid, row in by_id.items():
        chain = [cid]
        cur = row
        while str(cur.get('supersedes', '')):
            prev = str(cur['supersedes'])
            if prev not in by_id or prev in chain:
                break
            chain.append(prev)
            cur = by_id[prev]
        for old in chain[1:]:
            superseded_by[old] = cid
        effective[cid] = cid
    for old, new in superseded_by.items():
        effective[old] = new
    return effective, superseded_by, by_id

# ---------------------------------------------------------------------------
# Plate-level evidence and rating
# ---------------------------------------------------------------------------

def _load_evidence_file(db, kind, path):
    """Read a local contract/plate JSON and keep its raw bytes in the ledger."""
    source, payload = capture(db, kind, path)
    return source, payload

def _plate_outcome(analysis, contract):
    """Worst defensible verdict of the plate's specimens under one alignment."""
    required = int(contract['required_cycles'])
    needs_high_temp = contract.get('temperature_band_c') is not None
    if needs_high_temp:
        t_lo, t_hi = contract['temperature_band_c']
    else:
        t_lo, t_hi = -math.inf, math.inf
    verdicts = []
    for a in analysis:
        if a['pending_reason']:
            verdicts.append('pending_calibration')
        elif not (t_lo <= a['max_temperature_c'] <= t_hi):
            verdicts.append('temperature_not_covered')
        else:
            path_verdicts = []
            for p in a['alignment_paths']:
                fail = p['first_failure_cycle']
                if fail is not None and fail <= required:
                    path_verdicts.append('failure')
                elif a['right_censored'] and a['observed_cycles'] < required:
                    path_verdicts.append('right_censored')
                elif a['observed_cycles'] < required:
                    path_verdicts.append('insufficient_cycles')
                else:
                    path_verdicts.append('survived')
            verdicts.append('failure' if 'failure' in path_verdicts else path_verdicts[0])
    if 'pending_calibration' in verdicts:
        return 'pending_calibration'
    if 'failure' in verdicts:
        return 'failure'
    if 'temperature_not_covered' in verdicts and len(set(verdicts)) == 1:
        return 'temperature_not_covered'
    if any(v in ('right_censored', 'insufficient_cycles') for v in verdicts):
        return 'right_censored'
    return 'survived'


def rate_model(path, contract_file, plate_file, model, calibration_id=None,
               out_dir='outputs/ratings'):
    db = connect(path)
    try:
        contract_source, contract_doc = _load_evidence_file(db, 'rating_contract', contract_file)
        plate_source, plate_doc = _load_evidence_file(db, 'rating_plate', plate_file)
        contracts = {str(c['contract_id']): c for c in contract_doc['contracts']}
        plate_info = {str(p['build_plate']): p for p in plate_doc['plates']}
        effective, superseded_by, cal_rows = calibration_lineage(db)
        specimens = [body_of(r) for r in db.execute(
            "SELECT * FROM load_specimen WHERE json_extract(body,'$.model')=?", (model,))]
        if not specimens:
            raise ValueError('no_specimen_evidence:' + model)
        with db:
            return _build_rating(db, model, specimens, contracts, plate_info,
                                 effective, superseded_by, cal_rows,
                                 calibration_id, contract_source, plate_source,
                                 contract_doc, plate_doc, Path(out_dir))
    finally:
        db.close()


def _build_rating(db, model, specimens, contracts, plate_info, effective,
                  superseded_by, cal_rows, calibration_id, contract_source,
                  plate_source, contract_doc, plate_doc, out_dir):
    levels = sorted({float(s['load_level_n']) for s in specimens})
    contract_results = {}
    for cid, contract in contracts.items():
        per_level = []
        for level in levels:
            level_specs = [s for s in specimens if float(s['load_level_n']) == level]
            plate_evidence = {}
            for s in level_specs:
                used_cal = effective.get(str(s['calibration_id']), str(s['calibration_id']))
                if calibration_id and used_cal != calibration_id:
                    continue
                analysis = analyse_specimen(s, contract, cal_rows.get(used_cal))
                analysis['effective_calibration_id'] = used_cal
                plate_evidence.setdefault(str(s['build_plate']), []).append(analysis)
            plates = []
            for plate, group in sorted(plate_evidence.items()):
                outcome = _plate_outcome(group, contract)
                plates.append({
                    'build_plate': plate,
                    'plate_batch': plate_info.get(plate, {}).get('plate_batch'),
                    'specimens': [a['specimen_id'] for a in group],
                    'outcome': outcome,
                })
            decided = [p for p in plates if p['outcome'] != 'temperature_not_covered']
            failures = sum(1 for p in decided if p['outcome'] == 'failure')
            # Censored/pending plates cannot be counted as independent passes.
            non_pass = sum(1 for p in decided
                           if p['outcome'] in ('failure', 'right_censored', 'pending_calibration'))
            units = len(decided)
            bound = risk_upper_bound(non_pass, units,
                                     float(contract.get('confidence', 0.9)))
            per_level.append({
                'load_level_n': level,
                'independent_plates': units,
                'plate_outcomes': plates,
                'failure_plates': failures,
                'non_pass_plates': non_pass,
                'failure_risk_upper_bound': bound,
                'risk_limit': float(contract['risk_limit']),
                'meets_limit': bool(bound is not None and bound < float(contract['risk_limit'])),
                'pending_calibration': any(p['outcome'] == 'pending_calibration' for p in decided),
            })
        certified = _certified_levels(per_level)
        follow_ups = _follow_ups(per_level, contract)
        contract_results[cid] = {
            'contract': contract,
            'levels': per_level,
            'certified_load_n': certified[-1] if certified else None,
            'certified_levels_n': certified,
            'follow_up_tests': follow_ups,
        }
    # The effective calibration is the latest head of the lineage that at
    # least one captured specimen resolves to; independent heads never merge.
    used = {effective.get(str(s['calibration_id']), str(s['calibration_id']))
            for s in specimens}
    if calibration_id:
        if calibration_id not in used:
            raise ValueError('calibration_has_no_specimen_evidence:' + calibration_id)
        active_cal = calibration_id
    else:
        active_cal = sorted(used)[-1]
    rating_id = f'{model}:{active_cal}'
    basis = {
        'model': model,
        'calibration_id': active_cal,
        'superseded_calibrations': sorted(superseded_by),
        'contracts': contract_results,
        'candidate_load_levels_n': levels,
        'specimen_count': len(specimens),
        'sources': {
            'contract_file_hash': _hash_file(db, contract_source),
            'plate_file_hash': _hash_file(db, plate_source),
        },
        'statistical_method': {
            'name': 'clopper_pearson_one_sided',
            'independent_unit': 'build_plate',
            'censoring': 'right_censored_plates_counted_as_non_pass',
            'confidence_per_contract': {cid: float(c.get('confidence', 0.9))
                                        for cid, c in contracts.items()},
        },
    }
    status = '已签发'
    reason = 'risk_bound_below_contract_limit'
    if all(r['certified_load_n'] is None for r in contract_results.values()):
        reason = 'insufficient_evidence_no_level_certified'
    _issue_rating(db, rating_id, contract_source, contract_doc['version'],
                  basis, status, reason)
    withdrawals = _withdraw_superseded(db, model, active_cal, contract_source,
                                       contract_doc['version'])
    basis['withdrawals'] = withdrawals
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f'{model}.json'
    doc = {
        'rating_id': rating_id,
        'model': model,
        'status': status,
        'calibration_id': active_cal,
        'basis': basis,
    }
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding='utf-8')
    return out, doc


def _issue_rating(db, rating_id, source_id, version, basis, status, reason):
    """Idempotent, versioned issuance.

    Re-running with unchanged evidence returns the same issuance; a changed
    basis replaces the current rating row while the previous status stays
    readable in the decision log (evidence is never overwritten).
    """
    encoded = json.dumps(basis, sort_keys=True, ensure_ascii=False)
    old = db.execute('SELECT * FROM load_rating WHERE id=?', (rating_id,)).fetchone()
    if old and old['body'] == encoded:
        decide(db, '载荷额定签发', rating_id, old['status'], old['reason'], old['source_id'])
        return
    if old is not None:
        db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                   (source_id, 'rating_revision',
                    f'{rating_id}:prior_status={old["status"]}:{old["reason"]}'))
    db.execute('INSERT INTO load_rating(id,source_id,version,body,status,reason) '
               'VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
               'source_id=excluded.source_id,version=excluded.version,'
               'body=excluded.body,status=excluded.status,reason=excluded.reason',
               (rating_id, source_id, version, encoded, status, reason))
    decide(db, '载荷额定签发', rating_id, status, reason, source_id)


def _certified_levels(per_level):
    """Largest load level whose risk bound is below the contract limit.

    Levels with no plate evidence cannot break the chain, but an evidence-
    bearing level that fails or awaits calibration blocks every higher level:
    a promise at 120 N is impossible while the 60 N evidence is unresolved.
    """
    certified = []
    blocked = False
    for entry in per_level:  # ascending load levels
        if entry['independent_plates'] == 0:
            continue
        if blocked or entry['pending_calibration'] or not entry['meets_limit']:
            blocked = True
            continue
        certified.append(entry['load_level_n'])
    return certified


def _follow_ups(per_level, contract):
    """Minimum extra independent plates/cycles for levels lacking evidence."""
    confidence = float(contract.get('confidence', 0.9))
    limit = float(contract['risk_limit'])
    needed_zero_fail = math.ceil(math.log(1.0 - confidence) / math.log(1.0 - limit))
    items = []
    for entry in per_level:
        if entry['meets_limit'] and not entry['pending_calibration']:
            continue
        decided = [p for p in entry['plate_outcomes']
                   if p['outcome'] != 'temperature_not_covered']
        passed = sum(1 for p in decided if p['outcome'] == 'survived')
        item = {
            'load_level_n': entry['load_level_n'],
            'add_independent_plates': max(0, needed_zero_fail - passed),
            'run_to_cycle': int(contract['required_cycles']),
            'temperature_band_c': contract.get('temperature_band_c'),
        }
        if not decided:
            item['note'] = 'no_plate_covers_contract_temperature_band'
        pending_plates = [p['build_plate'] for p in decided
                          if p['outcome'] == 'pending_calibration']
        if pending_plates:
            item['recalibrate_plates'] = pending_plates
        if any(p['outcome'] == 'right_censored' for p in decided):
            item['note'] = 'right_censored_plates_must_run_to_required_cycles'
        items.append(item)
    return items


def _withdraw_superseded(db, model, active_cal, source_id, version):
    withdrawals = []
    rows = db.execute("SELECT * FROM load_rating WHERE id LIKE ? AND id != ?",
                      (model + ':%', model + ':' + active_cal)).fetchall()
    for row in rows:
        if row['status'] == '已撤回':
            withdrawals.append({'rating_id': row['id'], 'reason': row['reason']})
            continue
        reason = 'calibration_superseded_by:' + active_cal
        db.execute("UPDATE load_rating SET status='已撤回',reason=? WHERE id=?",
                   (reason, row['id']))
        decide(db, '载荷额定签发', row['id'], '已撤回', reason, source_id)
        withdrawals.append({'rating_id': row['id'], 'reason': reason})
    return withdrawals


def _hash_file(db, source_id):
    row = db.execute('SELECT hash FROM export_file WHERE id=?', (source_id,)).fetchone()
    return row['hash'] if row else None
