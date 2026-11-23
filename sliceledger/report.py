import hashlib,json
from pathlib import Path
from .store import connect,TABLES


def _maintenance_summary(db):
    """Current suggestion basis plus the retained history.

    Full history of diagnoses and plan revisions stays in ``entities``; this
    section is the review view a duty officer reads first.  Old plans and raw
    alarms are never summarised away.
    """
    summary={}
    commission=db.execute("SELECT * FROM maintenance_commission WHERE status='已委托' "
                          "ORDER BY rowid DESC LIMIT 1").fetchone()
    if commission:
        body=json.loads(commission['body'])
        summary['commission']={'id':commission['id'],
                               'kind':body.get('commission'),
                               'authority':body.get('authority')}
    diagnosis=db.execute('SELECT * FROM maintenance_diagnosis ORDER BY rowid DESC LIMIT 1').fetchone()
    if diagnosis:
        d=json.loads(diagnosis['body'])
        summary['diagnosis']={'id':d.get('id'),'revision':d.get('revision'),
                              'state':d.get('state'),'goal_met':d.get('goal_met'),
                              'candidates':d.get('candidates',[]),
                              'eliminated':d.get('eliminated',[]),
                              'retained_uncertainty':d.get('retained_uncertainty',[]),
                              'plan_cost':d.get('plan_cost'),
                              'machine_structure':d.get('machine_structure'),
                              'authorises_release':False}
    latest_plan=db.execute('SELECT * FROM maintenance_plan ORDER BY rowid DESC LIMIT 1').fetchone()
    if latest_plan:
        anchor=json.loads(latest_plan['body'])
        key=(anchor.get('commission'),anchor.get('revision'))
        plans=[json.loads(row['body']) for row in db.execute(
            "SELECT * FROM maintenance_plan WHERE json_extract(body,'$.commission')=? "
            "AND json_extract(body,'$.revision')=? ORDER BY rowid",
            (key[0],key[1]))]
        summary['plans']=[{'id':p.get('id'),'revision':p.get('revision'),
                           'next_test':p.get('next_test'),
                           'worst_minutes':p.get('worst_minutes'),
                           'risk':p.get('risk'),
                           'variant':p.get('variant'),
                           'variant_count':p.get('variant_count'),
                           'precondition':p.get('next_precondition'),
                           'branches':[{'outcome':b.get('outcome'),
                                        'candidates':b.get('candidates'),
                                        'next_test':b.get('next',{}).get('test'),
                                        'precondition':b.get('next',{}).get('precondition')}
                                       for b in (p.get('tree') or {}).get('branches',[])]
                           if p.get('tree',{}).get('kind')!='leaf' else [],
                           'authorises_release':False} for p in plans]
    executions=[json.loads(row['body']) for row in db.execute(
        'SELECT * FROM test_execution ORDER BY rowid')]
    summary['executions']=[{'id':e.get('id'),'test':e.get('test'),
                            'result':e.get('result'),'machine_state':e.get('machine_state'),
                            'isolated_steps':e.get('isolated_steps'),
                            'at':e.get('at'),'source':e.get('source')}
                           for e in executions]
    return summary


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
              'maintenance':_maintenance_summary(db)}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
