import json
import pytest
from sliceledger import operations,report,production,inventory,lot_quality
from sliceledger.store import connect

def write(root,name,obj):
    p=root/name;p.write_text(json.dumps(obj),encoding='utf-8');return p
def job(id='J1',material='PLA',size=None,layer=0.2):
    return dict(id=id,machine='M1',size=size or [20,20,20],build_limit=[200,200,200],
                nozzle=0.4,layer=layer,material=material,grams=15)
def completed_run(root,db):
    events=[{'seq':1,'state':'queued','at':'2026-01-01T10:00:00'},
            {'seq':2,'state':'running','at':'2026-01-01T10:01:00'},
            {'seq':3,'state':'completed','at':'2026-01-01T11:00:00'}]
    return production.import_runs(db,write(root,'run.json',{'version':'v1','rows':[
        {'id':'RUN1','job':'J1','reservation':'A1','machine':'M1','events':events}]}))

def test_slice_material_part_full_chain(tmp_path):
    result=operations.demo(tmp_path)
    assert [x[0]['status'] for x in result]==['可开机候选','已预留','已验收']
    conn=connect(tmp_path/'ledger.db')
    assert conn.execute("SELECT status FROM reservation WHERE id='A1'").fetchone()[0]=='已结算'
    assert all(x['verified'] for x in json.loads(report.build(tmp_path/'ledger.db',tmp_path/'report.json').read_text(encoding='utf-8'))['sources'])
    assert inventory.balance(tmp_path/'ledger.db','L1')['available']==82

