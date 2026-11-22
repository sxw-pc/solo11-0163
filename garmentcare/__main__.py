import argparse
import json
from pathlib import Path
from . import intake,process,returns,utility
from .report import report,dumps
from .source import file_record
from .store import connect


def run_import(db,command,raw):
    data=json.loads(raw)
    file_record(db,command,raw)
    return {'garment-import':intake.import_data,'process-import':process.import_data,'return-import':returns.import_data,'meter-import':utility.import_data}[command](db,data)


def audit(db):
    """Rebuild every decision from journaled source files on an empty database.

    The replay starts with no decisions, so meter pages, topology, clock
    checks, certificates, common loads and clamp tests must reproduce the
    same difference decomposition from their raw evidence alone.
    """
    replay=connect(':memory:')
    try:
        rows=db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for row in rows:run_import(replay,row['kind'],row['raw'])
        if report(db)!=report(replay):raise ValueError('replayed source files disagree with decisions')
        return {'status':'matched','source_files':len(rows),
                'energy_investigations':[dict(row) for row in replay.execute(
                    'SELECT site,period,main_units,adjusted_branch_units,explained_units,unexplained_units,'
                    'state,responsibility FROM energy_investigation ORDER BY site,period')]}
    finally:replay.close()


def main(argv=None):
    p=argparse.ArgumentParser(prog='garmentcare')
    p.add_argument('command',choices=['garment-import','process-import','return-import','meter-import','report','audit','demo','energy-investigate'])
    p.add_argument('--db',required=True)
    p.add_argument('--file')
    p.add_argument('--site')
    p.add_argument('--period')
    a=p.parse_args(argv)
    db=connect(a.db)
    try:
        if a.command=='report':print(dumps(db));return
        if a.command=='audit':print(json.dumps(audit(db),ensure_ascii=False));return
        if a.command=='energy-investigate':
            from .energy import investigate,_public
            sites=[a.site] if a.site else [r['site'] for r in db.execute('SELECT DISTINCT site FROM meter_reading ORDER BY site')]
            out=[]
            for site in sites:
                period=a.period
                if not period:
                    periods=[r['period'] for r in db.execute('SELECT DISTINCT period FROM meter_reading WHERE site=? ORDER BY period',(site,))]
                    if not periods:continue
                    period=periods[-1]
                row=investigate(db,site,period)
                if row:out.append(_public(row))
            db.commit()
            print(json.dumps(out,ensure_ascii=False,indent=2));return
        if a.command=='demo':
            root=Path(__file__).parent.parent/'fixtures'
            for command,name in [('garment-import','garment_ok.json'),('process-import','process_ok.json'),('return-import','return_ok.json'),('meter-import','meter_topology_ok.json')]:
                run_import(db,command,(root/name).read_bytes())
            print(dumps(db));return
        if not a.file:p.error('--file is required')
        print(json.dumps(run_import(db,a.command,Path(a.file).read_bytes()),ensure_ascii=False,indent=2))
    finally:db.close()


if __name__=='__main__':main()
