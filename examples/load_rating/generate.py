"""Offline synthesis of cyclic load-test evidence (no network, stdlib only).

Generates twelve specimens of one printed structure on six independent build
plates (two specimens per plate), two independently clocked channels with
synchronisation pulses and a signed drift envelope, two pre-fixed contracts
and calibration records.  Every trace has at most 50 sampling points; cycles
that are not sampled simply do not appear in the cycle index.

Run:  python3 -m examples.load_rating.generate <output_dir>
"""
import json
from pathlib import Path

MODEL = 'CLIP-A'
MATERIAL = 'LOT-PA12-07'
ORIENTATION = 'flat-x'
PERIOD_MS = 10.0

# (specimen, plate, load, temp, sampled cycles, behaviour)
SPECIMENS = [
    ('S01', 'P1', 120, 23.0, [1, 5, 10, 20, 30], 'clean'),
    ('S02', 'P1', 60, 55.0, [1, 10, 30, 100, 300, 1000, 2000, 3000], 'clean'),
    ('S03', 'P2', 120, 23.0, [1, 5, 10, 20, 30], 'clean'),
    ('S04', 'P2', 60, 55.0, [1, 10, 30, 100, 300, 1000, 2000, 3000], 'clean'),
    ('S05', 'P3', 120, 23.0, [1, 5, 10, 20, 30], 'clean'),
    # Ambiguous: drift envelope admits two adjacent offsets; a one-sample
    # shift flips elastic recovery vs permanent set under the 0.10 mm limit.
    ('S06', 'P3', 60, 55.0, [1, 10, 30, 100, 300, 1000, 2000, 3000], 'ambiguous'),
    ('S07', 'P4', 120, 23.0, [1, 5, 10, 20, 30], 'clean'),
    ('S08', 'P4', 60, 55.0, [1, 10, 30, 100, 300, 1000, 2000, 3000], 'clean'),
    ('S09', 'P5', 120, 23.0, [1, 5, 10, 20, 30], 'clean'),
    # Right-censored: voluntarily stopped at 500 cycles without failure.
    ('S10', 'P5', 180, 55.0, [1, 10, 30, 100, 300, 500], 'censored'),
    # Permanent deformation after 100 cycles while the force peak stays high.
    ('S11', 'P6', 180, 55.0, [1, 10, 30, 50, 100, 120, 140], 'permanent_set'),
    ('S12', 'P6', 120, 23.0, [1, 5, 10, 20, 30], 'clean'),
]

CONTRACTS = [
    {
        'contract_id': 'ONESHOT-30',
        'description': '一次性安装卡扣：30 次循环、常温、永久变形 < 0.20 mm',
        'required_cycles': 30,
        'permanent_set_mm': 0.20,
        'hysteresis_growth_mm': 0.08,
        'temperature_band_c': [10.0, 30.0],
        'risk_limit': 0.35,
        'confidence': 0.9,
    },
    {
        'contract_id': 'REUSE-3000',
        'description': '反复开合卡扣：3000 次循环、覆盖高温、永久变形 < 0.10 mm',
        'required_cycles': 3000,
        'permanent_set_mm': 0.10,
        'hysteresis_growth_mm': 0.05,
        'temperature_band_c': [45.0, 65.0],
        'risk_limit': 0.35,
        'confidence': 0.9,
    },
]

PLATES = [
    {'build_plate': f'P{i}', 'plate_batch': f'BATCH-{((i - 1) // 2) + 1}',
     'machine': 'PRINTER-1', 'nozzle_id': 'NZ-04-01',
     'ambient_note': '同板试片共享喷嘴与环境波动，不构成独立批次'}
    for i in range(1, 7)
]


