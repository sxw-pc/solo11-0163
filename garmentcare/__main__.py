import argparse
import json
from pathlib import Path
from . import intake,process,returns,solvent
from .report import report,dumps
from .source import file_record
from .store import connect


def run_import(db,command,raw):
    data=json.loads(raw)
    file_record(db,command,raw)
    return {'garment-import':intake.import_data,'process-import':process.import_data,'return-import':returns.import_data,'solvent-import':solvent.import_data}[command](db,data)


def audit(db):
    replay=connect(':memory:')
    try:
        rows=db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for row in rows:run_import(replay,row['kind'],row['raw'])
        if report(db)!=report(replay):raise ValueError('replayed source files disagree with decisions')
        return {'status':'matched','source_files':len(rows)}
    finally:replay.close()


def main(argv=None):
    p=argparse.ArgumentParser(prog='garmentcare')
    p.add_argument('command',choices=['garment-import','process-import','return-import','solvent-import','report','audit','demo'])
    p.add_argument('--db',required=True)
    p.add_argument('--file')
    a=p.parse_args(argv)
    db=connect(a.db)
    try:
        if a.command=='report':print(dumps(db));return
        if a.command=='audit':print(json.dumps(audit(db),ensure_ascii=False));return
        if a.command=='demo':
            root=Path(__file__).parent.parent/'fixtures'
            for command,name in [('garment-import','garment_ok.json'),('process-import','process_ok.json'),('process-import','solvent_ops.json'),('return-import','return_ok.json'),('solvent-import','solvent_base.json'),('solvent-import','solvent_distill.json')]:
                run_import(db,command,(root/name).read_bytes())
            print(dumps(db));return
        if not a.file:p.error('--file is required')
        print(json.dumps(run_import(db,a.command,Path(a.file).read_bytes()),ensure_ascii=False,indent=2))
    finally:db.close()


if __name__=='__main__':main()
