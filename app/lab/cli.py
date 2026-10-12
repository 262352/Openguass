from __future__ import annotations
import argparse,json,os
from pathlib import Path
from app.lab.environment import inspect
from app.lab.migration import report as migration_report
from app.lab.reporter import ExperimentReporter
from app.lab.tuner import LHSGPTuner
from app.lab.transition import ACTIVE,REGISTERED,run_transition
from app.lab.workloads import PostgresBenchBaseAdapter
from app.chain.phase2_graph import generate_phase2_graphs
from app.chain.preflight import chain_preflight
from app.chain.commands import capture_source_config, parse_directions, report as chain_report, resume as chain_resume, run_directions, status as chain_status, stop as chain_stop
from app.chain.loop import ChainRunOptions

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
 diag=sub.add_parser('diagnosis');ds=diag.add_subparsers(dest='action',required=True);graph=ds.add_parser('graph');graph.add_argument('--experiment-id',required=True);graph.add_argument('--direction',choices=['tpcc_to_twitter','twitter_to_tpcc'])
 chain=sub.add_parser('chain');cs=chain.add_subparsers(dest='action',required=True);cs.add_parser('preflight')
 def chain_args(parser):
  parser.add_argument('--mode',choices=['quick','formal'],default='formal');parser.add_argument('--experiment-id')
  parser.add_argument('--duration',type=int);parser.add_argument('--warmup',type=int);parser.add_argument('--terminals',type=int,default=2)
  parser.add_argument('--max-experiments',type=int);parser.add_argument('--max-restarts',type=int,default=60);parser.add_argument('--max-wall-seconds',type=int,default=7200)
  parser.add_argument('--min-tps-percent',type=float,default=5);parser.add_argument('--patience',type=int,default=3);parser.add_argument('--seed',type=int,default=20260929)
  parser.add_argument('--source-config-dir');parser.add_argument('--max-p95-regression-percent',type=float,default=10);parser.add_argument('--max-rollback-ratio-increase-pp',type=float,default=1)
  parser.add_argument('--min-screening-groups',type=int,default=4)
 cr=cs.add_parser('run');cr.add_argument('--source',choices=['tpcc','twitter','ycsb'],required=True);cr.add_argument('--target',choices=['tpcc','twitter','ycsb'],required=True);chain_args(cr)
 cd=cs.add_parser('run-directions');cd.add_argument('--directions',required=True);chain_args(cd)
 for action in ('status','stop','resume','report'):
  cp=cs.add_parser(action);cp.add_argument('--experiment-id',required=True)
 cc=cs.add_parser('capture-source-config');cc.add_argument('--workload',choices=['tpcc','twitter','ycsb'],required=True);cc.add_argument('--output')
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
 elif args.group=='diagnosis':emit({'experiment_id':args.experiment_id,'graphs':generate_phase2_graphs(args.experiment_id,direction=args.direction)})
 elif args.group=='chain':
  if args.action=='preflight':emit(chain_preflight())
  elif args.action=='status':emit(chain_status(Path('.'),args.experiment_id))
  elif args.action=='stop':emit(chain_stop(Path('.'),args.experiment_id))
  elif args.action=='resume':emit(chain_resume(Path('.'),args.experiment_id))
  elif args.action=='report':emit(chain_report(Path('.'),args.experiment_id))
  elif args.action=='capture-source-config':emit(capture_source_config(Path('.'),args.workload,Path(args.output) if args.output else None))
  else:
   duration=args.duration if args.duration is not None else (5 if args.mode=='quick' else 30)
   warmup=args.warmup if args.warmup is not None else (5 if args.mode=='quick' else 30)
   maximum=args.max_experiments if args.max_experiments is not None else (2 if args.mode=='quick' else 12)
   options=ChainRunOptions(mode=args.mode,duration=duration,warmup=warmup,terminals=args.terminals,max_experiments=maximum,max_restarts=args.max_restarts,max_wall_seconds=args.max_wall_seconds,min_tps_percent=args.min_tps_percent,patience=args.patience,seed=args.seed,source_config_dir=args.source_config_dir,max_p95_regression_percent=args.max_p95_regression_percent,max_rollback_ratio_increase_pp=args.max_rollback_ratio_increase_pp,min_screening_groups=args.min_screening_groups)
   directions=[(args.source,args.target)] if args.action=='run' else parse_directions(args.directions)
   result=run_directions(directions,options,args.experiment_id,require_preflight=os.getenv('ANDROMEDA_PREFLIGHT_PASSED')!='1')
   if os.getenv('ANDROMEDA_CONCISE')=='1':
    emit({'experiment_id':result.get('experiment_id'),'status':result.get('status'),'artifact_dir':result.get('artifact_dir'),'directions':[{'direction':x.get('direction'),'stop_reason':x.get('stop_reason'),'experiments':x.get('experiments'),'failure':x.get('failure'),'restored':x.get('start_config_restored')} for x in result.get('directions',[])]})
   else:emit(result)
 elif args.action=='matrix':emit({'registered':REGISTERED,'active':sorted(ACTIVE)})
 else:
  result=run_transition(args.source,args.target,args.duration,args.terminals,args.experiment_id,not args.strict_llm);emit({'source':result['source'],'target':result['target'],'status':result['status'],'recommendation':result['recommendation'],'comparison':result['comparison'],'report':f'artifacts/transitions/{args.experiment_id}/{args.source}_to_{args.target}/transition_report.json'})
if __name__=='__main__':main()
