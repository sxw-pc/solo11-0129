import hashlib,json
from pathlib import Path
from .store import connect,TABLES


def outbound(db):
    """按订单回读装箱方案、不可装原因和每箱实际交运状态。

    每箱状态:已交运(有承运证据)/ 待核(有清单但被挂起)/ 未交运 /
    已作废(所属装箱图已被同订单更新版本取代且未交运)。
    """
    plans = [(row, json.loads(row['body']))
             for row in db.execute('SELECT * FROM packing_plan ORDER BY source_id, id')]
    evidence = {row['id']: json.loads(row['body'])
                for row in db.execute('SELECT * FROM carrier_evidence')}
    manifest_status = {}
    for row in db.execute('SELECT status, body FROM carrier_manifest'):
        serial = str(json.loads(row['body']).get('box_serial'))
        if row['status'] == '已交运' or serial not in manifest_status:
            manifest_status[serial] = row['status']
    newest = {}
    for row, body in plans:
        newest[body.get('order')] = row['source_id']
    orders = {}
    for row, body in plans:
        entry = {'plan_id': body.get('plan_id'), 'status': row['status'], 'reason': row['reason'],
                 'contract': body.get('contract'), 'cutoff': body.get('cutoff'),
                 'ready_by': body.get('ready_by'), 'total_price': body.get('total_price'),
                 'diagnosis': body.get('diagnosis'), 'boxes': []}
        is_newest = row['source_id'] == newest.get(body.get('order'))
        for box in body.get('boxes', []):
            serial = box['serial']
            if serial in evidence:
                ship = '已交运'
            elif not is_newest:
                ship = '已作废'
            elif serial in manifest_status:
                ship = manifest_status[serial]
            else:
                ship = '未交运'
            ev = evidence.get(serial) or {}
            entry['boxes'].append({
                'serial': serial, 'box_type': box['box_type'],
                'parts': [item['part'] for item in box['parts']],
                'gross_kg': box['gross_kg'], 'chargeable_kg': box['chargeable_kg'],
                'tier_up_to': box['tier_up_to'], 'price': box['price'],
                'ship_status': ship, 'tracking': ev.get('tracking'),
                'seal_scan': ev.get('seal_scan'), 'weighed_kg': ev.get('weighed_kg')})
        shipped = sum(1 for box in entry['boxes'] if box['ship_status'] == '已交运')
        entry['shipped_boxes'] = shipped
        entry['total_boxes'] = len(entry['boxes'])
        if row['status'] != '已装箱':
            entry['order_status'] = row['status']
        elif entry['boxes'] and shipped == len(entry['boxes']):
            entry['order_status'] = '已交运'
        elif shipped:
            entry['order_status'] = '部分交运'
        else:
            entry['order_status'] = '未交运'
        orders.setdefault(body.get('order'), []).append(entry)
    return {'orders': orders}


def build(path,out):
    db=connect(path)
    try:
        sources=[]
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x=dict(item);x.pop('body')
            x['verified']=hashlib.sha256(item['body']).hexdigest()==item['hash']
            sources.append(x)
        data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
              'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')],
              'outbound':outbound(db)}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
