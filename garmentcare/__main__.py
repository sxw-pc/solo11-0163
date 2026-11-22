import argparse
import json
from pathlib import Path
from . import delivery,intake,process,returns
from .report import report,dumps
from .source import file_record
from .store import connect

IMPORTERS={'garment-import':intake.import_data,'process-import':process.import_data,
           'return-import':returns.import_data,'order-import':delivery.import_data}


def run_import(db,command,raw):
    data=json.loads(raw)
    file_record(db,command,raw)
    return IMPORTERS[command](db,data)


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
    p.add_argument('command',choices=[*IMPORTERS,'report','audit','demo','split','handoff'])
    p.add_argument('--db',required=True)
    p.add_argument('--file')
    p.add_argument('--order')
    p.add_argument('--garment')
    p.add_argument('--bag')
    p.add_argument('--seal')
    p.add_argument('--person')
    p.add_argument('--at')
    p.add_argument('--to-bag')
    p.add_argument('--new-seal')
    a=p.parse_args(argv)
    db=connect(a.db)
    try:
        if a.command=='report':print(dumps(db));return
        if a.command=='audit':print(json.dumps(audit(db),ensure_ascii=False));return
        if a.command=='demo':
            root=Path(__file__).parent.parent/'fixtures'
            for command,name in [('garment-import','garment_ok.json'),('process-import','process_ok.json'),('return-import','return_ok.json')]:
                run_import(db,command,(root/name).read_bytes())
            print(dumps(db));return
        if a.command=='split':
            for flag in ('order','garment','to_bag','new_seal','at'):
                if not getattr(a,flag):p.error(f'--{flag.replace("_","-")} is required')
            print(json.dumps(delivery.split_order(db,a.order,a.garment,a.to_bag,a.new_seal,a.at,
                                                  owner_signer=a.person),ensure_ascii=False,indent=2));return
        if a.command=='handoff':
            for flag in ('order','bag','seal','person','at'):
                if not getattr(a,flag):p.error(f'--{flag} is required')
            print(json.dumps(delivery.handoff_order(db,a.order,a.bag,a.seal,a.person,a.at,
                                                    owner_signer=a.person),ensure_ascii=False,indent=2));return
        if not a.file:p.error('--file is required')
        print(json.dumps(run_import(db,a.command,Path(a.file).read_bytes()),ensure_ascii=False,indent=2))
    finally:db.close()


if __name__=='__main__':main()
