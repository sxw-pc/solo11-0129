import hashlib,json
from pathlib import Path
from .store import connect,TABLES


def impact(db):
    """Certain vs. possible impact scopes derived from layer-level feed attribution."""
    certain=[]
    for row in db.execute("SELECT * FROM part_recall ORDER BY id"):
        body=json.loads(row['body'])
        certain.append({'part':body['part'],'lot':body['lot'],'inspection':body['inspection'],
                        'status':row['status'],'reason':row['reason']})
    uncertain=[]
    for row in db.execute("SELECT * FROM part_review WHERE status='待复核' ORDER BY id"):
        body=json.loads(row['body'])
        uncertain.append({'part':body['part'],'lot':body['lot'],'inspection':body['inspection'],
                          'status':row['status'],'reason':row['reason']})
    attributions=[]
    for row in db.execute("SELECT * FROM lot_usage ORDER BY id"):
        body=json.loads(row['body'])
        attributions.append({'part':body['part'],'run':body.get('run'),'feed':body['feed'],
                             'lots':body['lots'],'total_grams':body['total_grams'],
                             'usage':body['usage'],'status':row['status']})
    untrusted=[]
    for row in db.execute("SELECT * FROM feed_evidence WHERE status!='可信' ORDER BY id"):
        body=json.loads(row['body'])
        untrusted.append({'feed':row['id'],'run':body.get('run'),'status':row['status'],'reason':row['reason']})
    return {'certain_recalls':certain,'possible_impact':uncertain,
            'feed_attributions':attributions,'untrusted_feed':untrusted}


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
              'impact':impact(db)}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
