"""Baseline last-touch attribution for synthetic service bookings."""


def decide(db, booking_id):
    booking = db.execute('SELECT * FROM service_booking WHERE event_id=?', (booking_id,)).fetchone()
    if not booking:
        state, campaign, source = 'pending', None, None
    else:
        touch = db.execute('SELECT * FROM campaign_touch WHERE visitor=? AND at<=? ORDER BY at DESC,event_id LIMIT 1',
                           (booking['visitor'], booking['at'])).fetchone()
        state, campaign, source = ('attributed', touch['campaign'], touch['event_id']) if touch else ('organic', None, None)
    db.execute('INSERT INTO acquisition_decision VALUES(?,?,?,?) ON CONFLICT(booking_id) DO UPDATE SET state=excluded.state,campaign=excluded.campaign,source_event=excluded.source_event',
               (booking_id, state, campaign, source))
    return dict(db.execute('SELECT * FROM acquisition_decision WHERE booking_id=?', (booking_id,)).fetchone())