def test_machine_volume_layer_and_material_estimate(tmp_path):
    db=tmp_path/'db';rows=[job('J1'),job('J2',size=[300,20,20]),job('J3',layer=0.4)]
    result=operations.first(db,write(tmp_path,'slice.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['profile_compatible','build_volume_exceeded','layer_nozzle_mismatch']

def test_material_mismatch_and_shared_inventory_budget(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'slice.json',{'version':'v1','rows':[job('J1'),job('J2')]}))
    rows=[{'id':'A1','job':'J1','lot':'L1','material':'PLA','available_grams':30,'grams':20},
          {'id':'A2','job':'J2','lot':'L1','material':'PLA','available_grams':30,'grams':20},
          {'id':'A3','job':'J2','lot':'L2','material':'ABS','available_grams':100,'grams':20}]
    result=operations.second(db,write(tmp_path,'material.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['inventory_reserved','insufficient_inventory','material_mismatch']

def test_dimension_and_consumption_change_acceptance(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'slice.json',{'version':'v1','rows':[job()]}))
    operations.second(db,write(tmp_path,'material.json',{'version':'v1','rows':[
        {'id':'A1','job':'J1','lot':'L1','material':'PLA','available_grams':100,'grams':20}]}))
    completed_run(tmp_path,db)
    rows=[{'id':'P1','job':'J1','reservation':'A1','run_id':'RUN1','measured':[22],'nominal':[20],'tolerance':0.2,'used_grams':18},
          {'id':'P2','job':'J1','reservation':'A1','run_id':'RUN1','measured':[20],'nominal':[20],'tolerance':0.2,'used_grams':25}]
    result=operations.third(db,write(tmp_path,'parts.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['dimension_out_of_tolerance','consumption_exceeded']
    assert connect(db).execute("SELECT status FROM reservation WHERE id='A1'").fetchone()[0]=='已预留'

def test_part_cannot_accept_unqualified_slice(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'slice.json',{'version':'v1','rows':[job(size=[300,20,20])]}))
    operations.second(db,write(tmp_path,'material.json',{'version':'v1','rows':[
        {'id':'A1','job':'J1','lot':'L1','material':'PLA','available_grams':100,'grams':20}]}))
    result=operations.third(db,write(tmp_path,'parts.json',{'version':'v1','rows':[
        {'id':'P1','job':'J1','reservation':'A1','measured':[20],'nominal':[20],'tolerance':0.2,'used_grams':18}]}))
    assert result[0]['reason']=='upstream_not_ready'

def test_machine_interruption_blocks_part_until_verified_completion(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'slice.json',{'version':'v1','rows':[job()]}))
    operations.second(db,write(tmp_path,'material.json',{'version':'v1','rows':[
        {'id':'A1','job':'J1','lot':'L1','material':'PLA','available_grams':100,'grams':20}]}))
    interrupted={'version':'v1','rows':[{'id':'RUN1','job':'J1','reservation':'A1','machine':'M1','events':[
        {'seq':1,'state':'queued','at':'2026-01-01T10:00:00'},
        {'seq':2,'state':'running','at':'2026-01-01T10:01:00'},
        {'seq':3,'state':'interrupted','at':'2026-01-01T10:02:00'}]}]}
    assert production.import_runs(db,write(tmp_path,'run.json',interrupted))[0]['status']=='中断待续'
    part={'version':'v1','rows':[{'id':'P1','job':'J1','reservation':'A1','run_id':'RUN1',
        'measured':[20],'nominal':[20],'tolerance':0.2,'used_grams':18}]}
    assert operations.third(db,write(tmp_path,'part.json',part))[0]['reason']=='machine_run_unverified'
    assert inventory.balance(db,'L1')['reserved']==20

def test_machine_event_order_and_profile_mismatch(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'slice.json',{'version':'v1','rows':[job()]}))
    operations.second(db,write(tmp_path,'material.json',{'version':'v1','rows':[
        {'id':'A1','job':'J1','lot':'L1','material':'PLA','available_grams':100,'grams':20}]}))
    rows=[{'id':'R1','job':'J1','reservation':'A1','machine':'wrong','events':[{'seq':1,'state':'queued','at':'2026-01-01T10:00:00'}]},
          {'id':'R2','job':'J1','reservation':'A1','machine':'M1','events':[{'seq':1,'state':'queued','at':'2026-01-01T10:00:00'},
             {'seq':3,'state':'completed','at':'2026-01-01T10:01:00'}]}]
    assert [x['reason'] for x in production.import_runs(db,write(tmp_path,'run.json',{'version':'v1','rows':rows}))]==['machine_mismatch','event_order_conflict']

def test_consumed_stock_still_reduces_next_job_capacity(tmp_path):
    operations.demo(tmp_path);db=tmp_path/'ledger.db'
    operations.first(db,write(tmp_path,'slice2.json',{'version':'v1','rows':[job('J2')]}))
    result=operations.second(db,write(tmp_path,'material2.json',{'version':'v1','rows':[
        {'id':'A2','job':'J2','lot':'L1','material':'PLA','available_grams':100,'grams':85}]}))
    assert result[0]['reason']=='insufficient_inventory'
    assert inventory.balance(db,'L1')['consumed']==18

def test_inventory_snapshot_conflict_is_not_silent_overwrite(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'slice.json',{'version':'v1','rows':[job()]}))
    operations.second(db,write(tmp_path,'material.json',{'version':'v1','rows':[
        {'id':'A1','job':'J1','lot':'L1','material':'PLA','available_grams':100,'grams':20}]}))
    result=operations.second(db,write(tmp_path,'changed.json',{'version':'v1','rows':[
        {'id':'A2','job':'J1','lot':'L1','material':'PLA','available_grams':200,'grams':20}]}))
    assert result[0]['reason']=='inventory_snapshot_conflict'
    assert inventory.balance(db,'L1')['initial']==100

def test_reservation_and_acceptance_replay_do_not_double_consume(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'slice.json',{'version':'v1','rows':[job()]}))
    material=write(tmp_path,'material.json',{'version':'v1','rows':[
        {'id':'A1','job':'J1','lot':'L1','material':'PLA','available_grams':100,'grams':20}]})
    operations.second(db,material);operations.second(db,material)
    assert inventory.balance(db,'L1')['reserved']==20
    completed_run(tmp_path,db)
    part=write(tmp_path,'part.json',{'version':'v1','rows':[{'id':'P1','job':'J1','reservation':'A1','run_id':'RUN1',
        'measured':[20],'nominal':[20],'tolerance':0.2,'used_grams':18}]})
    assert operations.third(db,part)==operations.third(db,part)
    assert inventory.balance(db,'L1')['consumed']==18
    assert connect(db).execute('SELECT count(*) FROM material_movement').fetchone()[0]==1

