"""本地承运柜台清单:逐箱核对封箱与交运证据。

每一行承运导出只对应一只实体箱。两只物理箱必须有两只独立箱号、
两份称重、两个封箱扫描、两个追踪号,且都在截单时刻之前封箱;
一张带 tracking 的回执不能把两只箱都标成已交运。称重与装箱规划
的可行重量范围不符时只有该箱待核,其他箱的交运状态和已签回执
保留。重复扫描或重放不新增包裹;同一清单 id 内容被改动按
identity_conflict 拒绝,旧运单内容不得悄悄修改。
"""
import json
from datetime import datetime
from fractions import Fraction

from .ingest import capture
from .store import connect, save, decide


def _frac(value):
    return Fraction(str(value))


def manifest(path, file):
    source, payload = capture(path, 'desktop_carrier_manifest', file)
    db = connect(path)
    results = []
    try:
        with db:
            for index, row in enumerate(payload.get('rows', []), 1):
                results.append(_manifest_row(db, source, payload['version'], row, index))
        return results
    finally:
        db.close()


def _manifest_row(db, source, version, row, index):
    key = str(row.get('id') or f'line:{index}')
    encoded = json.dumps(row, sort_keys=True, ensure_ascii=False)
    old = db.execute('SELECT * FROM carrier_manifest WHERE id=?', (key,)).fetchone()
    if old and old['body'] == encoded:
        return decide(db, '承运清单', key, old['status'], old['reason'], source)
    status, reason = _check(db, key, row)
    save(db, 'carrier_manifest', key, source, version, row, status, reason)
    if status == '已交运':
        serial = str(row.get('box_serial'))
        evidence = {'plan': str(row.get('plan')), 'manifest': key,
                    'tracking': row.get('tracking'), 'seal_scan': row.get('seal_scan'),
                    'weighed_kg': row.get('weighed_kg'), 'sealed_at': row.get('sealed_at')}
        save(db, 'carrier_evidence', serial, source, version, evidence, '已交运', 'box_shipped')
    return decide(db, '承运清单', key, status, reason, source)


def _check(db, key, row):
    plan_id = str(row.get('plan', ''))
    plan_row = db.execute('SELECT * FROM packing_plan WHERE id=?', (plan_id,)).fetchone()
    if not plan_row:
        return '待核', 'packing_missing'
    if plan_row['status'] != '已装箱':
        return '待核', 'packing_not_ready'
    plan = json.loads(plan_row['body'])
    serial = str(row.get('box_serial', ''))
    boxes = {box['serial']: box for box in plan.get('boxes', [])}
    if serial not in boxes:
        return '待核', 'box_not_in_plan'
    allocation = db.execute('SELECT body FROM packing_box WHERE id=?', (serial,)).fetchone()
    if allocation and json.loads(allocation['body'])['plan_id'] != plan_id:
        return '待核', 'plan_superseded'
    if db.execute('SELECT id FROM carrier_evidence WHERE id=?', (serial,)).fetchone():
        return '待核', 'duplicate_box_scan'
    for other in db.execute('SELECT id, body FROM carrier_manifest WHERE id != ?', (key,)):
        other_body = json.loads(other['body'])
        if (str(other_body.get('box_serial')) == serial
                and str(other_body.get('plan')) == plan_id):
            return '待核', 'duplicate_box_scan'
    tracking = row.get('tracking')
    if not tracking:
        return '待核', 'tracking_missing'
    seal = row.get('seal_scan')
    if not seal:
        return '待核', 'seal_missing'
    for evidence_row in db.execute('SELECT id, body FROM carrier_evidence'):
        evidence = json.loads(evidence_row['body'])
        if evidence_row['id'] != serial:
            if evidence.get('tracking') == tracking:
                return '待核', 'tracking_reused'
            if evidence.get('seal_scan') == seal:
                return '待核', 'seal_reused'
    weighed = row.get('weighed_kg')
    if weighed is None:
        return '待核', 'weigh_missing'
    try:
        diff = abs(_frac(weighed) - _frac(boxes[serial]['gross_kg']))
        tolerance = _frac(plan.get('weight_tolerance_kg', '0.1'))
    except (TypeError, ValueError, ArithmeticError):
        return '待核', 'weigh_invalid'
    if diff > tolerance:
        return '待核', 'weight_mismatch'
    try:
        sealed_at = datetime.fromisoformat(str(row.get('sealed_at')))
    except (TypeError, ValueError):
        return '待核', 'seal_time_missing'
    cutoff = plan.get('cutoff')
    if cutoff and sealed_at > datetime.fromisoformat(str(cutoff)):
        return '待核', 'cutoff_missed'
    return '已交运', 'box_shipped'
