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
CREATE TABLE IF NOT EXISTS service_check(event_id TEXT PRIMARY KEY,shop TEXT NOT NULL,visit_id TEXT NOT NULL,observed_at TEXT NOT NULL,assessor TEXT NOT NULL,queue_minutes INTEGER NOT NULL,promise_minutes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS store_grade(shop TEXT PRIMARY KEY,state TEXT NOT NULL,failed_visits INTEGER NOT NULL,checked_visits INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sampling_plan(plan_id TEXT PRIMARY KEY,shop TEXT NOT NULL,week TEXT NOT NULL,min_per_stratum INTEGER NOT NULL,on_time_threshold INTEGER NOT NULL,promise_minutes INTEGER NOT NULL,sealed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sampling_stratum(plan_id TEXT NOT NULL,stratum TEXT NOT NULL,frame TEXT NOT NULL,min_samples INTEGER NOT NULL,PRIMARY KEY(plan_id,stratum));
CREATE TABLE IF NOT EXISTS sampling_task(task_id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,stratum TEXT NOT NULL,slot_at TEXT NOT NULL,promise_minutes INTEGER NOT NULL,signed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS visit_evidence(event_id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,task_id TEXT NOT NULL,shop TEXT NOT NULL,visit_id TEXT NOT NULL,stratum TEXT NOT NULL,observed_at TEXT NOT NULL,assessor TEXT NOT NULL,service_check_id TEXT,check_in_receipt TEXT NOT NULL,service_receipt TEXT,presence_ref TEXT NOT NULL,observed_minutes INTEGER NOT NULL,receipt_minutes INTEGER,uploaded_at TEXT,effective_minutes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ticket_resolution(event_id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,ticket_ref TEXT NOT NULL,visit_event_id TEXT NOT NULL,verdict TEXT NOT NULL,chain_ref TEXT NOT NULL,decided_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS certificate(plan_id TEXT NOT NULL,version INTEGER NOT NULL,cert_id TEXT NOT NULL,state TEXT NOT NULL,issued_at TEXT NOT NULL,revoked_at TEXT,reason TEXT NOT NULL,valid_sample_count INTEGER NOT NULL,on_time_count INTEGER NOT NULL,stratum_summary TEXT NOT NULL,recipients TEXT NOT NULL,content_sha TEXT NOT NULL,PRIMARY KEY(plan_id,version));
CREATE TABLE IF NOT EXISTS certification_decision(plan_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,valid_samples INTEGER NOT NULL,on_time_samples INTEGER NOT NULL,late_samples INTEGER NOT NULL,stratum_summary TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS certification_history(id INTEGER PRIMARY KEY,plan_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rectification(plan_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,opened_at TEXT NOT NULL,closed_at TEXT);
CREATE TABLE IF NOT EXISTS promise_policy(event_id TEXT PRIMARY KEY,shop TEXT NOT NULL,promise_minutes INTEGER NOT NULL,changed_at TEXT NOT NULL);
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