def test_completed_measurement_without_machine_completion_is_not_accepted(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'slice.json',{'version':'v1','rows':[job()]}))
    operations.second(db,write(tmp_path,'material.json',{'version':'v1','rows':[
        {'id':'A1','job':'J1','lot':'L1','material':'PLA','available_grams':100,'grams':20}]}))
    result=operations.third(db,write(tmp_path,'part.json',{'version':'v1','rows':[
        {'id':'P1','job':'J1','reservation':'A1','measured':[20],'nominal':[20],
         'tolerance':0.2,'used_grams':18}]}))
    assert result[0]['reason']=='machine_run_unverified'
    assert inventory.balance(db,'L1')['reserved']==20

def inspection(id='Q1',moisture=0.02,diameter=None,authority=None):
    row={'id':id,'lot':'L1','nominal_diameter':1.75,'diameter_tolerance':0.05,
         'diameter_samples':diameter or [1.74,1.76],'moisture':moisture,'max_moisture':0.05}
    if authority:row['release_authority']=authority
    return row

def test_lot_quarantine_blocks_machine_and_new_reservation(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'slice.json',{'version':'v1','rows':[job(),job('J2')]}))
    operations.second(db,write(tmp_path,'material.json',{'version':'v1','rows':[
        {'id':'A1','job':'J1','lot':'L1','material':'PLA','available_grams':100,'grams':20}]}))
    assert lot_quality.assess(db,write(tmp_path,'inspect.json',{'version':'v1','rows':[
        inspection(moisture=0.1)]}))[0]['reason']=='moisture_exceeded'
    assert inventory.balance(db,'L1')['available']==80
    assert inventory.balance(db,'L1')['usable']==0
    assert completed_run(tmp_path,db)[0]['reason']=='lot_quarantined'
    result=operations.second(db,write(tmp_path,'next.json',{'version':'v1','rows':[
        {'id':'A2','job':'J2','lot':'L1','material':'PLA','available_grams':100,'grams':20}]}))
    assert result[0]['reason']=='lot_quarantined'
    assert lot_quality.assess(db,write(tmp_path,'release.json',{'version':'v1','rows':[
        inspection('Q2')]}))[0]['reason']=='release_authority_missing'
    assert lot_quality.assess(db,write(tmp_path,'authorized.json',{'version':'v1','rows':[
        inspection('Q3',authority='quality-lead')]}))[0]['status']=='合格'
    assert inventory.balance(db,'L1')['usable']==80

def test_late_lot_inspection_recalls_accepted_part_without_refunding_material(tmp_path):
    operations.demo(tmp_path);db=tmp_path/'ledger.db'
    file=write(tmp_path,'inspect.json',{'version':'v1','rows':[inspection(diameter=[1.8,1.9])]})
    assert lot_quality.assess(db,file)==lot_quality.assess(db,file)
    conn=connect(db)
    assert conn.execute("SELECT status FROM print_part WHERE id='P1'").fetchone()[0]=='撤回'
    assert conn.execute('SELECT count(*) FROM part_recall').fetchone()[0]==1
    assert inventory.balance(db,'L1')['consumed']==18
    assert inventory.balance(db,'L1')['usable']==0

def test_same_job_identity_with_changed_desktop_export_is_rejected(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'slice.json',{'version':'v1','rows':[job()]}))
    with pytest.raises(ValueError,match='identity_conflict'):
        operations.first(db,write(tmp_path,'changed.json',{'version':'v1','rows':[job(size=[25,20,20])]}))
    conn=connect(db)
    assert conn.execute('SELECT count(*) FROM export_file').fetchone()[0]==2
    assert json.loads(conn.execute("SELECT body FROM slice_export WHERE id='J1'").fetchone()[0])['size']==[20,20,20]
