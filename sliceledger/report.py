import hashlib,json
from pathlib import Path
from .store import connect,TABLES,body_of
def build(path,out):
    db=connect(path)
    try:
        sources=[]
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x=dict(item);x.pop('body')
            x['verified']=hashlib.sha256(item['body']).hexdigest()==item['hash']
            sources.append(x)
        data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
              'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')]}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        data['load_ratings']=_load_rating_summary(db)
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()

def _load_rating_summary(db):
    """Current load promises with the withdrawal reasons of older promises."""
    summaries=[]
    for row in db.execute('SELECT * FROM load_rating ORDER BY id'):
        basis=body_of(row)
        contracts={}
        for cid,result in basis.get('contracts',{}).items():
            contracts[cid]={
                'certified_load_n':result.get('certified_load_n'),
                'certified_levels_n':result.get('certified_levels_n'),
                'follow_up_tests':result.get('follow_up_tests'),
            }
        summaries.append({
            'rating_id':row['id'],
            'model':basis.get('model'),
            'calibration_id':basis.get('calibration_id'),
            'status':row['status'],
            'reason':row['reason'],
            'contracts':contracts,
            'withdrawals':basis.get('withdrawals',[]),
        })
    # Withdrawn promises whose calibration was replaced: surface their reasons
    # from the decision log even when their rating row has been superseded.
    withdrawn=[]
    for d in db.execute("SELECT * FROM decision_log WHERE subsystem='载荷额定签发' AND status='已撤回' ORDER BY id"):
        withdrawn.append({'rating_id':d['subject'],'reason':d['reason']})
    for item in summaries:
        item['withdrawn_promises']=[w for w in withdrawn
                                    if w['rating_id'].split(':')[0] ==
                                    item['rating_id'].split(':')[0]]
    return summaries
