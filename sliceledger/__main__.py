import argparse,json,tempfile
from pathlib import Path
from . import operations,report,production,inventory
COMMANDS=['slice-import', 'material-reserve', 'part-accept','machine-import']
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('inventory-balance');arg.add_argument('--db',required=True);arg.add_argument('--lot',required=True)
    arg=sub.add_parser('report');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    x=parser.parse_args()
    if x.command=='demo':
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        result=operations.demo(root)
        print(json.dumps(result,ensure_ascii=False))
        print(report.build(root/'ledger.db',root/'report.json'))
    elif x.command=='report':print(report.build(x.db,x.out))
    elif x.command=='inventory-balance':print(json.dumps(inventory.balance(x.db,x.lot),ensure_ascii=False))
    else:
        fn=[operations.first,operations.second,operations.third,production.import_runs][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
