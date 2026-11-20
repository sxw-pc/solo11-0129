"""Capture force/displacement test traces and sensor calibrations.

Legacy exports (a single ``force_n`` series per row) keep their original
peak-only behaviour.  Structured exports carry two independently clocked
channels, synchronisation pulses with a signed drift envelope, build plate
identity, temperature, cycle index and an explicit stop reason so that
:mod:`sliceledger.load_rating` can derive a rated working load.
"""
import json

from .ingest import capture
from .store import connect, decide, merge_specimen, save

SPECIMEN_FIELDS = ('id', 'model', 'material_lot', 'build_plate', 'orientation',
                   'calibration_id', 'load_level_n', 'channels', 'cycle',
                   'temperature_c', 'sync', 'stop', 'failure_markers')


def _validate_specimen(row):
    missing = [f for f in ('id', 'build_plate', 'calibration_id', 'load_level_n',
                           'channels', 'sync', 'stop') if f not in row]
    if missing:
        raise ValueError('specimen_field_missing:' + ','.join(missing))
    force = row['channels'].get('force') if isinstance(row['channels'], dict) else None
    if not force or 'force_n' not in force or 't_ms' not in force:
        raise ValueError('force_channel_missing')
    # The displacement channel may arrive in a later export (late channel);
    # the rating engine keeps the specimen pending until it is merged in.
    sync = row['sync']
    for f in ('force_pulse_t_ms', 'disp_pulse_t_ms', 'drift_ppm'):
        if f not in sync:
            raise ValueError('sync_field_missing:' + f)
    if len(sync['force_pulse_t_ms']) != len(sync['disp_pulse_t_ms']):
        raise ValueError('sync_pulse_mismatch')
    lo, hi = sync['drift_ppm']
    if lo > hi:
        raise ValueError('drift_range_inverted')
    stop = row['stop']
    if stop.get('reason') not in ('fracture', 'permanent_set', 'hysteresis_growth',
                                  'acoustic_crack', 'right_censored', 'completed'):
        raise ValueError('unknown_stop_reason')
    n = len(force['force_n'])
    for f in ('cycle', 'temperature_c'):
        if f in row and len(row[f]) != n:
            raise ValueError('series_length_mismatch:' + f)


def import_trace(path, file):
    source, payload = capture(path, 'force_test', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                if 'channels' not in row:
                    # Legacy peak-only export: unchanged capture path.
                    readings = row['force_n']
                    body = {**row, 'peak_force_n': max(readings)}
                    save(db, 'force_trace', key, source, payload['version'], body,
                         '已采录', 'peak_recorded')
                    results.append(decide(db, '载荷试验', key, '已采录',
                                          'peak_recorded', source))
                    continue
                _validate_specimen(row)
                patch = {f: row[f] for f in SPECIMEN_FIELDS if f in row}
                old = db.execute('SELECT * FROM load_specimen WHERE id=?', (key,)).fetchone()
                if old:
                    previous = json.loads(old['body'])
                    if 'channels' in patch and 'channels' in previous:
                        # Merge channel-by-channel so a late displacement
                        # channel joins the captured force channel; an already
                        # captured channel with changed raw samples still
                        # raises identity_conflict inside merge_specimen.
                        patch['channels'] = {
                            **previous['channels'], **patch['channels']}
                    if 'sync' in patch and 'sync' in previous:
                        # A late channel file may refine the pulse pair; keep
                        # every captured pulse envelope rather than replacing it.
                        merged_sync = dict(previous['sync'])
                        for field, value in patch['sync'].items():
                            if field not in merged_sync:
                                merged_sync[field] = value
                        patch['sync'] = merged_sync
                reason = 'channel_merged' if old else 'trace_captured'
                merge_specimen(db, 'load_specimen', key, source, payload['version'],
                               patch, '已采录', reason)
                results.append(decide(db, '载荷试验', key, '已采录', reason, source))
        return results
    finally:
        db.close()


def import_calibration(path, file):
    """Capture a force-sensor calibration (or a corrected re-calibration).

    Calibration versions are never mixed: a corrected row names the version it
    supersedes, and the rating engine keeps evidence under each version apart.
    """
    source, payload = capture(path, 'force_calibration', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                if 'calibration_id' not in row:
                    raise ValueError('calibration_id_required')
                key = str(row['calibration_id'])
                save(db, 'load_calibration', key, source, payload['version'], row,
                     '现行', 'calibration_captured')
                results.append(decide(db, '传感器校准', key, '现行',
                                      'calibration_captured', source))
        return results
    finally:
        db.close()
