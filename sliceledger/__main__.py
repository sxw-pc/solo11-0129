import argparse,json,tempfile
from pathlib import Path
from . import operations,report,production,inventory,lot_quality,maintenance
COMMANDS=['slice-import', 'material-reserve', 'part-accept','machine-import','lot-inspect','sensor-import','maintenance-release',
          'diagnostic-manual-import','maintenance-commission','test-execution','maintenance-diagnose']
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True)
        if command!='maintenance-diagnose':arg.add_argument('file')
    arg=sub.add_parser('inventory-balance');arg.add_argument('--db',required=True);arg.add_argument('--lot',required=True)
    arg=sub.add_parser('report');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    arg=sub.add_parser('maintenance-plans');arg.add_argument('--db',required=True);arg.add_argument('--commission',required=True)
    x=parser.parse_args()
    if x.command=='demo':
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        result=operations.demo(root)
        print(json.dumps(result,ensure_ascii=False))
        print(report.build(root/'ledger.db',root/'report.json'))
    elif x.command=='report':print(report.build(x.db,x.out))
    elif x.command=='inventory-balance':print(json.dumps(inventory.balance(x.db,x.lot),ensure_ascii=False))
    elif x.command=='maintenance-diagnose':
        print(json.dumps(maintenance.diagnose(x.db),ensure_ascii=False,default=str))
    elif x.command=='maintenance-plans':
        from .store import connect
        db=connect(x.db)
        try:print(json.dumps(maintenance.plans_for(db,x.commission),ensure_ascii=False))
        finally:db.close()
    else:
        fn=[operations.first,operations.second,operations.third,production.import_runs,
            lot_quality.assess,maintenance.import_sensors,maintenance.release,
            maintenance.import_manual,maintenance.delegate,maintenance.execute_test][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
