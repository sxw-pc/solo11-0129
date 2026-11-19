"""Independent inspection of desktop material batch measurements."""
import json
from .ingest import capture
from .store import connect, save, decide


def latest(db, lot):
    return db.execute("SELECT * FROM lot_inspection WHERE json_extract(body,'$.lot')=? ORDER BY rowid DESC LIMIT 1",
                      (str(lot),)).fetchone()


def assess(path, file):
    source, payload = capture(path, 'desktop_lot_inspection', file)
    db = connect(path)
    results = []
    try:
        with db:
            for row in payload.get('rows', []):
                key = str(row['id'])
                lot = str(row['lot'])
                old = db.execute('SELECT * FROM lot_inspection WHERE id=?', (key,)).fetchone()
                if old:
                    if old['body'] != json.dumps(row, sort_keys=True, ensure_ascii=False):
                        raise ValueError('inspection_identity_conflict')
                    results.append(decide(db, '材料批次质检', key, old['status'], old['reason'], source))
                    continue
                previous = latest(db, lot)
                stock = db.execute('SELECT 1 FROM material_lot WHERE id=?', (lot,)).fetchone()
                diameter = row.get('diameter_samples', [])
                if not stock: status, reason = '待核', 'lot_missing'
                elif not diameter: status, reason = '待核', 'diameter_samples_missing'
                elif max(abs(float(x) - float(row['nominal_diameter'])) for x in diameter) > float(row['diameter_tolerance']):
                    status, reason = '隔离', 'diameter_out_of_tolerance'
                elif float(row['moisture']) > float(row['max_moisture']):
                    status, reason = '隔离', 'moisture_exceeded'
                else: status, reason = '合格', 'lot_qualified'
                if previous and previous['status'] == '隔离' and status == '合格' and not row.get('release_authority'):
                    status, reason = '隔离', 'release_authority_missing'
                save(db, 'lot_inspection', key, source, payload['version'], row, status, reason)
                if status == '隔离':
                    parts = db.execute("SELECT p.* FROM print_part p JOIN reservation r ON "
                                       "json_extract(p.body,'$.reservation')=r.id "
                                       "WHERE json_extract(r.body,'$.lot')=? AND p.status='已验收'", (lot,)).fetchall()
                    for part in parts:
                        db.execute("UPDATE print_part SET status='撤回',reason='lot_quarantined' WHERE id=?", (part['id'],))
                        save(db, 'part_recall', f'{key}:{part["id"]}', source, payload['version'],
                             {'inspection': key, 'lot': lot, 'part': part['id']}, '待通知', 'accepted_part_recalled')
                results.append(decide(db, '材料批次质检', key, status, reason, source))
        return results
    finally:
        db.close()
