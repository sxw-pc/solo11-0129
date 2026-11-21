import hashlib,json
from pathlib import Path
from .store import connect,TABLES


def packing_section(db):
    """Read back per-plan layout, infeasibility diagnosis and per-box shipment."""
    layouts={}
    for row in db.execute('SELECT * FROM packing_layout ORDER BY id'):
        layouts.setdefault(json.loads(row['body']).get('plan'),[]).append(row)
    shipments={row['id']:row for row in db.execute('SELECT * FROM carrier_box')}
    plans=[]
    for plan in db.execute('SELECT * FROM packing_plan ORDER BY id'):
        body=json.loads(plan['body'])
        entry={'plan':plan['id'],'status':plan['status'],'reason':plan['reason'],
               'contract':body.get('contract'),'cutoff':body.get('cutoff'),
               'boxes':[],'diagnosis':None,'total_price':None,'ready_at':None,'shipped':False}
        for row in layouts.get(plan['id'],[]):
            data=json.loads(row['body'])
            if data.get('kind')=='summary':
                entry['total_price']=data['total_price'];entry['ready_at']=data['ready_at']
            elif data.get('kind')=='diagnosis':
                entry['diagnosis']=data.get('causes',[])
            elif data.get('kind')=='box':
                receipt=shipments.get(f"{plan['id']}:{data['box_no']}")
                shipment={'status':'待交运'}
                if receipt:
                    proof=json.loads(receipt['body'])
                    shipment={'status':receipt['status'],'tracking':proof['tracking'],
                              'seal':proof['seal'],'weighed':proof['weighed'],
                              'scanned_at':proof['scanned_at']}
                entry['boxes'].append({'box_no':data['box_no'],'box_type':data['box_type'],
                                       'parts':data['parts'],'price':data['price'],
                                       'tier_up_to':data['tier_up_to'],
                                       'chargeable_weight':data['chargeable_weight'],
                                       'shipment':shipment})
        entry['shipped']=bool(entry['boxes']) and all(
            b['shipment']['status']=='已交运' for b in entry['boxes'])
        plans.append(entry)
    return plans


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
              'packing':packing_section(db)}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
