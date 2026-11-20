"""Tests for rated working load: synchronisation, censoring, plates,
calibration lineage, two fixed contracts and replay.
"""
import json

import pytest

from sliceledger import load_rating, load_test, report
from sliceledger.store import connect, body_of

PERIOD = 10.0
MODEL = 'CLIP-T'


def write(root, name, obj):
    p = root / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    return p


def contracts_doc(risk_limit=0.35):
    return {'version': 'ct-1', 'contracts': [
        {'contract_id': 'ONESHOT-30', 'required_cycles': 30,
         'permanent_set_mm': 0.20, 'hysteresis_growth_mm': 0.08,
         'temperature_band_c': [10.0, 30.0],
         'risk_limit': risk_limit, 'confidence': 0.9},
        {'contract_id': 'REUSE-3000', 'required_cycles': 3000,
         'permanent_set_mm': 0.10, 'hysteresis_growth_mm': 0.05,
         'temperature_band_c': [45.0, 65.0],
         'risk_limit': risk_limit, 'confidence': 0.9},
    ]}


PLATES = {'version': 'pt-1', 'plates': [
    {'build_plate': f'P{i}', 'plate_batch': f'B{i}', 'machine': 'PR-1'}
    for i in range(1, 7)]}


def trace(sid, plate, load, temp, cycles, behaviour, cal='CAL-01',
          include_disp=True, sync_offset_us=1000.0, drift_ppm=30.0,
          envelope=(-50.0, 50.0), clock_offset_ms=0.0,
          pulse_drift_ppm=None):
    force_t, force_n, disp_t, disp_mm, cycle_idx, temps = [], [], [], [], [], []
    if behaviour == 'ambiguous':
        clock_offset_ms = PERIOD
        envelope = (-60000.0, 60000.0)
        sync_offset_us = 12000.0
        if include_disp:
            disp_t.append(-PERIOD)
            disp_mm.append(0.0)
    for c in cycles:
        if behaviour == 'permanent_set' and c >= 100:
            residual = 0.16
        elif behaviour == 'ambiguous' and c >= 300:
            residual = 0.11
        else:
            residual = 0.01
        for f, d in ((0.0, residual), (float(load), 1.2), (0.0, residual)):
            i = len(force_t)
            ft = i * PERIOD
            force_t.append(round(ft, 3))
            if include_disp:
                dt = ft + clock_offset_ms + drift_ppm * 1e-6 * ft
                disp_t.append(round(dt, 3))
                disp_mm.append(d)
            force_n.append(f)
            cycle_idx.append(c)
            temps.append(temp)
    channels = {'force': {'t_ms': force_t, 'force_n': force_n, 'unit': 'N'}}
    if include_disp:
        channels['displacement'] = {'t_ms': disp_t, 'disp_mm': disp_mm, 'unit': 'mm'}
        if behaviour == 'ambiguous':
            pulse_d = [0.0, round(disp_t[-1] - clock_offset_ms, 3)]
        elif pulse_drift_ppm is not None:
            pulse_d = [0.0, round(force_t[-1] * (1 + pulse_drift_ppm * 1e-6), 3)]
        else:
            pulse_d = [0.0, disp_t[-1]]
    else:
        pulse_d = [0.0, force_t[-1] + clock_offset_ms]
    return {
        'id': sid, 'model': MODEL, 'material_lot': 'LOT-1',
        'build_plate': plate, 'orientation': 'flat-x',
        'calibration_id': cal, 'load_level_n': float(load),
        'cycle': cycle_idx, 'temperature_c': temps, 'channels': channels,
        'sync': {'force_pulse_t_ms': [0.0, force_t[-1]],
                 'disp_pulse_t_ms': pulse_d,
                 'drift_ppm': list(envelope),
                 'offset_tolerance_us': sync_offset_us},
        'failure_markers': {'acoustic_crack_cycle': [], 'fracture_cycle': []},
        'stop': {'reason': {
            'clean': 'completed', 'censored': 'right_censored',
            'permanent_set': 'permanent_set', 'ambiguous': 'completed'}[behaviour],
            'note': behaviour},
    }


