"""Collect synthetic utility meter pages for laundry operations."""

from html.parser import HTMLParser
from .source import record


class MeterParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'meta':
            attrs = dict(attrs)
            if attrs.get('name') in ('site', 'period', 'reading'):
                self.values[attrs['name']] = attrs.get('content', '')


def import_data(db, data):
    sites = set()
    with db:
        for row in data.get('pages', []):
            parser = MeterParser()
            parser.feed(row['html'])
            if parser.values.get('site') != row['site']:
                raise ValueError('meter site mismatch')
            if record(db, row['event_id'], 'meter_page', row):
                db.execute('INSERT INTO meter_page VALUES(?,?,?,?,?)',
                           (row['event_id'], row['supplier'], row['site'], row['fetched_at'], row['html']))
                db.execute('INSERT INTO meter_reading VALUES(?,?,?,?,?)',
                           (row['event_id'], row['site'], parser.values['period'], int(parser.values['reading']), row['supplier']))
            sites.add(row['site'])
        from .energy import decide
        return [decide(db, site) for site in sorted(sites)]
