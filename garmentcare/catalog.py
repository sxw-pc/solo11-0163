"""Offline manufacturer care-label page collection for synthetic fixtures."""

from html.parser import HTMLParser
from .source import record


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'meta':
            return
        values = dict(attrs)
        key = values.get('name')
        if key in ('sku', 'material', 'care'):
            self.values[key] = values.get('content', '')


def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('pages', []):
            parser = MetaParser()
            parser.feed(row['html'])
            if parser.values.get('sku') != row['sku']:
                raise ValueError('page SKU does not match envelope')
            if record(db, row['event_id'], 'label_page', row):
                db.execute('INSERT INTO label_page VALUES(?,?,?,?,?)',
                           (row['event_id'], row['source'], row['sku'], row['fetched_at'], row['html']))
            touched.add(row['sku'])
        for sku in sorted(touched):
            pages = db.execute('SELECT * FROM label_page WHERE sku=? ORDER BY fetched_at DESC,event_id', (sku,)).fetchall()
            page = pages[0]
            parser = MetaParser()
            parser.feed(page['html'])
            db.execute('INSERT INTO label_entry VALUES(?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET source=excluded.source,material=excluded.material,care=excluded.care,source_event=excluded.source_event',
                       (sku, page['source'], parser.values.get('material', ''), parser.values.get('care', ''), page['event_id']))
    return [dict(db.execute('SELECT * FROM label_entry WHERE sku=?', (sku,)).fetchone()) for sku in sorted(touched)]