def synth_rows():
    short = [1, 5, 10, 20, 30]
    long = [1, 10, 30, 100, 300, 1000, 2000, 3000]
    return [
        trace('S01', 'P1', 120, 23.0, short, 'clean'),
        trace('S02', 'P1', 60, 55.0, long, 'clean'),
        trace('S03', 'P2', 120, 23.0, short, 'clean'),
        trace('S04', 'P2', 60, 55.0, long, 'clean'),
        trace('S05', 'P3', 120, 23.0, short, 'clean'),
        trace('S06', 'P3', 60, 55.0, long, 'ambiguous'),
        trace('S07', 'P4', 120, 23.0, short, 'clean'),
        trace('S08', 'P4', 60, 55.0, long, 'clean'),
        trace('S09', 'P5', 120, 23.0, short, 'clean'),
        trace('S10', 'P5', 180, 55.0, [1, 10, 30, 100, 300, 500], 'censored'),
        trace('S11', 'P6', 180, 55.0, [1, 10, 30, 50, 100, 120, 140],
              'permanent_set'),
        trace('S12', 'P6', 120, 23.0, short, 'clean'),
    ]


def seed(root, rows=None, risk_limit=0.35, cal_rows=None):
    rows = synth_rows() if rows is None else rows
    db = root / 'ledger.db'
    load_test.import_trace(db, write(root, 'traces.json',
                                     {'version': 't-1', 'rows': rows}))
    if cal_rows is None:
        cal_rows = [{'calibration_id': 'CAL-01', 'sensor': 'FC7',
                     'scale_correction': 1.0, 'bias_n': 0.0,
                     'issued_at': '2026-10-01', 'supersedes': None}]
    load_test.import_calibration(db, write(root, 'cal.json',
                                           {'version': 'c-1', 'rows': cal_rows}))
    cf = write(root, 'contracts.json', contracts_doc(risk_limit))
    pf = write(root, 'plates.json', PLATES)
    return db, cf, pf


def result_for(doc, cid):
    return doc['basis']['contracts'][cid]


def test_legacy_peak_only_trace_still_supported(tmp_path):
    db = tmp_path / 'legacy.db'
    f = write(tmp_path, 'legacy.json', {'version': 'v1', 'rows': [
        {'id': 'T1', 'force_n': [0, 100, 90], 'cycle': [0, 1, 2]}]})
    assert load_test.import_trace(db, f)[0]['status'] == '已采录'
    conn = connect(db)
    try:
        assert json.loads(conn.execute(
            "SELECT body FROM force_trace WHERE id='T1'").fetchone()[0]
        )['peak_force_n'] == 100
    finally:
        conn.close()


def test_drift_envelope_admits_single_alignment_on_clean_traces(tmp_path):
    rows = synth_rows()
    clean = [r for r in rows if r['id'] not in ('S06',)]
    for r in clean:
        assert load_rating.compatible_shifts(r) == [0]
    ambiguous = next(r for r in rows if r['id'] == 'S06')
    assert load_rating.compatible_shifts(ambiguous) == [-1, 0]


def test_drift_outside_envelope_has_no_compatible_alignment(tmp_path):
    # The sync pulses claim the clocks run at ~0 ppm drift, but the actual
    # displacement timestamps accumulate 30 ppm over ~3 s; no offset k can
    # reconcile them inside a +/-1 ppm envelope.
    r = trace('X1', 'P1', 100, 23.0, list(range(1, 101)), 'clean',
              drift_ppm=30.0, pulse_drift_ppm=0.0,
              envelope=(-1.0, 1.0), sync_offset_us=1000.0)
    with pytest.raises(ValueError, match='no_compatible_alignment'):
        load_rating.compatible_shifts(r)


