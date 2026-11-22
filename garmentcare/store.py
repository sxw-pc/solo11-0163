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
CREATE TABLE IF NOT EXISTS fit_measure(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,at TEXT NOT NULL,current_hem INTEGER NOT NULL,target_hem INTEGER NOT NULL,allowance INTEGER NOT NULL,signer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alteration_decision(garment_id TEXT PRIMARY KEY,state TEXT NOT NULL,delta INTEGER NOT NULL,source_event TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hem_baseline(garment_id TEXT PRIMARY KEY,signed_at TEXT NOT NULL,signer TEXT NOT NULL,current_hem INTEGER NOT NULL,target_hem INTEGER NOT NULL,tolerance REAL NOT NULL,hem_width INTEGER NOT NULL,regular_hem_keep INTEGER NOT NULL,narrow_hem_keep INTEGER,narrow_consent INTEGER NOT NULL,band_consent INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rate_card(rate_key TEXT PRIMARY KEY,amount INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fabric_bolt(bolt_id TEXT PRIMARY KEY,width INTEGER NOT NULL,length INTEGER NOT NULL,grain_warp TEXT NOT NULL,pattern_period INTEGER,unit_price INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS defect_zone(zone_id TEXT PRIMARY KEY,bolt_id TEXT NOT NULL,x INTEGER NOT NULL,y INTEGER NOT NULL,width INTEGER NOT NULL,length INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cut_template(template_id TEXT PRIMARY KEY,width INTEGER NOT NULL,length INTEGER NOT NULL,grain_required TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS layout_plan(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,bolt_id TEXT NOT NULL,template_id TEXT NOT NULL,x INTEGER NOT NULL,y INTEGER NOT NULL,valid INTEGER NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cut_receipt(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,layout_event TEXT NOT NULL,bolt_id TEXT NOT NULL,template_id TEXT NOT NULL,x INTEGER NOT NULL,y INTEGER NOT NULL,at TEXT NOT NULL,area INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sew_record(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,at TEXT NOT NULL,route TEXT NOT NULL,finished_edge TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS remeasure(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,at TEXT NOT NULL,phase TEXT NOT NULL,measured_hem INTEGER NOT NULL,sew_id TEXT,measurer_signer TEXT NOT NULL,owner_signer TEXT NOT NULL,valid INTEGER NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rework_choice(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,at TEXT NOT NULL,route TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rework_cost_line(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,kind TEXT NOT NULL,amount INTEGER NOT NULL,basis TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rework_decision(garment_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,route TEXT,finished_edge TEXT,within_tolerance INTEGER,final_hem INTEGER,total_cost INTEGER NOT NULL,cut_open INTEGER NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fabric_ledger(bolt_id TEXT PRIMARY KEY,used_area INTEGER NOT NULL,available_area INTEGER NOT NULL,state TEXT NOT NULL);
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
