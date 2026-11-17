"""Lot balances across reservations, consumption and released remainder."""
import json
from .store import connect,save


def balance_db(db,lot):
    stock=db.execute('SELECT * FROM material_lot WHERE id=?',(str(lot),)).fetchone()
    if not stock:raise ValueError('unknown_lot')
    initial=float(json.loads(stock['body'])['available_grams'])
    consumed=sum(float(json.loads(x['body'])['used_grams']) for x in db.execute('SELECT body FROM material_movement')
                 if json.loads(x['body'])['lot']==lot)
    reserved=sum(float(json.loads(x['body'])['grams']) for x in db.execute("SELECT body FROM reservation WHERE status='已预留'")
                 if json.loads(x['body'])['lot']==lot)
    return {'lot':lot,'initial':initial,'consumed':consumed,'reserved':reserved,
            'available':initial-consumed-reserved}


def balance(path,lot):
    db=connect(path)
    try:return balance_db(db,lot)
    finally:db.close()


def settle(db,reservation_id,used_grams,part_id,source,version):
    reservation=db.execute('SELECT * FROM reservation WHERE id=?',(str(reservation_id),)).fetchone()
    if not reservation or reservation['status']!='已预留':raise ValueError('reservation_not_open')
    details=json.loads(reservation['body'])
    used=float(used_grams);reserved=float(details['grams'])
    if used<0 or used>reserved:raise ValueError('consumption_exceeded')
    movement={'part':part_id,'reservation':reservation_id,'lot':details['lot'],
              'used_grams':used,'released_grams':reserved-used}
    save(db,'material_movement',f'movement:{part_id}',source,version,movement,'已结算','consumption_recorded')
    db.execute("UPDATE reservation SET status='已结算',reason='unused_released' WHERE id=?",(str(reservation_id),))
    return movement
