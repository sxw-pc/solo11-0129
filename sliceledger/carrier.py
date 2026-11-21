"""Per-box carrier manifest verification against a recorded packing plan.

Every manifest row is one physical box handover: it must name the
packing plan and the box number from that plan's layout, and carry an
independent seal scan, a tracking number, an on-site scale weight and a
scan timestamp.  A single receipt can never mark two boxes shipped.
A box whose scale weight falls outside the planned feasible range turns
待核 on its own; the other boxes of the same order keep their 已交运
status and signed receipts.  Evidence already recorded for a box is
immutable -- replaying an identical scan is a no-op (duplicate_scan) and
a conflicting one is rejected (waybill_conflict); moving a part to
another box requires a new packing plan version and fresh seal evidence.
"""
import json
from datetime import datetime
from .ingest import capture
from .store import connect, save, decide
from .packing import EPS


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _verify(db, row, plan, layout, plan_id, box_no, source, version):
    if not plan:
        return '待核', 'packing_missing'
    if plan['status'] != '已装箱':
        return '待核', 'packing_not_ready'
    if not layout:
        return '待核', 'box_unknown'
    if not row.get('tracking'):
        return '待核', 'tracking_missing'
    if not row.get('seal'):
        return '待核', 'seal_missing'
    weighed = _float(row.get('weighed'))
    if weighed is None:
        return '待核', 'weight_missing'
    try:
        scanned = datetime.fromisoformat(row.get('scanned_at'))
        cutoff = datetime.fromisoformat(json.loads(plan['body'])['cutoff'])
        late = scanned > cutoff
    except (TypeError, ValueError):
        return '待核', 'scan_time_missing'
    if late:
        return '待核', 'cutoff_missed'
    lo, hi = json.loads(layout['body'])['weight_range']
    if not lo - EPS <= weighed <= hi + EPS:
        return '待核', 'weight_out_of_range'
    evidence = {'packing': plan_id, 'box_no': box_no, 'tracking': str(row['tracking']),
                'seal': str(row['seal']), 'weighed': weighed, 'scanned_at': row['scanned_at']}
    existing = db.execute('SELECT * FROM carrier_box WHERE id=?',
                          (f'{plan_id}:{box_no}',)).fetchone()
    if existing:
        if json.loads(existing['body']) == evidence:
            return '已交运', 'duplicate_scan'
        return '待核', 'waybill_conflict'
    save(db, 'carrier_box', f'{plan_id}:{box_no}', source, version, evidence,
         '已交运', 'box_shipped')
    return '已交运', 'box_shipped'


def manifest(path, file):
    source, payload = capture(path, 'desktop_carrier_manifest', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                old = db.execute('SELECT * FROM carrier_manifest WHERE id=?', (key,)).fetchone()
                if old and old['body'] == json.dumps(row, sort_keys=True, ensure_ascii=False):
                    results.append(decide(db, '承运清单', key, old['status'], old['reason'], source))
                    continue
                plan_id = str(row.get('packing'))
                box_no = str(row.get('box_no') or '')
                plan = db.execute('SELECT * FROM packing_plan WHERE id=?',
                                  (plan_id,)).fetchone()
                layout = None
                if plan and box_no:
                    layout = db.execute('SELECT * FROM packing_layout WHERE id=?',
                                        (f'{plan_id}:box:{box_no}',)).fetchone()
                status, reason = _verify(db, row, plan, layout, plan_id, box_no,
                                         source, payload['version'])
                save(db, 'carrier_manifest', key, source, payload['version'],
                     row, status, reason)
                results.append(decide(db, '承运清单', key, status, reason, source))
        return results
    finally:
        db.close()
