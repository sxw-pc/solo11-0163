"""Baseline shop score from every imported quality observation."""


def decide(db, shop):
    rows = db.execute('SELECT * FROM service_check WHERE shop=?', (shop,)).fetchall()
    failed = sum(row['queue_minutes'] > row['promise_minutes'] for row in rows)
    state = 'needs_improvement' if failed else 'meets_promise'
    db.execute('INSERT INTO store_grade VALUES(?,?,?,?) ON CONFLICT(shop) DO UPDATE SET state=excluded.state,failed_visits=excluded.failed_visits,checked_visits=excluded.checked_visits',
               (shop, state, failed, len(rows)))
    return dict(db.execute('SELECT * FROM store_grade WHERE shop=?', (shop,)).fetchone())
