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
CREATE TABLE IF NOT EXISTS solvent_route(event_id TEXT PRIMARY KEY,route_id TEXT NOT NULL,kind TEXT NOT NULL,approved_by TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS solvent_barrel(event_id TEXT PRIMARY KEY,barrel_id TEXT NOT NULL,batch TEXT NOT NULL,chemical TEXT NOT NULL,role TEXT NOT NULL,liters REAL NOT NULL,weight_kg REAL NOT NULL,density_kg_per_l REAL NOT NULL,meter_id TEXT NOT NULL,at TEXT NOT NULL,supplier TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS solvent_draw(event_id TEXT PRIMARY KEY,barrel_id TEXT NOT NULL,operation_id TEXT NOT NULL,liters REAL NOT NULL,meter_id TEXT NOT NULL,at TEXT NOT NULL,shop TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS solvent_reflux(event_id TEXT PRIMARY KEY,barrel_id TEXT NOT NULL,operation_id TEXT NOT NULL,liters REAL NOT NULL,meter_id TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS solvent_distillation(event_id TEXT PRIMARY KEY,route_id TEXT NOT NULL,input_barrel_id TEXT NOT NULL,output_barrel_id TEXT NOT NULL,input_liters REAL NOT NULL,reusable_liters REAL NOT NULL,residue_liters REAL NOT NULL,meter_id TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS solvent_handover(event_id TEXT PRIMARY KEY,barrel_id TEXT NOT NULL,route_id TEXT NOT NULL,liters REAL NOT NULL,sealed_weight_kg REAL NOT NULL,carrier TEXT NOT NULL,receiver TEXT NOT NULL,meter_id TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS solvent_waste_receipt(event_id TEXT PRIMARY KEY,handover_id TEXT NOT NULL,status TEXT NOT NULL,received_liters REAL,received_weight_kg REAL,at TEXT NOT NULL,signer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS solvent_calibration(event_id TEXT PRIMARY KEY,meter_id TEXT NOT NULL,factor REAL NOT NULL,effective_from TEXT NOT NULL,issued_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS solvent_stock_check(event_id TEXT PRIMARY KEY,barrel_id TEXT NOT NULL,measured_liters REAL NOT NULL,meter_id TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS solvent_decision(barrel_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,quantities TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS solvent_operation(operation_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,quantities TEXT NOT NULL,version INTEGER NOT NULL);
"""


def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db


def update(db,table,entity_id,key='garment_id',**fields):
    old=db.execute(f'SELECT * FROM {table} WHERE {key}=?',(entity_id,)).fetchone()
    if old and all(old[name]==value for name,value in fields.items()):
        return dict(old)
    version=(old['version']+1) if old else 1
    names=[key,*fields,'version']
    values=[entity_id,*fields.values(),version]
    sql=f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)}) ON CONFLICT({key}) DO UPDATE SET "
    sql+=','.join(f'{name}=excluded.{name}' for name in [*fields,'version'])
    db.execute(sql,values)
    db.execute('INSERT INTO decision_history(subsystem,garment_id,version,state,reason) VALUES(?,?,?,?,?)',
               (table,entity_id,version,fields['state'],fields['reason']))
    return dict(db.execute(f'SELECT * FROM {table} WHERE {key}=?',(entity_id,)).fetchone())
