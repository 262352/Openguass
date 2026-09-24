from __future__ import annotations
import argparse,json
from pathlib import Path
from app.lab.environment import inspect
from app.lab.migration import report as migration_report
from app.lab.reporter import ExperimentReporter
from app.lab.tuner import LHSGPTuner
from app.lab.transition import ACTIVE,REGISTERED,run_transition
from app.lab.workloads import PostgresBenchBaseAdapter

def emit(value):print(json.dumps(value,ensure_ascii=False,indent=2))
def main():
 p=argparse.ArgumentParser(description='Andromeda PostgreSQL reproducible experiment CLI');sub=p.add_subparsers(dest='group',required=True)
 env=sub.add_parser('environment');env.add_subparsers(dest='action',required=True).add_parser('inspect')
 mig=sub.add_parser('migration');mig.add_subparsers(dest='action',required=True).add_parser('report')
 ex=sub.add_parser('experiment');exs=ex.add_subparsers(dest='action',required=True)
 init=exs.add_parser('init');init.add_argument('--id',required=True);init.add_argument('--mode',choices=['formal','quick'],required=True);init.add_argument('--lhs',type=int,required=True);init.add_argument('--gp',type=int,required=True)
 stat=exs.add_parser('status');stat.add_argument('--id',required=True)
 fin=exs.add_parser('finish');fin.add_argument('--id',required=True);fin.add_argument('--state',choices=['completed','paused','failed'],required=True);fin.add_argument('--message',default='')
 w=sub.add_parser('workload');ws=w.add_subparsers(dest='action',required=True);prep=ws.add_parser('prepare');prep.add_argument('workload',choices=['tpcc','twitter','ycsb']);prep.add_argument('--reload',action='store_true');run=ws.add_parser('run');run.add_argument('workload',choices=['tpcc','twitter','ycsb']);run.add_argument('--duration',type=int,default=15);run.add_argument('--terminals',type=int,default=2)
 tune=sub.add_parser('tune');ts=tune.add_subparsers(dest='action',required=True);ref=ts.add_parser('reference');ref.add_argument('workload',choices=['tpcc','twitter']);ref.add_argument('--experiment-id',required=True);ref.add_argument('--lhs',type=int,default=15);ref.add_argument('--gp',type=int,default=35);ref.add_argument('--duration',type=int,default=15);ref.add_argument('--terminals',type=int,default=2);ref.add_argument('--seed',type=int,default=20260921);ref.add_argument('--no-resume',action='store_true');ref.add_argument('--snapshot-isolation',action='store_true')
 refs=sub.add_parser('reference');rss=refs.add_subparsers(dest='action',required=True);sel=rss.add_parser('select');sel.add_argument('workload',choices=['tpcc','twitter']);sel.add_argument('--experiment-id',required=True);sel.add_argument('--duration',type=int,default=15);sel.add_argument('--terminals',type=int,default=2);sel.add_argument('--repeats',type=int,default=5);sel.add_argument('--top-k',type=int,default=5);sel.add_argument('--snapshot-isolation',action='store_true')
 tr=sub.add_parser('transition');trs=tr.add_subparsers(dest='action',required=True);trs.add_parser('matrix');rr=trs.add_parser('run');rr.add_argument('source',choices=['tpcc','twitter','ycsb']);rr.add_argument('target',choices=['tpcc','twitter','ycsb']);rr.add_argument('--experiment-id',required=True);rr.add_argument('--duration',type=int,default=15);rr.add_argument('--terminals',type=int,default=2);rr.add_argument('--strict-llm',action='store_true')
 args=p.parse_args()
 if args.group=='environment':emit(inspect())
 elif args.group=='migration':emit(migration_report())
 elif args.group=='experiment':
  reporter=ExperimentReporter(args.id)
  if args.action=='init':emit(reporter.initialize(args.mode,args.lhs,args.gp))
  elif args.action=='status':emit(json.loads(reporter.status_path.read_text()))
  else:emit(reporter.finish(args.state,args.message))
 elif args.group=='workload':
  a=PostgresBenchBaseAdapter(args.workload);emit(a.prepare(args.reload) if args.action=='prepare' else a.run(args.duration,args.terminals,{}))
 elif args.group=='tune':emit(LHSGPTuner(args.experiment_id,args.workload,args.duration,args.terminals,args.seed,snapshot_isolation=args.snapshot_isolation).run(args.lhs,args.gp,not args.no_resume))
 elif args.group=='reference':emit(LHSGPTuner(args.experiment_id,args.workload,args.duration,args.terminals,snapshot_isolation=args.snapshot_isolation).select_reference(args.repeats,args.top_k))
 elif args.action=='matrix':emit({'registered':REGISTERED,'active':sorted(ACTIVE)})
 else:
  result=run_transition(args.source,args.target,args.duration,args.terminals,args.experiment_id,not args.strict_llm);emit({'source':result['source'],'target':result['target'],'status':result['status'],'recommendation':result['recommendation'],'comparison':result['comparison'],'report':f'artifacts/transitions/{args.experiment_id}/{args.source}_to_{args.target}/transition_report.json'})
if __name__=='__main__':main()