def test_adjacent_sample_ambiguity_marks_specimen_pending(tmp_path):
    db, cf, pf = seed(tmp_path)
    conn = connect(db)
    try:
        s06 = body_of(conn.execute(
            "SELECT * FROM load_specimen WHERE id='S06'").fetchone())
        reuse = contracts_doc()['contracts'][1]
        analysis = load_rating.analyse_specimen(s06, reuse)
        assert analysis['ambiguous'] is True
        assert analysis['pending_reason'] == 'alignment_ambiguous'
        cycles = {p['first_failure_cycle'] for p in analysis['alignment_paths']}
        assert cycles == {300, 1000}
        oneshot = contracts_doc()['contracts'][0]
        easy = load_rating.analyse_specimen(s06, oneshot)
        assert easy['ambiguous'] is False
    finally:
        conn.close()


def test_two_contracts_get_different_ratings_from_same_specimens(tmp_path):
    db, cf, pf = seed(tmp_path)
    out, doc = load_rating.rate_model(db, cf, pf, MODEL,
                                      out_dir=tmp_path / 'ratings')
    assert out.read_text(encoding='utf-8')  # issuance file exists
    one = result_for(doc, 'ONESHOT-30')
    reuse = result_for(doc, 'REUSE-3000')
    # One-shot: all six independent plates survive 30 cycles at 120 N.
    assert one['certified_load_n'] == 120.0
    # Reuse: the 60 N evidence contains an ambiguous plate, so no certification.
    assert reuse['certified_load_n'] is None
    assert any(lv['pending_calibration'] for lv in reuse['levels']
               if lv['load_level_n'] == 60.0)


def test_right_censored_500_cycles_is_not_a_3000_cycle_pass(tmp_path):
    db, cf, pf = seed(tmp_path)
    _, doc = load_rating.rate_model(db, cf, pf, MODEL, out_dir=tmp_path / 'r')
    reuse = result_for(doc, 'REUSE-3000')
    level180 = next(lv for lv in reuse['levels'] if lv['load_level_n'] == 180.0)
    outcomes = {p['build_plate']: p['outcome'] for p in level180['plate_outcomes']}
    assert outcomes['P5'] == 'right_censored'
    assert outcomes['P6'] == 'failure'
    assert level180['meets_limit'] is False
    follow = next(f for f in reuse['follow_up_tests']
                  if f['load_level_n'] == 180.0)
    assert follow['run_to_cycle'] == 3000
    assert 'right_censored' in follow['note']


def test_plate_repeats_count_as_one_independent_unit(tmp_path):
    # Two specimens on one plate must never be counted as two batches.
    rows = [
        trace('A1', 'P1', 120, 23.0, [1, 5, 10, 20, 30], 'clean'),
        trace('A2', 'P1', 120, 23.0, [1, 5, 10, 20, 30], 'clean'),
        trace('A3', 'P2', 120, 23.0, [1, 5, 10, 20, 30], 'clean'),
    ]
    db, cf, pf = seed(tmp_path, rows=rows)
    _, doc = load_rating.rate_model(db, cf, pf, MODEL, out_dir=tmp_path / 'r')
    one = result_for(doc, 'ONESHOT-30')
    level = next(lv for lv in one['levels'] if lv['load_level_n'] == 120.0)
    assert level['independent_plates'] == 2
    # Zero failures in 2 plates: CP one-sided 90% upper bound = 1-0.1^(1/2).
    assert level['failure_risk_upper_bound'] == pytest.approx(
        1 - 0.1 ** 0.5)


def test_clopper_pearson_bounds():
    assert load_rating.risk_upper_bound(0, 6) == pytest.approx(
        1 - 0.1 ** (1 / 6))
    assert load_rating.risk_upper_bound(2, 2) == 1.0
    # One failure in four units: the one-sided upper bound strictly exceeds
    # the observed 0.25 fraction.
    b14 = load_rating.risk_upper_bound(1, 4)
    assert b14 > 0.25
    assert b14 < 1.0
    # More evidence of zero failures tightens the bound.
    assert load_rating.risk_upper_bound(0, 6) < load_rating.risk_upper_bound(0, 4)
    assert load_rating.risk_upper_bound(0, 4) < 0.5
    assert load_rating.risk_upper_bound(0, 0) is None


