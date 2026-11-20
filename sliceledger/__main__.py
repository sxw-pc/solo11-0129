import argparse,json,tempfile
from pathlib import Path
from . import operations,report,production,inventory,lot_quality,load_test,load_rating
COMMANDS=['slice-import', 'material-reserve', 'part-accept','machine-import','lot-inspect','force-import',
          'calibration-import']
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
    rate=sub.add_parser('load-rate-files')
    rate.add_argument('--db',required=True)
    rate.add_argument('--model',required=True)
    rate.add_argument('--contracts',required=True)
    rate.add_argument('--plates',required=True)
    rate.add_argument('--calibration-id')
    rate.add_argument('--out-dir',default='outputs/ratings')
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
    elif x.command=='load-rate-files':
        out,doc=load_rating.rate_model(x.db,x.contracts,x.plates,x.model,
                                       calibration_id=x.calibration_id,out_dir=x.out_dir)
        print(json.dumps({'out':str(out),'rating_id':doc['rating_id'],
                          'status':doc['status']},ensure_ascii=False))
    elif x.command=='calibration-import':
        print(json.dumps(load_test.import_calibration(x.db,x.file),ensure_ascii=False))
    else:
        fn=[operations.first,operations.second,operations.third,production.import_runs,
            lot_quality.assess,load_test.import_trace][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
