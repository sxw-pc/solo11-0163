"""Offline intake of synthetic laundry campaign visits and bookings."""

from .source import record


def import_data(db, data):
    bookings = set()
    with db:
        for row in data.get('touches', []):
            if record(db, row['event_id'], 'campaign_touch', row):
                db.execute('INSERT INTO campaign_touch VALUES(?,?,?,?,?)',
                           (row['event_id'], row['visitor'], row['campaign'], row['channel'], row['at']))
            bookings.update(r[0] for r in db.execute('SELECT event_id FROM service_booking WHERE visitor=?', (row['visitor'],)))
        for row in data.get('bookings', []):
            if record(db, row['event_id'], 'service_booking', row):
                db.execute('INSERT INTO service_booking VALUES(?,?,?,?)',
                           (row['event_id'], row['visitor'], row['garment_id'], row['at']))
            bookings.add(row['event_id'])
        from .attribution import decide
        return [decide(db, booking) for booking in sorted(bookings)]
