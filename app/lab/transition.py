from __future__ import annotations
import json
from pathlib import Path
from app.lab.leakage import assert_no_leakage
from app.lab.llm import TransitionLLM
from app.lab.models import TransitionRecord
from app.lab.reporter import ExperimentReporter
from app.lab.space import KNOBS
from app.lab.telemetry import PostgresSampler,analyze
from app.lab.workloads import PostgresBenchBaseAdapter

REGISTERED=[f'{a}->{b}' for a in ('tpcc','twitter','ycsb') for b in ('tpcc','twitter','ycsb') if a!=b]
ACTIVE={'tpcc->twitter','twitter->tpcc'}
def metrics(r):return {'throughput_rps':float(r['throughput_rps']),'p95_us':float(r['latency']['95th Percentile Latency (microseconds)']),'artifact':str(Path(r['summary_file']).parent)}
def clamp(knob,value):
 k=next(k for k in KNOBS if k.name==knob);v=max(k.low,min(k.high,float(value)));return int(round(v)) if k.integer else round(v,4)
def compact_telemetry(value):return {k:v for k,v in value.items() if k!='raw_samples'}

def run_transition(source,target,duration=15,terminals=2,experiment_id='transition',allow_fallback=True):
 key=f'{source}->{target}'
 if key not in REGISTERED:raise ValueError(key)
 if key not in ACTIVE:raise RuntimeError(f'{key} is registered but intentionally inactive')
 out=Path('artifacts/transitions')/experiment_id/f'{source}_to_{target}';out.mkdir(parents=True,exist_ok=True);reporter=ExperimentReporter(experiment_id);reporter.initialize()
 trace=[];raw_telemetry={};src=json.loads((Path('artifacts/references')/source/'reference.json').read_text());source_cfg=src['config'];record=TransitionRecord(source=source,target=target,status='running',state_trace=trace,source_config=source_cfg)
 def measured(label,raw,tel):
  raw_telemetry[label]=tel.get('raw_samples',[]);return {**metrics(raw),'telemetry':compact_telemetry(tel)}
 def persist():
  record.state_trace=list(trace);tmp=(out/'transition_report.json.tmp');tmp.write_text(record.model_dump_json(indent=2));tmp.replace(out/'transition_report.json');(out/'telemetry_samples.json').write_text(json.dumps(raw_telemetry,indent=2))
 try:
  trace.append('SOURCE_STEADY');reporter.event('transition_stage',key,'source_steady',1,6,message='measuring source with source reference');a=PostgresBenchBaseAdapter(source);s=PostgresSampler(source).start();raw=a.run(duration,terminals,source_cfg);tel=analyze(s.stop());a.stop();record.source_result=measured('source_steady',raw,tel);reporter.event('transition_measurement',key,'source_steady',1,6,'valid',record.source_result['throughput_rps'],record.source_result['p95_us'],artifact=record.source_result['artifact']);persist()
  trace.append('TARGET_WITH_SOURCE_CONFIG');reporter.event('transition_stage',key,'target_mismatch',2,6,message='measuring target with source config');b=PostgresBenchBaseAdapter(target);s=PostgresSampler(target).start();raw=b.run(duration,terminals,source_cfg);tel=analyze(s.stop());b.stop();record.transition_result=measured('target_mismatch',raw,tel);reporter.event('transition_measurement',key,'target_mismatch',2,6,'valid',record.transition_result['throughput_rps'],record.transition_result['p95_us'],artifact=record.transition_result['artifact']);persist()
  visible={'objective':{'metric':'target_workload_throughput_rps','direction':'maximize','other_metrics':'diagnostic evidence only'},'transition':key,'current_config':source_cfg,'source_steady':record.source_result,'target_after_switch':record.transition_result,'safe_knobs':[{'name':k.name,'low':k.low,'high':k.high,'integer':k.integer} for k in KNOBS]};assert_no_leakage(visible);record.diagnosis_input=visible;trace.append('COMPOSE_QUESTION');reporter.event('transition_stage',key,'diagnosis',3,6,message='calling DeepSeek question composer and diagnosis')
  llm=TransitionLLM(out/'llm');fallback=False
  try:
   q=llm.compose(visible);record.composed_question=q.model_dump(mode='json');assert_no_leakage(record.composed_question);trace.append('DIAGNOSE');rec=llm.diagnose({'question':record.composed_question,'evidence':visible,'instruction':'Select exactly one safe knob change. Set decision exactly to recommend_change and provide one different in-range value.'});recommendation=rec.model_dump(mode='json')
  except Exception as e:
   if not allow_fallback:raise
   fallback=True;recommendation={'decision':'recommend_change','knob':'random_page_cost','recommended_value':1.5 if target=='twitter' else 4.0,'rationale':'Deterministic safe fallback because the recorded DeepSeek call failed.','confidence':0.0,'evidence_ids':[],'llm_error':f'{type(e).__name__}: {e}'};record.composed_question={'workload_transition':key,'observations':['DeepSeek call failed; see llm_error'],'question':'Evaluate one session-scoped PostgreSQL planner cost change.','evidence_ids':[],'fallback':True}
  knob=recommendation.get('knob');corrected=dict(source_cfg)
  if recommendation.get('decision')!='recommend_change' or knob not in corrected:raise RuntimeError('diagnosis did not select an allowed knob')
  corrected[knob]=clamp(knob,recommendation['recommended_value']);changed=[name for name in corrected if corrected[name]!=source_cfg[name]]
  if changed!=[knob]:raise RuntimeError(f'exactly one changed knob required; got {changed}')
  recommendation['fallback_used']=fallback;recommendation['applied_config']=corrected;recommendation['changed_knobs']=changed;record.recommendation=recommendation;reporter.event('diagnosis_result',key,'diagnosis',3,6,'selected',knob=knob,old_value=source_cfg[knob],new_value=corrected[knob],message=f"confidence={recommendation['confidence']}");persist()
  trace.append('VERIFY_CORRECTION');reporter.event('transition_stage',key,'corrected_target',4,6,message='measuring target after one-knob change');s=PostgresSampler(target).start();raw=b.run(duration,terminals,corrected);tel=analyze(s.stop());b.stop();record.corrected_result=measured('corrected_target',raw,tel);reporter.event('transition_measurement',key,'corrected_target',4,6,'valid',record.corrected_result['throughput_rps'],record.corrected_result['p95_us'],knob=knob,old_value=source_cfg[knob],new_value=corrected[knob],artifact=record.corrected_result['artifact']);persist()
  trace.append('UNBLIND_TARGET_REFERENCE');reporter.event('transition_stage',key,'target_reference',5,6,message='unblinding and measuring target reference');target_ref=json.loads((Path('artifacts/references')/target/'reference.json').read_text());s=PostgresSampler(target).start();raw=b.run(duration,terminals,target_ref['config']);tel=analyze(s.stop());b.stop();record.target_reference_result={**measured('target_reference',raw,tel),'config':target_ref['config']}
  before=record.transition_result;after=record.corrected_result;ref=record.target_reference_result;ref_distribution=target_ref['distribution'];ref['reference_distribution']=ref_distribution;record.comparison={'throughput_change_pct':100*(after['throughput_rps']/before['throughput_rps']-1),'p95_change_pct':100*(after['p95_us']/before['p95_us']-1),'throughput_gap_to_reference_pct':100*(after['throughput_rps']/ref_distribution['throughput_median']-1),'p95_gap_to_reference_pct':100*(after['p95_us']/ref_distribution['p95_median_us']-1),'reference_basis':'stored repeated-reference medians (D_B)'};reporter.event('transition_measurement',key,'target_reference',5,6,'valid',ref['throughput_rps'],ref['p95_us'],artifact=ref['artifact'])
  trace.append('ROLLBACK');b.stop();record.status='passed';reporter.event('transition_complete',key,'rollback',6,6,'passed',after['throughput_rps'],after['p95_us'],knob=knob,old_value=source_cfg[knob],new_value=corrected[knob],message=f"throughput_change={record.comparison['throughput_change_pct']:.2f}% p95_change={record.comparison['p95_change_pct']:.2f}%");persist();return json.loads((out/'transition_report.json').read_text())
 except Exception as e:
  trace.append('ROLLBACK');record.status='failed';record.error=f'{type(e).__name__}: {e}';reporter.event('transition_complete',key,'rollback',6,6,'failed',message=record.error);persist();raise
