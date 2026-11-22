"""Collect synthetic utility meter pages and physical topology evidence.

Pages only carry what is printed on them (site, period, reading, read time and,
when present, the meter clock offset).  Topology, clock checks, calibration
certificates, common loads and clamp tests are separate evidence records.  Raw
readings are never rewritten: clock corrections are stored next to the raw
value so a late certificate can only change attribution, not the number.
"""

from datetime import datetime, timedelta
from html.parser import HTMLParser

from .source import record


class MeterParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'meta':
            attrs = dict(attrs)
            if attrs.get('name') in ('site', 'period', 'reading', 'meter_id', 'reading_at', 'time_offset_seconds'):
                self.values[attrs['name']] = attrs.get('content', '')


def parse_page(html):
    parser = MeterParser()
    parser.feed(html)
    if 'site' not in parser.values or 'period' not in parser.values or 'reading' not in parser.values:
        raise ValueError('meter page missing site, period or reading')
    return parser.values


def _iso(value):
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)).isoformat()
    except ValueError:
        return str(value)


def read_at_of(period, value):
    """Reading cut-off on the meter clock; banner periods alone are not cut-offs."""
    return _iso(value) if value else f'{period}-01T00:00:00'


def corrected_at(read_at, offset_seconds):
    return (datetime.fromisoformat(read_at) + timedelta(seconds=int(offset_seconds or 0))).isoformat()


def _ensure_meter(db, site, meter_id, supplier=None):
    if not db.execute('SELECT 1 FROM meter WHERE meter_id=?', (meter_id,)).fetchone():
        db.execute('INSERT INTO meter(meter_id,site,role,parent_meter,branch_id,supplier,reversed) '
                   'VALUES(?,?,?,?,?,?,?)', (meter_id, site, 'unclassified', None, None, supplier, 0))


def _clock_check(db, site, meter_id, period, fetched_at):
    """Return (offset, verified) from a clock check made no later than the fetch."""
    row = db.execute('SELECT * FROM clock_check WHERE site=? AND meter_id=? AND period=? AND checked_at<=? '
                     'ORDER BY checked_at DESC,event_id DESC', (site, meter_id, period, fetched_at)).fetchone()
    return (int(row['offset_seconds']), 1, row['certificate_id']) if row else (0, 0, None)


def import_pages(db, data):
    sites = set()
    for row in data.get('pages', []):
        values = parse_page(row['html'])
        if values['site'] != row['site']:
            raise ValueError('meter site mismatch')
        site = row['site']
        meter_id = row.get('meter_id') or values.get('meter_id') or site
        period = values['period']
        read_at = row.get('reading_at') or values.get('reading_at')
        read_at = read_at_of(period, read_at)
        page_offset = row.get('time_offset_seconds', values.get('time_offset_seconds'))
        if record(db, row['event_id'], 'meter_page', row):
            db.execute('INSERT INTO meter_page VALUES(?,?,?,?,?)',
                       (row['event_id'], row['supplier'], site, row['fetched_at'], row['html']))
            _ensure_meter(db, site, meter_id, row['supplier'])
            check_offset, verified, cert = _clock_check(db, site, meter_id, period, row['fetched_at'])
            if verified:
                offset = check_offset
            else:
                offset = int(page_offset or 0)
            db.execute('INSERT INTO meter_reading(event_id,site,period,reading,supplier,meter_id,read_at,'
                       'clock_offset_seconds,corrected_at,clock_verified) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (row['event_id'], site, period, int(values['reading']), row['supplier'], meter_id,
                        read_at, offset, corrected_at(read_at, offset), verified))
        sites.add(site)
    return sites


