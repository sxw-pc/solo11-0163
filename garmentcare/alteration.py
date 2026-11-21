"""Baseline hem alteration feasibility from the latest measurement."""


def decide(db, garment_id):
    row = db.execute('SELECT * FROM fit_measure WHERE garment_id=? ORDER BY at DESC,event_id LIMIT 1', (garment_id,)).fetchone()
    delta = row['target_hem'] - row['current_hem']
    state = 'feasible' if delta <= row['allowance'] else 'needs_material'
    db.execute('INSERT INTO alteration_decision VALUES(?,?,?,?) ON CONFLICT(garment_id) DO UPDATE SET state=excluded.state,delta=excluded.delta,source_event=excluded.source_event',
               (garment_id, state, delta, row['event_id']))
    return dict(db.execute('SELECT * FROM alteration_decision WHERE garment_id=?', (garment_id,)).fetchone())
