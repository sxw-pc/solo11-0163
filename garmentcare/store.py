import sqlite3

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS garment(id TEXT PRIMARY KEY,owner TEXT NOT NULL,material TEXT NOT NULL,accepted_at TEXT NOT NULL,declared_value INTEGER NOT NULL,cap_rate INTEGER NOT NULL,allowed_process TEXT NOT NULL,pickup_person TEXT NOT NULL,claim_until TEXT NOT NULL,expected_bag TEXT NOT NULL,sku TEXT NOT NULL DEFAULT '',label_route TEXT NOT NULL DEFAULT 'manufacturer',label_route_basis TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS intake_photo(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,version INTEGER NOT NULL,photo_sha TEXT NOT NULL,damage TEXT NOT NULL,owner_signer TEXT NOT NULL,clerk_signer TEXT NOT NULL,material TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intake_decision(garment_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,photo_id TEXT,version INTEGER NOT NULL,label_material TEXT,label_care TEXT,label_status TEXT,label_event TEXT);
CREATE TABLE IF NOT EXISTS recipe(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,version INTEGER NOT NULL,process TEXT NOT NULL,max_temp INTEGER NOT NULL,banned_chemicals TEXT NOT NULL,approved_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operation(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,recipe_id TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,temp INTEGER NOT NULL,chemical TEXT NOT NULL,from_shop TEXT NOT NULL,to_shop TEXT NOT NULL,sender TEXT,receiver TEXT);
CREATE TABLE IF NOT EXISTS process_decision(garment_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,recipe_id TEXT,version INTEGER NOT NULL,nominal_state TEXT,label_effect TEXT,new_processing TEXT NOT NULL DEFAULT 'eligible');
CREATE TABLE IF NOT EXISTS pickup_authority(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,person TEXT NOT NULL,owner_signer TEXT NOT NULL,valid_to TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS return_receipt(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,person TEXT NOT NULL,at TEXT NOT NULL,bag TEXT NOT NULL,condition TEXT NOT NULL,owner_signer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claim(event_id TEXT PRIMARY KEY,garment_id TEXT NOT NULL,receipt_id TEXT NOT NULL,at TEXT NOT NULL,photo_sha TEXT NOT NULL,damage TEXT NOT NULL,requested_amount INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS compensation_decision(garment_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,payable INTEGER NOT NULL,frozen INTEGER NOT NULL,version INTEGER NOT NULL,nominal_state TEXT);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,garment_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS decision_history_meta(history_id INTEGER PRIMARY KEY,evidence_ref TEXT,at_as_of TEXT);
CREATE TABLE IF NOT EXISTS label_page(event_id TEXT PRIMARY KEY,source TEXT NOT NULL,sku TEXT NOT NULL,fetched_at TEXT NOT NULL,html TEXT NOT NULL,
  declared_effective_at TEXT,effective_at TEXT,effective_basis TEXT NOT NULL DEFAULT 'fetch_time_fallback',
  content_sha TEXT NOT NULL,page_sha TEXT NOT NULL,revision TEXT NOT NULL DEFAULT '',signer TEXT NOT NULL DEFAULT '',
  declared_material TEXT NOT NULL DEFAULT '',declared_care TEXT NOT NULL DEFAULT '',
  revision_of TEXT NOT NULL DEFAULT '',withdraws TEXT NOT NULL DEFAULT '',url TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS label_withdrawal(event_id TEXT PRIMARY KEY,source TEXT NOT NULL,sku TEXT NOT NULL,fetched_at TEXT NOT NULL,
  target_event TEXT NOT NULL,effective_at TEXT,declared_effective_at TEXT,effective_basis TEXT NOT NULL DEFAULT 'fetch_time_fallback',
  reason TEXT NOT NULL DEFAULT '',content_sha TEXT NOT NULL,revision TEXT NOT NULL DEFAULT '',signer TEXT NOT NULL DEFAULT '',
  target_source TEXT NOT NULL DEFAULT '',target_content_sha TEXT NOT NULL DEFAULT '',target_revision TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS label_revision(id INTEGER PRIMARY KEY,sku TEXT NOT NULL,version INTEGER NOT NULL,at_as_of TEXT NOT NULL,
  state TEXT NOT NULL,reason TEXT NOT NULL,careness TEXT NOT NULL,adopt_event TEXT,adopt_source TEXT,adopt_material TEXT,adopt_care TEXT,
  effective_at TEXT,conflict_count INTEGER NOT NULL DEFAULT 0,pending_count INTEGER NOT NULL DEFAULT 0,
  candidate_events TEXT NOT NULL DEFAULT '[]',pending_events TEXT NOT NULL DEFAULT '[]',rejected_events TEXT NOT NULL DEFAULT '[]',
  withdrawn_events TEXT NOT NULL DEFAULT '[]',source_chain TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS label_entry(sku TEXT PRIMARY KEY,source TEXT NOT NULL,material TEXT NOT NULL,care TEXT NOT NULL,source_event TEXT NOT NULL,
  declared_effective_at TEXT,effective_at TEXT,effective_basis TEXT NOT NULL DEFAULT 'fetch_time_fallback',
  content_sha TEXT NOT NULL DEFAULT '',page_sha TEXT NOT NULL DEFAULT '',revision TEXT NOT NULL DEFAULT '',signer TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'verified',reason TEXT NOT NULL DEFAULT '',careness TEXT NOT NULL DEFAULT 'verified',
  as_of TEXT NOT NULL DEFAULT '',candidates TEXT NOT NULL DEFAULT '[]',conflict_events TEXT NOT NULL DEFAULT '[]',
  pending_events TEXT NOT NULL DEFAULT '[]',withdrawn_events TEXT NOT NULL DEFAULT '[]',
  source_chain TEXT NOT NULL DEFAULT '[]',version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS label_label_event(event_key TEXT PRIMARY KEY,sku TEXT NOT NULL,source TEXT NOT NULL,source_kind TEXT NOT NULL,
  label_code TEXT NOT NULL,material TEXT NOT NULL,care TEXT NOT NULL,signer TEXT NOT NULL,signed_at TEXT NOT NULL,
  effective_at TEXT,source_event TEXT);
CREATE TABLE IF NOT EXISTS label_route(sku TEXT PRIMARY KEY,chosen TEXT NOT NULL,fallback TEXT NOT NULL DEFAULT '',
  basis TEXT NOT NULL,evidence_event TEXT,declared_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS label_route_event(event_id TEXT PRIMARY KEY,sku TEXT NOT NULL,chosen TEXT NOT NULL,fallback TEXT NOT NULL,
  basis TEXT NOT NULL,effective_at TEXT,event_sha TEXT);
CREATE TABLE IF NOT EXISTS garment_label_review(garment_id TEXT PRIMARY KEY,sku TEXT NOT NULL,route TEXT NOT NULL,
  label_material TEXT,label_care TEXT,label_state TEXT,label_event TEXT,label_revision INTEGER,checked_as_of TEXT,
  task_state TEXT NOT NULL,reason TEXT NOT NULL,impact TEXT NOT NULL DEFAULT 'continue',conflict INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS garment_label_review_history(id INTEGER PRIMARY KEY,garment_id TEXT NOT NULL,version INTEGER NOT NULL,
  task_state TEXT NOT NULL,reason TEXT NOT NULL,route TEXT NOT NULL,label_state TEXT NOT NULL,label_care TEXT,label_event TEXT,
  impact TEXT NOT NULL DEFAULT 'continue',conflict INTEGER NOT NULL DEFAULT 0,at DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS catalog_source_chain(sku TEXT PRIMARY KEY,history_id INTEGER NOT NULL,state TEXT NOT NULL,adopt_event TEXT,as_of TEXT NOT NULL);
"""

# Columns added after the first label baseline.  They are applied to existing
# databases so a state file created by the earlier version keeps working.
MIGRATIONS = {
    'garment': {
        'sku': "TEXT NOT NULL DEFAULT ''",
        'label_route': "TEXT NOT NULL DEFAULT 'manufacturer'",
        'label_route_basis': "TEXT NOT NULL DEFAULT ''",
    },
    'intake_decision': {
        'label_material': 'TEXT', 'label_care': 'TEXT', 'label_status': 'TEXT', 'label_event': 'TEXT',
    },
    'process_decision': {'nominal_state': 'TEXT', 'label_effect': 'TEXT',
                         'new_processing': "TEXT NOT NULL DEFAULT 'eligible'"},
    'compensation_decision': {'nominal_state': 'TEXT'},
    'label_page': {
        'declared_effective_at': 'TEXT', 'effective_at': 'TEXT',
        'effective_basis': "TEXT NOT NULL DEFAULT 'fetch_time_fallback'",
        'content_sha': "TEXT NOT NULL DEFAULT ''", 'page_sha': "TEXT NOT NULL DEFAULT ''",
        'revision': "TEXT NOT NULL DEFAULT ''", 'signer': "TEXT NOT NULL DEFAULT ''",
        'declared_material': "TEXT NOT NULL DEFAULT ''", 'declared_care': "TEXT NOT NULL DEFAULT ''",
        'revision_of': "TEXT NOT NULL DEFAULT ''", 'withdraws': "TEXT NOT NULL DEFAULT ''",
        'url': "TEXT NOT NULL DEFAULT ''",
    },
    'label_withdrawal': {
        'effective_basis': "TEXT NOT NULL DEFAULT 'fetch_time_fallback'",
        'target_source': "TEXT NOT NULL DEFAULT ''", 'target_content_sha': "TEXT NOT NULL DEFAULT ''",
        'target_revision': "TEXT NOT NULL DEFAULT ''",
    },
    'label_entry': {
        'declared_effective_at': 'TEXT', 'effective_at': 'TEXT', 'effective_basis': 'TEXT',
        'content_sha': "TEXT NOT NULL DEFAULT ''", 'page_sha': "TEXT NOT NULL DEFAULT ''",
        'revision': "TEXT NOT NULL DEFAULT ''", 'signer': "TEXT NOT NULL DEFAULT ''",
        'state': "TEXT NOT NULL DEFAULT 'verified'", 'reason': "TEXT NOT NULL DEFAULT ''",
        'careness': "TEXT NOT NULL DEFAULT 'verified'", 'as_of': "TEXT NOT NULL DEFAULT ''",
        'candidates': "TEXT NOT NULL DEFAULT '[]'", 'conflict_events': "TEXT NOT NULL DEFAULT '[]'",
        'pending_events': "TEXT NOT NULL DEFAULT '[]'", 'withdrawn_events': "TEXT NOT NULL DEFAULT '[]'",
        'source_chain': "TEXT NOT NULL DEFAULT '[]'", 'version': 'INTEGER NOT NULL DEFAULT 1',
    },
    'label_label_event': {'garment_id': "TEXT NOT NULL DEFAULT ''"},
    'garment_label_review': {
        'impact': "TEXT NOT NULL DEFAULT 'continue'", 'conflict': 'INTEGER NOT NULL DEFAULT 0',
    },
    'garment_label_review_history': {
        'impact': "TEXT NOT NULL DEFAULT 'continue'", 'conflict': 'INTEGER NOT NULL DEFAULT 0',
    },
    'decision_history_meta': {'evidence_ref': 'TEXT', 'at_as_of': 'TEXT'},
}


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        existing = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
        for name, decl in columns.items():
            if name not in existing:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')
    return db


def update(db, table, garment_id, **fields):
    # ``_evidence_ref`` / ``_as_of`` annotate the history row; they are not
    # columns of the decision table.
    evidence_ref = fields.pop('_evidence_ref', None)
    at_as_of = fields.pop('_as_of', None)
    old = db.execute(f'SELECT * FROM {table} WHERE garment_id=?', (garment_id,)).fetchone()
    if old and all(old[key] == value for key, value in fields.items()):
        return dict(old)
    version = (old['version'] + 1) if old else 1
    names = ['garment_id', *fields, 'version']
    values = [garment_id, *fields.values(), version]
    sql = f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)}) ON CONFLICT(garment_id) DO UPDATE SET "
    sql += ','.join(f'{name}=excluded.{name}' for name in [*fields, 'version'])
    db.execute(sql, values)
    cursor = db.execute('INSERT INTO decision_history(subsystem,garment_id,version,state,reason) VALUES(?,?,?,?,?)',
                        (table, garment_id, version, fields['state'], fields['reason']))
    db.execute('INSERT INTO decision_history_meta(history_id,evidence_ref,at_as_of) VALUES(?,?,?)',
               (cursor.lastrowid, evidence_ref, at_as_of))
    return dict(db.execute(f'SELECT * FROM {table} WHERE garment_id=?', (garment_id,)).fetchone())