def import_topology(db, data):
    sites = set()
    for row in data.get('topology', []):
        site = row['site']
        action = row['action']
        if action not in ('install', 'move', 'remove'):
            raise ValueError(f'unknown topology action: {action}')
        if record(db, row['event_id'], 'meter_topology', row):
            db.execute('INSERT INTO meter_topology(event_id,site,meter_id,branch_id,parent_meter,action,'
                       'effective_at,from_branch,segment,document_id,signer_a,signer_b,reversed,filed_at) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (row['event_id'], site, row['meter_id'], row['branch_id'], row.get('parent_meter'),
                        action, row['effective_at'], row.get('from_branch'), row.get('segment'),
                        row.get('document_id'), row.get('signer_a'), row.get('signer_b'),
                        int(bool(row.get('reversed', False))), row.get('filed_at') or row['effective_at']))
            _ensure_meter(db, site, row['meter_id'], row.get('supplier'))
            if action == 'install':
                role = 'top' if not row.get('parent_meter') else 'branch'
                db.execute('UPDATE meter SET parent_meter=?,branch_id=?,role=? WHERE meter_id=?',
                           (row.get('parent_meter'), row['branch_id'], role, row['meter_id']))
            elif action == 'move':
                role = 'top' if not row.get('parent_meter') else 'branch'
                db.execute('UPDATE meter SET parent_meter=?,branch_id=?,role=? WHERE meter_id=?',
                           (row.get('parent_meter'), row['branch_id'], role, row['meter_id']))
            else:
                db.execute("UPDATE meter SET role='removed' WHERE meter_id=?", (row['meter_id'],))
        sites.add(site)
    return sites


def import_clock_checks(db, data):
    sites = set()
    for row in data.get('clock_checks', []):
        site = row['site']
        if record(db, row['event_id'], 'clock_check', row):
            db.execute('INSERT INTO clock_check VALUES(?,?,?,?,?,?,?,?)',
                       (row['event_id'], site, row['meter_id'], row['period'], row['checked_at'],
                        int(row['offset_seconds']), row.get('certificate_id'), row.get('signer')))
            # Apply the check to raw readings of the same period already collected.
            for reading in db.execute('SELECT r.*,p.fetched_at FROM meter_reading r LEFT JOIN meter_page p '
                                      'ON p.event_id=r.event_id WHERE r.site=? AND r.meter_id=? AND r.period=?',
                                      (site, row['meter_id'], row['period'])).fetchall():
                fetched = reading['fetched_at'] or row['checked_at']
                if row['checked_at'] <= fetched:
                    db.execute('UPDATE meter_reading SET clock_offset_seconds=?,clock_verified=1,corrected_at=? '
                               'WHERE event_id=?',
                               (int(row['offset_seconds']),
                                corrected_at(read_at_of(reading['period'], reading['read_at']), row['offset_seconds']),
                                reading['event_id']))
        sites.add(site)
    return sites


def import_calibrations(db, data):
    sites = set()
    for row in data.get('calibrations', []):
        site = row['site']
        if record(db, row['event_id'], 'calibration_cert', row):
            db.execute('INSERT INTO calibration_cert VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (row['event_id'], site, row['meter_id'], row['issued_by'], row['issuer_role'],
                        row['period'], row['valid_from'], row['valid_to'], int(row['correction_units']),
                        int(row.get('uncertainty_units', 0)), row.get('certificate_id'), row.get('signer')))
        sites.add(site)
    return sites


def import_common_loads(db, data):
    sites = set()
    for row in data.get('common_loads', []):
        site = row['site']
        if record(db, row['event_id'], 'common_load', row):
            db.execute('INSERT INTO common_load VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (row['event_id'], site, row['load_id'], row.get('description', row['load_id']),
                        row['period'], int(row['units']), int(row.get('branch_metered_units', 0)),
                        row.get('connected_meter'), row.get('metered_by'), row.get('source', 'record'),
                        row.get('evidence_id'), row.get('signer_a'), row.get('signer_b'),
                        int(bool(row.get('valid', True)))))
        sites.add(site)
    return sites


def import_clamp_tests(db, data):
    sites = set()
    for row in data.get('clamp_tests', []):
        site = row['site']
        if record(db, row['event_id'], 'clamp_test', row):
            db.execute('INSERT INTO clamp_test VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (row['event_id'], site, row['load_id'], row.get('description', row['load_id']),
                        row['period'], int(row['units']), row.get('segment'), row.get('bypassed_meter'),
                        row.get('wiring_diagram_id'), row.get('signer_a'), row.get('signer_b'),
                        row.get('tested_at'), int(bool(row.get('valid', True)))))
        sites.add(site)
    return sites


def import_data(db, data):
    sites = set()
    with db:
        # Load the supporting evidence first so a page collected in the same
        # batch already sees its clock check, then collect the pages.
        sites |= import_topology(db, data)
        sites |= import_clock_checks(db, data)
        sites |= import_calibrations(db, data)
        sites |= import_common_loads(db, data)
        sites |= import_clamp_tests(db, data)
        sites |= import_pages(db, data)
        from .energy import investigate_site
        return investigate_site(db, sorted(sites))
