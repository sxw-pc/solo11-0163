import sqlite3

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS garment(id TEXT PRIMARY KEY,owner TEXT NOT NULL,material TEXT NOT NULL,accepted_at TEXT NOT NULL,declared_value INTEGER NOT NULL,cap_rate INTEGER NOT NULL,allowed_process TEXT NOT NULL,pickup_person TEXT NOT NULL,claim_until TEXT NOT NULL,expected_bag TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intake_photo(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,version INTEGER NOT NULL,photo_sha TEXT NOT NULL,damage TEXT NOT NULL,owner_signer TEXT NOT NULL,clerk_signer TEXT NOT NULL,material TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intake_decision(garment_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,photo_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS recipe(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,version INTEGER NOT NULL,process TEXT NOT NULL,max_temp INTEGER NOT NULL,banned_chemicals TEXT NOT NULL,approved_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operation(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,recipe_id TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,temp INTEGER NOT NULL,chemical TEXT NOT NULL,from_shop TEXT NOT NULL,to_shop TEXT NOT NULL,sender TEXT,receiver TEXT);
CREATE TABLE IF NOT EXISTS process_decision(garment_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,recipe_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pickup_authority(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,person TEXT NOT NULL,owner_signer TEXT NOT NULL,valid_to TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS return_receipt(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,person TEXT NOT NULL,at TEXT NOT NULL,bag TEXT NOT NULL,condition TEXT NOT NULL,owner_signer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claim(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,receipt_id TEXT NOT NULL,at TEXT NOT NULL,photo_sha TEXT NOT NULL,damage TEXT NOT NULL,requested_amount INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS compensation_decision(garment_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,payable INTEGER NOT NULL,frozen INTEGER NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,garment_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS care_order(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL UNIQUE,owner TEXT NOT NULL,allow_split INTEGER NOT NULL,original_bag TEXT NOT NULL,original_seal TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_member(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,garment_id TEXT NOT NULL,UNIQUE(order_id,garment_id));
CREATE TABLE IF NOT EXISTS order_split(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,garment_id TEXT NOT NULL,from_bag TEXT NOT NULL,to_bag TEXT NOT NULL,new_seal TEXT NOT NULL,at TEXT NOT NULL,requested_by TEXT NOT NULL,owner_signer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_handoff(event_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,bag TEXT NOT NULL,seal TEXT NOT NULL,person TEXT NOT NULL,at TEXT NOT NULL,condition TEXT NOT NULL,owner_signer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bag_membership(id INTEGER PRIMARY KEY,order_id TEXT NOT NULL,bag TEXT NOT NULL,garment_id TEXT NOT NULL,from_bag TEXT,split_event TEXT NOT NULL,at TEXT NOT NULL,UNIQUE(order_id,bag,garment_id,split_event));
CREATE TABLE IF NOT EXISTS delivery_decision(garment_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,bag TEXT,receipt_id TEXT,claim_frozen INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS order_decision(order_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,delivered TEXT NOT NULL,undelivered TEXT NOT NULL,frozen TEXT NOT NULL,disputes TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS order_decision_history(id INTEGER PRIMARY KEY,order_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db


def update(db,table,garment_id,**fields):
    old=db.execute(f'SELECT * FROM {table} WHERE garment_id=?',(garment_id,)).fetchone()
    if old and all(old[key]==value for key,value in fields.items()):
        return dict(old)
    version=(old['version']+1) if old else 1
    names=['garment_id',*fields,'version']
    values=[garment_id,*fields.values(),version]
    sql=f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)}) ON CONFLICT(garment_id) DO UPDATE SET "
    sql+=','.join(f'{name}=excluded.{name}' for name in [*fields,'version'])
    db.execute(sql,values)
    db.execute('INSERT INTO decision_history(subsystem,garment_id,version,state,reason) VALUES(?,?,?,?,?)',
               (table,garment_id,version,fields['state'],fields['reason']))
    return dict(db.execute(f'SELECT * FROM {table} WHERE garment_id=?',(garment_id,)).fetchone())


def update_order(db,order_id,**fields):
    """Upsert the order-level decision; text-set fields are stored canonically sorted."""
    old=db.execute('SELECT * FROM order_decision WHERE order_id=?',(order_id,)).fetchone()
    if old and all(old[key]==value for key,value in fields.items()):
        return dict(old)
    version=(old['version']+1) if old else 1
    names=['order_id',*fields,'version']
    values=[order_id,*fields.values(),version]
    sql=f"INSERT INTO order_decision({','.join(names)}) VALUES({','.join('?' for _ in names)}) ON CONFLICT(order_id) DO UPDATE SET "
    sql+=','.join(f'{name}=excluded.{name}' for name in [*fields,'version'])
    db.execute(sql,values)
    db.execute('INSERT INTO order_decision_history(order_id,version,state,reason) VALUES(?,?,?,?)',
               (order_id,version,fields['state'],fields['reason']))
    return dict(db.execute('SELECT * FROM order_decision WHERE order_id=?',(order_id,)).fetchone())


def orders_for_garment(db,garment_id):
    """Order ids whose fixed membership roster names this garment (ordered)."""
    return [row['order_id'] for row in db.execute(
        'SELECT order_id FROM order_member WHERE garment_id=? ORDER BY order_id',(garment_id,))]
