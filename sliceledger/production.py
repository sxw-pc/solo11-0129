"""Desktop printer event sequence and evidence based run completion."""
import json
from datetime import datetime
from .ingest import capture
from .store import connect,save,decide

TRANSITIONS={'queued':{'running'},'running':{'interrupted','completed'},
             'interrupted':{'resumed'},'resumed':{'interrupted','completed'},'completed':set()}


def import_runs(path,file):
    source,payload=capture(path,'desktop_machine_queue',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);job=db.execute('SELECT * FROM slice_export WHERE id=?',(str(row['job']),)).fetchone()
                reservation=db.execute('SELECT * FROM reservation WHERE id=?',(str(row['reservation']),)).fetchone()
                events=row.get('events',[]);state=None;reason='run_completed'
                if not job or job['status']!='可开机候选' or not reservation or reservation['status']!='已预留':
                    status,reason='待核','upstream_not_ready'
                elif row.get('machine')!=json.loads(job['body'])['machine']:status,reason='待核','machine_mismatch'
                elif not events or events[0].get('state')!='queued':status,reason='待核','missing_queue_event'
                else:
                    status='已完成';previous_time=None;expected_seq=1
                    for event in events:
                        at=datetime.fromisoformat(event['at'])
                        if event['seq']!=expected_seq or (previous_time and at<previous_time):
                            status,reason='待核','event_order_conflict';break
                        if state is not None and event['state'] not in TRANSITIONS[state]:
                            status,reason='待核','invalid_transition';break
                        state=event['state'];previous_time=at;expected_seq+=1
                    if status=='已完成' and state!='completed':status,reason='中断待续','run_not_completed'
                for event in events:
                    save(db,'machine_event',f"{key}:{event['seq']}",source,payload['version'],event,status,reason)
                save(db,'machine_run',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'机台执行',key,status,reason,source))
        return results
    finally:db.close()