def test_late_displacement_channel_keeps_specimen_pending_then_resolves(tmp_path):
    rows = synth_rows()
    s02 = next(r for r in rows if r['id'] == 'S02')
    force_only = {k: v for k, v in s02.items() if k != 'channels'}
    force_only['channels'] = {'force': s02['channels']['force']}
    partial = [force_only if r['id'] == 'S02' else r for r in rows]
    db = tmp_path / 'ledger.db'
    load_test.import_trace(db, write(tmp_path, 't1.json',
                                     {'version': 't-1', 'rows': partial}))
    load_test.import_calibration(db, write(tmp_path, 'cal.json', {'version': 'c-1', 'rows': [
        {'calibration_id': 'CAL-01', 'scale_correction': 1.0, 'bias_n': 0.0,
         'supersedes': None}]}))
    conn = connect(db)
    try:
        stored = body_of(conn.execute(
            "SELECT * FROM load_specimen WHERE id='S02'").fetchone())
        analysis = load_rating.analyse_specimen(stored, contracts_doc()['contracts'][1])
        assert analysis['pending_reason'] == 'displacement_channel_pending'
    finally:
        conn.close()
    # Late channel arrives and merges into the same specimen record.
    load_test.import_trace(db, write(tmp_path, 't2.json',
                                     {'version': 't-1', 'rows': [s02]}))
    conn = connect(db)
    try:
        stored = body_of(conn.execute(
            "SELECT * FROM load_specimen WHERE id='S02'").fetchone())
        assert 'displacement' in stored['channels']
        analysis = load_rating.analyse_specimen(stored, contracts_doc()['contracts'][1])
        assert analysis['pending_reason'] is None
        # Repeated imports do not create extra independent specimens.
        assert conn.execute('SELECT count(*) FROM load_specimen').fetchone()[0] == 12
    finally:
        conn.close()


def test_corrected_calibration_reopens_and_withdraws_old_promise(tmp_path):
    db, cf, pf = seed(tmp_path)
    _, doc1 = load_rating.rate_model(db, cf, pf, MODEL, out_dir=tmp_path / 'r')
    assert result_for(doc1, 'ONESHOT-30')['certified_load_n'] == 120.0
    # Corrected calibration raises forces by 10 % and supersedes CAL-01.
    # Specimens keep their original calibration_id (raw evidence is immutable);
    # the lineage maps them onto the effective calibration CAL-02.
    cal2 = [{'calibration_id': 'CAL-02', 'sensor': 'FC7',
             'scale_correction': 1.1, 'bias_n': 0.0,
             'issued_at': '2026-11-10', 'supersedes': 'CAL-01'}]
    load_test.import_calibration(db, write(tmp_path, 'cal2.json',
                                           {'version': 'c-2', 'rows': cal2}))
    _, doc2 = load_rating.rate_model(db, cf, pf, MODEL, out_dir=tmp_path / 'r')
    assert doc2['calibration_id'] == 'CAL-02'
    assert 'CAL-01' in doc2['basis']['superseded_calibrations']
    withdrawn = [w for w in doc2['basis']['withdrawals']
                 if w['rating_id'] == f'{MODEL}:CAL-01']
    assert withdrawn and 'CAL-02' in withdrawn[0]['reason']
    conn = connect(db)
    try:
        old = conn.execute("SELECT status FROM load_rating WHERE id=?",
                           (f'{MODEL}:CAL-01',)).fetchone()
        assert old[0] == '已撤回'
        # The old issuance reason remains readable in the decision log.
        reasons = [r[0] for r in conn.execute(
            "SELECT reason FROM decision_log WHERE subject=? ORDER BY id",
            (f'{MODEL}:CAL-01',))]
        assert 'risk_bound_below_contract_limit' in reasons
        # Raw force samples were never rewritten.
        s01 = body_of(conn.execute(
            "SELECT * FROM load_specimen WHERE id='S01'").fetchone())
        assert s01['calibration_id'] == 'CAL-01'
    finally:
        conn.close()


