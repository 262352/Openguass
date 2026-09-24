from __future__ import annotations
import json, math, statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from app.lab.models import TrialRecord, ReferenceRecord
from app.lab.space import KNOBS, decode, maximin_lhs
from app.lab.gp import propose
from app.lab.workloads import PostgresBenchBaseAdapter
from app.lab.reporter import ExperimentReporter

P95='95th Percentile Latency (microseconds)'

def append_jsonl(path:Path,obj:dict):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a',encoding='utf-8') as f: f.write(json.dumps(obj,ensure_ascii=False)+'\n')

def objective(metrics:dict,baseline:dict)->float:
    # The declared optimization target is TPS only. Latency and telemetry are evidence.
    return float(metrics['throughput_rps'])/max(float(baseline['throughput_rps']),1e-9)

def _metrics(result): return {'throughput_rps':float(result['throughput_rps']),'p95_us':float(result['latency'][P95])}

class LHSGPTuner:
  def __init__(self,experiment_id:str,workload:str,duration:int=15,terminals:int=2,seed:int=20260921,root:Path=Path('artifacts/experiments'),snapshot_isolation:bool=False):
    self.workload=workload; self.duration=duration; self.terminals=terminals; self.seed=seed
    self.dir=root/experiment_id/workload; self.dir.mkdir(parents=True,exist_ok=True)
    self.trials_path=self.dir/'trials.jsonl'; self.checkpoint=self.dir/'checkpoint.json'
    self.adapter=PostgresBenchBaseAdapter(workload); self.snapshot_isolation=snapshot_isolation; self.reporter=ExperimentReporter(experiment_id,root)
  def existing(self):
    if not self.trials_path.exists(): return []
    return [TrialRecord.model_validate_json(x) for x in self.trials_path.read_text().splitlines() if x.strip()]
  def _run(self,phase,point,trial_id,baseline,phase_index,phase_total):
    cfg=decode(point); self.reporter.event('trial_start',self.workload,phase,phase_index,phase_total,message=f'trial_id={trial_id}')
    if self.snapshot_isolation: self.adapter.restore_snapshot()
    now=datetime.now(timezone.utc).isoformat()
    try:
      raw=self.adapter.run(self.duration,self.terminals,cfg); met=_metrics(raw); score=objective(met,baseline)
      rec=TrialRecord(trial_id=trial_id,phase=phase,workload=self.workload,normalized={k.name:x for k,x in zip(KNOBS,point)},config=cfg,started_at=now,duration_seconds=self.duration,terminals=self.terminals,valid=True,throughput_rps=met['throughput_rps'],p95_us=met['p95_us'],objective=score,artifact=str(Path(raw['summary_file']).parent))
    except Exception as e:
      rec=TrialRecord(trial_id=trial_id,phase=phase,workload=self.workload,normalized={k.name:x for k,x in zip(KNOBS,point)},config=cfg,started_at=now,duration_seconds=self.duration,terminals=self.terminals,valid=False,error=f'{type(e).__name__}: {e}')
    append_jsonl(self.trials_path,rec.model_dump(mode='json')); self.adapter.stop(); self.reporter.event('trial_result',self.workload,phase,phase_index,phase_total,'valid' if rec.valid else 'invalid',rec.throughput_rps or '',rec.p95_us or '',rec.objective if rec.objective is not None else '',message=rec.error or '',artifact=rec.artifact or ''); return rec
  def run(self,lhs_count=15,gp_count=35,resume=True,max_invalid=20):
    self.reporter.initialize('formal' if (lhs_count,gp_count)==(15,35) else 'quick',lhs_count,gp_count); self.adapter.prepare(False)
    baseline_path=self.dir/'baseline.json'
    if resume and baseline_path.exists(): baseline=json.loads(baseline_path.read_text())
    else:
      
      if self.snapshot_isolation: self.adapter.restore_snapshot()
      self.reporter.event('baseline_start',self.workload,status='running',message='measuring untuned baseline')
      raw=self.adapter.run(self.duration,self.terminals,{}); baseline=_metrics(raw); baseline['artifact']=str(Path(raw['summary_file']).parent); baseline_path.write_text(json.dumps(baseline,indent=2)); self.adapter.stop(); self.reporter.event('baseline_result',self.workload,status='valid',throughput_rps=baseline['throughput_rps'],p95_us=baseline['p95_us'],artifact=baseline['artifact'])
    records=self.existing() if resume else []
    valid_lhs=[r for r in records if r.valid and r.phase=='lhs']; valid_gp=[r for r in records if r.valid and r.phase=='gp']
    xs=[[r.normalized[k.name] for k in KNOBS] for r in records if r.valid]
    ys=[r.objective for r in records if r.valid and r.objective is not None]
    trial_id=max([r.trial_id for r in records],default=0)+1; invalid=sum(not r.valid for r in records)
    design=maximin_lhs(lhs_count,self.seed)
    for point in design[len(valid_lhs):]:
      while True:
        rec=self._run('lhs',point,trial_id,baseline,len(valid_lhs)+1,lhs_count); trial_id+=1
        if rec.valid: xs.append(point); ys.append(rec.objective); valid_lhs.append(rec); break
        invalid+=1
        if invalid>max_invalid: raise RuntimeError('invalid trial budget exhausted')
        point=maximin_lhs(1,self.seed+trial_id)[0]
    while len(valid_gp)<gp_count:
      point,ei=propose(xs,ys,self.seed+trial_id)
      rec=self._run('gp',point,trial_id,baseline,len(valid_gp)+1,gp_count); trial_id+=1
      if rec.valid: xs.append(point); ys.append(rec.objective); valid_gp.append(rec)
      else:
        invalid+=1
        if invalid>max_invalid: raise RuntimeError('invalid trial budget exhausted')
      self.checkpoint.write_text(json.dumps({'valid_lhs':len(valid_lhs),'valid_gp':len(valid_gp),'last_trial_id':trial_id-1,'seed':self.seed},indent=2))
    result={'experiment_id':self.dir.parent.name,'workload':self.workload,'objective':{'metric':'throughput_rps','direction':'maximize','formula':'throughput_rps / baseline_throughput_rps'},'formal_budget':{'lhs':15,'gp':35},'executed_budget':{'lhs':len(valid_lhs),'gp':len(valid_gp)},'valid_total':len(valid_lhs)+len(valid_gp),'baseline':baseline,'trials':str(self.trials_path)}
    (self.dir/'tuning_summary.json').write_text(json.dumps(result,indent=2)); return result
  def select_reference(self,repeats=5,top_k=5):
    valid=sorted((r for r in self.existing() if r.valid and r.objective is not None),key=lambda r:r.objective,reverse=True)
    if not valid: raise RuntimeError('no valid trials')
    baseline=json.loads((self.dir/'baseline.json').read_text()); shortlist=[]
    for rank,candidate in enumerate(valid[:min(top_k,len(valid))],1):
      results=[]
      self.reporter.event('reference_candidate',self.workload,'shortlist',rank,min(top_k,len(valid)),status='selected',objective=candidate.objective,message=f'trial_id={candidate.trial_id} one_shot_rank={rank}')
      for repeat_index in range(1,repeats+1):
        if self.snapshot_isolation:self.adapter.restore_snapshot()
        self.reporter.event('reference_repeat_start',self.workload,f'candidate_{rank}',repeat_index,repeats,message=f'trial_id={candidate.trial_id}')
        raw=self.adapter.run(self.duration,self.terminals,candidate.config);repeat={**_metrics(raw),'artifact':str(Path(raw['summary_file']).parent)};repeat['objective']=objective(repeat,baseline);results.append(repeat)
        self.reporter.event('reference_repeat_result',self.workload,f'candidate_{rank}',repeat_index,repeats,'valid',repeat['throughput_rps'],repeat['p95_us'],repeat['objective'],message=f'trial_id={candidate.trial_id}',artifact=repeat['artifact'])
      throughputs=[r['throughput_rps'] for r in results];p95=[r['p95_us'] for r in results];median_t=statistics.median(throughputs);median_p=statistics.median(p95);robust=objective({'throughput_rps':median_t,'p95_us':median_p},baseline)
      shortlist.append({'rank_by_one_shot':rank,'trial_id':candidate.trial_id,'config':candidate.config,'one_shot_objective':candidate.objective,'robust_objective':robust,'repeat_results':results,'distribution':{'throughput_median':median_t,'throughput_stdev':statistics.stdev(throughputs) if len(throughputs)>1 else 0.0,'p95_median_us':median_p,'p95_stdev_us':statistics.stdev(p95) if len(p95)>1 else 0.0}})
    self.adapter.stop();winner=max(shortlist,key=lambda item:item['robust_objective'])
    ref=ReferenceRecord(workload=self.workload,selected_trial_id=winner['trial_id'],config=winner['config'],objective=winner['robust_objective'],one_shot_objective=winner['one_shot_objective'],selection_method=f'top_{len(shortlist)}_by_one_shot_then_{repeats}_repeats_each_max_median_objective',shortlist=shortlist,repeat_results=winner['repeat_results'],distribution=winner['distribution'])
    out=Path('artifacts/references')/self.workload;out.mkdir(parents=True,exist_ok=True);(out/'reference.json').write_text(ref.model_dump_json(indent=2));self.reporter.event('reference_selected',self.workload,status='selected',objective=ref.objective,throughput_rps=ref.distribution['throughput_median'],p95_us=ref.distribution['p95_median_us'],message=f'trial_id={ref.selected_trial_id} method=robust_top_k');return ref.model_dump(mode='json')
