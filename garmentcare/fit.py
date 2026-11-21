"""Offline intake of synthetic garment fitting measurements."""

from .source import record


def import_data(db, data):
    garments = set()
    with db:
        for row in data.get('measures', []):
            if record(db, row['event_id'], 'fit_measure', row):
                db.execute('INSERT INTO fit_measure VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['garment_id'], row['at'], row['current_hem'], row['target_hem'], row['allowance'], row['signer']))
            garments.add(row['garment_id'])
        from .alteration import decide
        return [decide(db, garment_id) for garment_id in sorted(garments)]