def test_different_calibration_versions_are_not_pooled(tmp_path):
    db, cf, pf = seed(tmp_path)
    # An independent calibration line is captured but never supersedes CAL-01.
    load_test.import_calibration(db, write(tmp_path, 'cal2.json', {'version': 'c-2', 'rows': [
        {'calibration_id': 'CAL-09', 'scale_correction': 1.0, 'bias_n': 0.0,
         'supersedes': None}]}))
    # Specimens stay bound to CAL-01; evidence under CAL-09 is a separate pool.
    _, doc = load_rating.rate_model(db, cf, pf, MODEL,
                                    calibration_id='CAL-01',
                                    out_dir=tmp_path / 'r')
    assert result_for(doc, 'ONESHOT-30')['certified_load_n'] == 120.0
    # An export that mutates an immutable raw series is rejected, not merged.
    rows = synth_rows()
    rows[0]['channels']['force']['force_n'][1] += 5.0
    with pytest.raises(ValueError, match='identity_conflict'):
        load_test.import_trace(db, write(tmp_path, 'mutated.json',
                                         {'version': 't-2', 'rows': rows}))


def test_replay_is_idempotent_and_report_shows_withdrawal(tmp_path):
    db, cf, pf = seed(tmp_path)
    d1 = load_rating.rate_model(db, cf, pf, MODEL, out_dir=tmp_path / 'r')[1]
    d2 = load_rating.rate_model(db, cf, pf, MODEL, out_dir=tmp_path / 'r')[1]
    assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)
    out = report.build(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    summary = next(x for x in data['load_ratings'] if x['rating_id'] == f'{MODEL}:CAL-01')
    assert summary['status'] == '已签发'
    assert summary['contracts']['ONESHOT-30']['certified_load_n'] == 120.0
    # Raw samples, sync range, plate id and censoring are all retained.
    conn = connect(db)
    try:
        s10 = body_of(conn.execute(
            "SELECT * FROM load_specimen WHERE id='S10'").fetchone())
        assert s10['stop']['reason'] == 'right_censored'
        assert s10['build_plate'] == 'P5'
        assert 'drift_ppm' in s10['sync']
    finally:
        conn.close()


def test_corrected_calibration_reinterprets_first_failure_cycle(tmp_path):
    # Sensor bias accumulates with the cycle count.  CAL-01's +/-1 N zero band
    # keeps every unloaded sample anchored; the corrected CAL-02 narrows the
    # band to +/-0.5 N, so from cycle 100 the unloaded samples lose their
    # anchor and the loop growth is flagged earlier than under CAL-01.
    r = trace('H1', 'P1', 100, 55.0, [1, 10, 30, 100, 300], 'clean')
    bias_by_cycle = {1: 0.2, 10: 0.3, 30: 0.4, 100: 0.6, 300: 0.8}
    for i, f in enumerate(r['channels']['force']['force_n']):
        if f == 0.0:
            r['channels']['force']['force_n'][i] = bias_by_cycle[r['cycle'][i]]
    # Loop stays inside the allowance at 100 but crosses it at 300.
    r['channels']['displacement']['disp_mm'] = [
        0.01, 1.20, 0.01, 0.01, 1.20, 0.01, 0.01, 1.20, 0.01,
        0.02, 1.22, 0.02, 0.03, 1.30, 0.03]
    contract = dict(contracts_doc()['contracts'][1])
    contract['hysteresis_growth_mm'] = 0.08
    cal1 = {'calibration_id': 'CAL-01', 'zero_band_n': 1.0,
            'scale_correction': 1.0, 'bias_n': 0.0}
    cal2 = {'calibration_id': 'CAL-02', 'zero_band_n': 0.5,
            'scale_correction': 1.0, 'bias_n': 0.0}
    a1 = load_rating.analyse_specimen(r, contract, cal1)
    a2 = load_rating.analyse_specimen(r, contract, cal2)
    first1 = a1['alignment_paths'][0]['first_failure_cycle']
    first2 = a2['alignment_paths'][0]['first_failure_cycle']
    assert first2 < first1
    assert a2['alignment_paths'][0]['failure_mode'] == 'unconfirmed_recovery'


def test_acoustic_marker_is_an_independent_failure_path(tmp_path):
    r = trace('AC1', 'P1', 100, 23.0, [1, 5, 10, 20, 30], 'clean')
    r['failure_markers'] = {'acoustic_crack_cycle': [10], 'fracture_cycle': []}
    contract = contracts_doc()['contracts'][0]
    analysis = load_rating.analyse_specimen(r, contract)
    path = analysis['alignment_paths'][0]
    assert path['first_failure_cycle'] == 10
    assert path['failure_mode'] == 'acoustic_crack'


def test_fracture_is_an_independent_failure_path(tmp_path):
    r = trace('FR1', 'P1', 100, 23.0, [1, 5, 10, 20, 30], 'clean')
    r['failure_markers'] = {'acoustic_crack_cycle': [], 'fracture_cycle': [20]}
    contract = contracts_doc()['contracts'][0]
    analysis = load_rating.analyse_specimen(r, contract)
    path = analysis['alignment_paths'][0]
    assert path['first_failure_cycle'] == 20
    assert path['failure_mode'] == 'fracture'


def test_pending_displacement_channel_blocks_level_certification(tmp_path):
    rows = synth_rows()
    s02 = next(r for r in rows if r['id'] == 'S02')
    force_only = {k: v for k, v in s02.items() if k != 'channels'}
    force_only['channels'] = {'force': s02['channels']['force']}
    partial = [force_only if r['id'] == 'S02' else r for r in rows]
    db = tmp_path / 'ledger.db'
    load_test.import_trace(db, write(tmp_path, 't1.json',
                                     {'version': 't-1', 'rows': partial}))
    load_test.import_calibration(db, write(tmp_path, 'cal.json', {'version': 'c-1', 'rows': [
        {'calibration_id': 'CAL-01', 'scale_correction': 1.0, 'bias_n': 0.0,
         'zero_band_n': 1.0, 'supersedes': None}]}))
    cf = write(tmp_path, 'contracts.json', contracts_doc())
    pf = write(tmp_path, 'plates.json', PLATES)
    _, doc = load_rating.rate_model(db, cf, pf, MODEL, out_dir=tmp_path / 'r')
    reuse = result_for(doc, 'REUSE-3000')
    level60 = next(lv for lv in reuse['levels'] if lv['load_level_n'] == 60.0)
    assert level60['pending_calibration'] is True
    assert reuse['certified_load_n'] is None
    pending = next(f for f in reuse['follow_up_tests']
                   if f['load_level_n'] == 60.0)
    # P1 awaits the late displacement channel; P3 awaits alignment resolution.
    assert set(pending['recalibrate_plates']) == {'P1', 'P3'}


def test_reimport_unchanged_trace_does_not_duplicate(tmp_path):
    db, cf, pf = seed(tmp_path)
    conn = connect(db)
    first = conn.execute('SELECT count(*) FROM export_record').fetchone()[0]
    conn.close()
    seed(tmp_path)  # re-import the identical files
    conn = connect(db)
    try:
        assert conn.execute('SELECT count(*) FROM export_record').fetchone()[0] == first
        assert conn.execute('SELECT count(*) FROM load_specimen').fetchone()[0] == 12
    finally:
        conn.close()


def test_follow_up_names_plate_and_cycle_instead_of_peak_discount(tmp_path):
    db, cf, pf = seed(tmp_path)
    _, doc = load_rating.rate_model(db, cf, pf, MODEL, out_dir=tmp_path / 'r')
    reuse = result_for(doc, 'REUSE-3000')
    follow60 = next(f for f in reuse['follow_up_tests']
                    if f['load_level_n'] == 60.0)
    assert follow60['recalibrate_plates'] == ['P3']
    assert follow60['run_to_cycle'] == 3000
    follow120 = next(f for f in reuse['follow_up_tests']
                     if f['load_level_n'] == 120.0)
    assert follow120['add_independent_plates'] >= 1
    assert follow120['note'] == 'no_plate_covers_contract_temperature_band'
