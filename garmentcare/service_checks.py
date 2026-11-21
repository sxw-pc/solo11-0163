"""Offline intake of synthetic counter-service quality observations."""

from .source import record


def import_data(db, data):
    shops = set()
    with db:
        for row in data.get('checks', []):
            if record(db, row['event_id'], 'service_check', row):
                db.execute('INSERT INTO service_check VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['shop'], row['visit_id'], row['observed_at'], row['assessor'], row['queue_minutes'], row['promise_minutes']))
            shops.add(row['shop'])
        from .store_score import decide
        return [decide(db, shop) for shop in sorted(shops)]
