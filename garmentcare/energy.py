"""Baseline usage from two latest synthetic meter readings."""


def decide(db, site):
    rows = db.execute('SELECT * FROM meter_reading WHERE site=? ORDER BY period DESC,event_id', (site,)).fetchall()
    if len(rows) < 2:
        state, units = 'pending', 0
    else:
        units = rows[0]['reading'] - rows[1]['reading']
        state = 'measured' if units >= 0 else 'review'
    period = rows[0]['period'] if rows else ''
    db.execute('INSERT INTO energy_usage VALUES(?,?,?,?) ON CONFLICT(site) DO UPDATE SET state=excluded.state,latest_period=excluded.latest_period,units=excluded.units',
               (site, state, period, units))
    return dict(db.execute('SELECT * FROM energy_usage WHERE site=?', (site,)).fetchone())