def _trace(specimen_id, plate, load, temp, sampled_cycles, behaviour):
    """Three samples per sampled cycle: near-zero start, loaded peak, end."""
    force_t, force_n, disp_t, disp_mm, cycle_idx, temps = [], [], [], [], [], []
    drift_ppm = 30.0
    clock_offset_ms = 0.0
    envelope = (-50.0, 50.0)
    offset_tol_us = 1000.0
    if behaviour == 'ambiguous':
        # Displacement clock is about one sample late.  The displacement
        # channel carries one extra leading sample; the offset tolerance and
        # wide drift envelope admit both k=0 and k=-1, whereas a clean trace
        # keeps a unique alignment.
        clock_offset_ms = PERIOD_MS
        drift_ppm = 40.0
        envelope = (-60000.0, 60000.0)
        offset_tol_us = 12000.0
        disp_t.append(-PERIOD_MS)
        disp_mm.append(0.0)
    for c in sampled_cycles:
        if behaviour == 'permanent_set' and c >= 100:
            residual = 0.16
        elif behaviour == 'ambiguous' and c >= 300:
            # 0.11 mm: under the correct alignment the near-zero-force sample
            # keeps this below the 0.20 mm one-shot limit; a one-sample shift
            # pairs it with the loaded peak and crosses the 0.10 mm reuse limit.
            residual = 0.11
        else:
            residual = 0.01
        width = 1.2
        points = [
            (0.0, residual),
            (float(load), width),
            (0.0, residual),
        ]
        for f, d in points:
            i = len(force_t)
            ft = i * PERIOD_MS
            dt = ft + clock_offset_ms + drift_ppm * 1e-6 * ft
            force_t.append(round(ft, 3))
            disp_t.append(round(dt, 3))
            force_n.append(f)
            disp_mm.append(d)
            cycle_idx.append(c)
            temps.append(temp)
    stop = {
        'clean': {'reason': 'completed', 'note': '达到目标循环后正常结束'},
        'censored': {'reason': 'right_censored', 'note': '500 次未失效后主动停机'},
        'permanent_set': {'reason': 'permanent_set',
                          'note': '100 次后残余变形超限，力峰仍保持高值'},
        'ambiguous': {'reason': 'completed', 'note': '采样边界处于残余变形阈值附近'},
    }[behaviour]
    return {
        'id': specimen_id,
        'model': MODEL,
        'material_lot': MATERIAL,
        'build_plate': plate,
        'orientation': ORIENTATION,
        'calibration_id': 'CAL-01',
        'load_level_n': float(load),
        'cycle': cycle_idx,
        'temperature_c': temps,
        'channels': {
            'force': {'t_ms': force_t, 'force_n': force_n, 'unit': 'N'},
            'displacement': {'t_ms': disp_t, 'disp_mm': disp_mm, 'unit': 'mm'},
        },
        'sync': {
            'force_pulse_t_ms': [0.0, force_t[-1]],
            'disp_pulse_t_ms': ([0.0, round(disp_t[-1] - clock_offset_ms, 3)]
                                if behaviour == 'ambiguous'
                                else [0.0, disp_t[-1]]),
            'drift_ppm': list(envelope),
            'offset_tolerance_us': offset_tol_us,
        },
        'failure_markers': {'acoustic_crack_cycle': [], 'fracture_cycle': []},
        'stop': stop,
    }


def generate(out_dir):
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    traces = [_trace(*s) for s in SPECIMENS]
    assert all(len(t['channels']['force']['force_n']) <= 50 for t in traces)
    (root / 'force_traces.json').write_text(
        json.dumps({'version': 'trace-synth-1', 'rows': traces},
                   ensure_ascii=False, indent=2), encoding='utf-8')
    (root / 'contracts.json').write_text(
        json.dumps({'version': 'contract-synth-1', 'contracts': CONTRACTS},
                   ensure_ascii=False, indent=2), encoding='utf-8')
    (root / 'plates.json').write_text(
        json.dumps({'version': 'plate-synth-1', 'plates': PLATES},
                   ensure_ascii=False, indent=2), encoding='utf-8')
    (root / 'calibrations.json').write_text(
        json.dumps({'version': 'cal-synth-1', 'rows': [
            {'calibration_id': 'CAL-01', 'sensor': 'force-cell-FC7',
             'scale_correction': 1.0, 'bias_n': 0.0,
             'issued_at': '2026-10-01', 'supersedes': None}]},
            ensure_ascii=False, indent=2), encoding='utf-8')
    return root


if __name__ == '__main__':
    import sys
    print(generate(sys.argv[1] if len(sys.argv) > 1 else 'examples/load_rating/data'))
