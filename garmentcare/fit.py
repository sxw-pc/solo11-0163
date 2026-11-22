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
        results = [decide(db, garment_id) for garment_id in sorted(garments)]
        # A late measuring stick (e.g. a corrected target) recomputes rework
        # acceptance, but never reverses cut receipts already on the ledger.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hem_baseline'").fetchone():
            from .rework import decide as rework_decide
            for garment_id in sorted(garments):
                if db.execute('SELECT 1 FROM hem_baseline WHERE garment_id=?', (garment_id,)).fetchone():
                    rework_decide(db, garment_id)
        return results
