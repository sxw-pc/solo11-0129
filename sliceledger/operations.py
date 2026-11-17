"""Slicer export qualification, material allocation, and part acceptance."""
import json
from .ingest import capture
from .store import connect,save,decide


def first(path,file):
    source,payload=capture(path,'desktop_slice_export',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);size=row['size'];limit=row['build_limit']
                if any(float(s)>float(l) or float(s)<=0 for s,l in zip(size,limit)) or len(size)!=3:status,reason='待复切','build_volume_exceeded'
                elif float(row['layer'])<=0 or float(row['layer'])>float(row['nozzle'])*0.8:status,reason='待复切','layer_nozzle_mismatch'
                elif float(row['grams'])<=0:status,reason='待复切','invalid_material_estimate'
                else:status,reason='可开机候选','profile_compatible'
                save(db,'machine_profile',str(row['machine']),source,payload['version'],
                     {'machine':row['machine'],'build_limit':limit,'nozzle':row['nozzle']},'已登记','desktop_profile')
                save(db,'slice_export',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'切片作业',key,status,reason,source))
        return results
    finally:db.close()


def second(path,file):
    source,payload=capture(path,'desktop_material_inventory',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);job=db.execute('SELECT * FROM slice_export WHERE id=?',(str(row['job']),)).fetchone()
                job_data=json.loads(job['body']) if job else {}
                existing_lot=db.execute('SELECT * FROM material_lot WHERE id=?',(str(row['lot']),)).fetchone()
                if not existing_lot:
                    save(db,'material_lot',str(row['lot']),source,payload['version'],
                         {'lot':row['lot'],'material':row['material'],'available_grams':row['available_grams']},'已登记','inventory_snapshot')
                snapshot_conflict=bool(existing_lot and existing_lot['body']!=json.dumps(
                    {'lot':row['lot'],'material':row['material'],'available_grams':row['available_grams']},sort_keys=True,ensure_ascii=False))
                from .inventory import balance_db
                available=balance_db(db,str(row['lot']))['available']
                if snapshot_conflict:status,reason='待核','inventory_snapshot_conflict'
                elif not job or job['status']!='可开机候选':status,reason='待核','slice_unqualified'
                elif row['material']!=job_data['material']:status,reason='待换料','material_mismatch'
                elif float(row['grams'])>available or float(row['grams'])<float(job_data['grams']):status,reason='待换料','insufficient_inventory'
                else:status,reason='已预留','inventory_reserved'
                save(db,'reservation',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'材料预留',key,status,reason,source))
        return results
    finally:db.close()


def third(path,file):
    source,payload=capture(path,'desktop_print_completion',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);job=db.execute('SELECT status FROM slice_export WHERE id=?',(str(row['job']),)).fetchone()
                old=db.execute('SELECT * FROM print_part WHERE id=?',(key,)).fetchone()
                if old and old['body']==json.dumps(row,sort_keys=True,ensure_ascii=False):
                    results.append(decide(db,'制件验收',key,old['status'],old['reason'],source));continue
                reserve=db.execute('SELECT * FROM reservation WHERE id=?',(str(row['reservation']),)).fetchone()
                reserved=json.loads(reserve['body']) if reserve else {}
                measured=row.get('measured',[]);nominal=row.get('nominal',[])
                run_required=bool(db.execute('SELECT 1 FROM machine_run WHERE json_extract(body,\'$.job\')=?',(str(row['job']),)).fetchone())
                run=db.execute('SELECT * FROM machine_run WHERE id=?',(str(row.get('run_id','')),)).fetchone()
                if not job or job['status']!='可开机候选' or not reserve or reserve['status']!='已预留':status,reason='待核','upstream_not_ready'
                elif run_required and (not run or run['status']!='已完成' or
                      json.loads(run['body'])['job']!=row['job'] or
                      json.loads(run['body'])['reservation']!=row['reservation']):status,reason='待核','machine_run_unverified'
                elif len(measured)!=len(nominal) or not measured:status,reason='待核','measurement_missing'
                elif any(abs(float(m)-float(n))>float(row['tolerance']) for m,n in zip(measured,nominal)):status,reason='拒收','dimension_out_of_tolerance'
                elif float(row['used_grams'])>float(reserved['grams']):status,reason='待核','consumption_exceeded'
                else:status,reason='已验收','part_accepted'
                save(db,'print_part',key,source,payload['version'],row,status,reason)
                save(db,'measurement',f'measure:{key}',source,payload['version'],
                     {'measured':measured,'nominal':nominal,'tolerance':row['tolerance']},status,reason)
                if status=='已验收':
                    from .inventory import settle
                    settle(db,str(row['reservation']),row['used_grams'],key,source,payload['version'])
                results.append(decide(db,'制件验收',key,status,reason,source))
        return results
    finally:db.close()


def demo(root):
    db=root/'ledger.db';files=[
        {'version':'v1','rows':[{'id':'J1','machine':'M1','size':[20,20,20],'build_limit':[200,200,200],'nozzle':0.4,'layer':0.2,'material':'PLA','grams':15}]},
        {'version':'v1','rows':[{'id':'A1','job':'J1','lot':'L1','material':'PLA','available_grams':100,'grams':20}]},
        {'version':'v1','rows':[{'id':'P1','job':'J1','reservation':'A1','run_id':'RUN1','measured':[20.1,20,19.9],'nominal':[20,20,20],'tolerance':0.2,'used_grams':18}]}]
    out=[]
    for i,(fn,item) in enumerate(zip((first,second,third),files)):
        if i==2:
            from .production import import_runs
            runfile=root/'machine.json';runfile.write_text(json.dumps({'version':'v1','rows':[{'id':'RUN1','job':'J1','reservation':'A1','machine':'M1',
                'events':[{'seq':1,'state':'queued','at':'2026-01-01T10:00:00'},
                          {'seq':2,'state':'running','at':'2026-01-01T10:01:00'},
                          {'seq':3,'state':'completed','at':'2026-01-01T11:00:00'}]}]}),encoding='utf-8')
            import_runs(db,runfile)
        p=root/f'export{i}.json';p.write_text(json.dumps(item),encoding='utf-8');out.append(fn(db,p))
    return out
