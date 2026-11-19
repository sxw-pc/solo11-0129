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
    from .lot_quality import latest
    inspection=latest(db,lot)
    available=initial-consumed-reserved
    return {'lot':lot,'initial':initial,'consumed':consumed,'reserved':reserved,
            'available':available,'usable':available if not inspection or inspection['status']=='合格' else 0}


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


def settle_attribution(db,part_id,feed_id,source,version):
    """Settle one movement per roll from the trusted layer-level attribution.

    Idempotent across replays and across superseding feed evidence: old movements of
    the part are removed first, so the same export never deducts twice and the final
    balances always equal the newest trusted attribution.
    """
    from .feed import trusted_feed
    usage_row=db.execute('SELECT * FROM lot_usage WHERE json_extract(body,\'$.part\')=? ORDER BY rowid DESC LIMIT 1',
                         (str(part_id),)).fetchone()
    if not usage_row:
        raise ValueError('attribution_missing')
    usage=json.loads(usage_row['body'])
    part=db.execute('SELECT * FROM print_part WHERE id=?',(str(part_id),)).fetchone()
    reservation_id=json.loads(part['body'])['reservation'] if part else usage.get('reservation')
    reservation=db.execute('SELECT * FROM reservation WHERE id=?',(str(reservation_id),)).fetchone()
    if not reservation or reservation['status'] not in ('已预留','已结算'):
        raise ValueError('reservation_not_open')
    reserved=json.loads(reservation['body']);reserved_grams=float(reserved['grams'])
    on_reserved=sum(float(x['grams']) for x in usage['usage'] if str(x['lot'])==str(reserved['lot']))
    if on_reserved>reserved_grams+1e-6:
        raise ValueError('consumption_exceeded')
    db.execute("DELETE FROM material_movement WHERE json_extract(body,'$.part')=?",(str(part_id),))
    movements=[]
    for item in usage['usage']:
        lot=str(item['lot']);used=float(item['grams'])
        released=round(reserved_grams-used,9) if lot==str(reserved['lot']) else 0.0
        movement={'part':part_id,'reservation':reservation_id,'lot':lot,'feed':feed_id,
                  'used_grams':round(used,9),'released_grams':released}
        save(db,'material_movement',f'movement:{part_id}:{lot}',source,version,
             movement,'已结算','attributed_consumption_recorded')
        movements.append(movement)
    db.execute("UPDATE reservation SET status='已结算',reason='unused_released' WHERE id=?",(str(reservation_id),))
    return movements
