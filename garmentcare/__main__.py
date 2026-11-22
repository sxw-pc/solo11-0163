import argparse
import json
from pathlib import Path

from . import catalog, intake, process, returns
from .report import dumps, report
from .source import canonical_json, file_record
from .store import connect

IMPORTS = {
    'garment-import': intake.import_data,
    'process-import': process.import_data,
    'return-import': returns.import_data,
    'label-import': catalog.import_data,
}


def route_import(db, data):
    return choose_route(db, data['garment_id'], data['route'], data['basis'], data['at'])

IMPORTS['label-route'] = route_import


def run_import(db, command, raw, as_of=None):
    data = json.loads(raw)
    if as_of is not None:
        data['as_of'] = as_of
    file_record(db, command, raw)
    return IMPORTS[command](db, data)


def choose_route(db, garment_id, route, basis, at):
    """Record the customer's explicit evidence-authority choice.

    Changing the route is an owner authorization, not a derived guess.  It
    re-adjudicates the garment immediately and is journaled so audit can
    replay it.
    """
    garment = db.execute('SELECT * FROM garment WHERE id=?', (garment_id,)).fetchone()
    if not garment:
        raise ValueError(f'unknown garment: {garment_id}')
    if route not in ('manufacturer', 'physical_label'):
        raise ValueError(f'unknown label route: {route}')
    document = {'garment_id': garment_id, 'route': route, 'basis': basis, 'at': at}
    event_id = f'route-{garment_id}-{route}-{at}'
    with db:
        if db.execute('SELECT 1 FROM label_route_event WHERE event_id=?', (event_id,)).fetchone():
            return intake.decide(db, garment_id)
        db.execute('INSERT INTO label_route_event(event_id,sku,chosen,fallback,basis,effective_at) '
                   'VALUES(?,?,?,?,?,?)',
                   (event_id, garment['sku'], route, garment['label_route'], basis, at))
        db.execute('INSERT INTO label_route(sku,chosen,fallback,basis,evidence_event,declared_at) '
                   'VALUES(?,?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET chosen=excluded.chosen,'
                   'fallback=excluded.fallback,basis=excluded.basis,evidence_event=excluded.evidence_event,'
                   'declared_at=excluded.declared_at',
                   (garment['sku'], route, garment['label_route'], basis, event_id, at))
        db.execute('UPDATE garment SET label_route=?,label_route_basis=? WHERE id=?',
                   (route, basis, garment_id))
        return intake.decide(db, garment_id)


def audit(db):
    replay = connect(':memory:')
    try:
        rows = db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for row in rows:
            run_import(replay, row['kind'], row['raw'])
        live_report = report(db)
        replay_report = report(replay)
        if live_report != replay_report:
            raise ValueError(_first_report_difference(live_report, replay_report))
        return {'status': 'matched', 'source_files': len(rows)}
    finally:
        replay.close()


def _first_report_difference(left, right, path=''):
    if type(left) is not type(right):
        return f'replayed source files disagree at {path or "root"}: {type(left).__name__} != {type(right).__name__}'
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return f'replayed source files disagree at {path or "root"}: keys {sorted(left)} != {sorted(right)}'
        for key in left:
            found = _first_report_difference(left[key], right[key], f'{path}.{key}' if path else key)
            if found:
                return found
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f'replayed source files disagree at {path}: length {len(left)} != {len(right)}'
        for index, (a, b) in enumerate(zip(left, right)):
            found = _first_report_difference(a, b, f'{path}[{index}]')
            if found:
                return found
        return None
    if left != right:
        return f'replayed source files disagree at {path}: {left!r} != {right!r}'
    return None


def main(argv=None):
    p = argparse.ArgumentParser(prog='garmentcare')
    p.add_argument('command', choices=['garment-import', 'process-import', 'return-import', 'label-import',
                                       'label-route', 'report', 'audit', 'demo'])
    p.add_argument('--db', required=True)
    p.add_argument('--file')
    p.add_argument('--garment')
    p.add_argument('--route', choices=['manufacturer', 'physical_label'])
    p.add_argument('--basis')
    p.add_argument('--as-of')
    p.add_argument('--at')
    a = p.parse_args(argv)
    db = connect(a.db)
    try:
        if a.command == 'report':
            print(dumps(db, a.as_of))
            return
        if a.command == 'audit':
            print(json.dumps(audit(db), ensure_ascii=False))
            return
        if a.command == 'label-route':
            document = {'garment_id': a.garment, 'route': a.route, 'basis': a.basis, 'at': a.at}
            print(json.dumps(run_import(db, 'label-route', canonical_json(document).encode()),
                             ensure_ascii=False, indent=2, default=str))
            return
        if a.command == 'demo':
            root = Path(__file__).parent.parent / 'fixtures'
            for command, name in [('garment-import', 'garment_ok.json'),
                                  ('process-import', 'process_ok.json'),
                                  ('return-import', 'return_ok.json'),
                                  ('label-import', 'label_page_ok.json')]:
                run_import(db, command, (root / name).read_bytes())
            print(dumps(db))
            return
        if not a.file:
            p.error('--file is required')
        print(json.dumps(run_import(db, a.command, Path(a.file).read_bytes(), a.as_of),
                         ensure_ascii=False, indent=2, default=str))
    finally:
        db.close()


if __name__ == '__main__':
    main()
