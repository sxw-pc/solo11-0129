import sqlite3
from pathlib import Path
TABLES=['slice_export', 'machine_profile', 'material_lot', 'reservation', 'print_part', 'measurement', 'material_movement', 'machine_run', 'machine_event', 'lot_inspection', 'part_recall', 'coupon_scan']
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS export_file(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,version TEXT NOT NULL,hash TEXT NOT NULL,body BLOB NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(kind,hash));
CREATE TABLE IF NOT EXISTS export_record(id INTEGER PRIMARY KEY,source_id INT NOT NULL REFERENCES export_file(id),line_no INT NOT NULL,identity TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,source_id INT REFERENCES export_file(id),code TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,subject TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,source_id INT REFERENCES export_file(id),created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(subsystem,subject,status,reason,source_id));
CREATE TABLE IF NOT EXISTS slice_export(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS machine_profile(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS material_lot(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reservation(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS print_part(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurement(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS material_movement(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS machine_run(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS machine_event(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lot_inspection(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS part_recall(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coupon_scan(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db
def save(db,table,key,source_id,version,body,status,reason):
    import json
    if table not in TABLES:raise ValueError('unknown_table')
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False)
    old=db.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()
    if old and (old['version'],old['body'])!=(version,encoded):
        db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',(source_id,'identity_conflict',f'{table}:{key}'))
        raise ValueError('identity_conflict')
    db.execute(f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason',
               (key,source_id,version,encoded,status,reason))
def decide(db,subsystem,subject,status,reason,source_id):
    db.execute('INSERT OR IGNORE INTO decision_log(subsystem,subject,status,reason,source_id) VALUES (?,?,?,?,?)',
               (subsystem,subject,status,reason,source_id))
    return {'subject':subject,'status':status,'reason':reason}

