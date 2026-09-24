import json
from pathlib import Path
import pytest
from app.lab.gp import predict,propose
from app.lab.leakage import assert_no_leakage
from app.lab.space import KNOBS,decode,maximin_lhs,session_settings
from app.lab.transition import ACTIVE,REGISTERED

def test_lhs_is_deterministic_stratified_and_unique():
 a=maximin_lhs(15,42);b=maximin_lhs(15,42)
 assert a==b and len(a)==15 and len({tuple(x) for x in a})==15
 for j in range(len(KNOBS)): assert sorted(int(x[j]*15) for x in a)==list(range(15))

def test_space_decodes_within_bounds_and_pg_units():
 cfg=decode([0,1,.5,.25,.5]);settings=session_settings(cfg)
 assert cfg['random_page_cost']==1.0 and cfg['effective_cache_size_mb']==4096
 assert settings['effective_cache_size'].endswith('MB') and settings['work_mem'].endswith('MB')

def test_gp_prediction_and_ei_candidate():
 xs=[[0.0]*5,[1.0]*5];ys=[0.0,1.0]
 mu,sigma=predict(xs,ys,[.5]*5);x,ei=propose(xs,ys,7,pool=32)
 assert 0<=mu<=1 and sigma>0 and len(x)==5 and ei>=0

def test_leakage_guard_is_fail_closed():
 assert assert_no_leakage({'current_config':{'work_mem':4}})
 with pytest.raises(AssertionError): assert_no_leakage({'target_reference':{'work_mem':8}})

def test_transition_registry_has_six_but_only_two_active():
 assert len(REGISTERED)==6
 assert ACTIVE=={'tpcc->twitter','twitter->tpcc'}

def test_recommendation_requires_exact_change_shape():
 from pydantic import ValidationError
 from app.lab.llm import SafeRecommendation
 with pytest.raises(ValidationError):
  SafeRecommendation(decision='recommend_change',knob=None,recommended_value=None,rationale='x',confidence=.2,evidence_ids=[])
 assert SafeRecommendation(decision='recommend_change',knob='work_mem_mb',recommended_value=8,rationale='x',confidence=.2,evidence_ids=[]).knob=='work_mem_mb'

def test_formal_budget_is_exact_and_resume_does_not_repeat(tmp_path,monkeypatch):
 import app.lab.tuner as mod
 class FakeAdapter:
  calls=0
  def __init__(self,name): self.name=name
  def prepare(self,reload_data=False): return {}
  def stop(self): return {}
  def run(self,seconds,terminals,config):
   FakeAdapter.calls+=1
   score=100.0+sum(float(x) for x in config.values()) if config else 100.0
   return {'throughput_rps':score,'latency':{'95th Percentile Latency (microseconds)':1000.0},'summary_file':str(tmp_path/'fake'/'summary.json')}
 monkeypatch.setattr(mod,'PostgresBenchBaseAdapter',FakeAdapter)
 monkeypatch.setattr(mod,'maximin_lhs',lambda n,seed:[[i/max(n,1)]*len(KNOBS) for i in range(n)])
 monkeypatch.setattr(mod,'propose',lambda xs,ys,seed:([[((len(xs)+1)%97)/97]*len(KNOBS),0.1]))
 tuner=mod.LHSGPTuner('formal-count','tpcc',5,1,root=tmp_path)
 result=tuner.run(15,35,True)
 assert result['executed_budget']=={'lhs':15,'gp':35} and result['valid_total']==50
 calls=FakeAdapter.calls
 resumed=tuner.run(15,35,True)
 assert resumed['valid_total']==50 and FakeAdapter.calls==calls

def test_live_report_separates_human_csv_and_status(tmp_path):
 from app.lab.reporter import ExperimentReporter
 reporter=ExperimentReporter('x',tmp_path)
 reporter.initialize('quick',2,1)
 reporter.event('trial_result','tpcc','lhs',1,2,'valid',12.5,4000,.2,print_line=False)
 status=json.loads(reporter.status_path.read_text())
 assert status['completed_valid_trials']['tpcc']==1
 assert 'tpcc | 1 | 0 | 3' in reporter.md.read_text()
 assert 'throughput_rps' in reporter.csv.read_text().splitlines()[0]

def test_reference_selection_uses_repeat_median_not_one_shot_winner(tmp_path,monkeypatch):
 from app.lab.models import TrialRecord
 from app.lab.tuner import LHSGPTuner
 monkeypatch.setenv('POSTGRES_BENCH_PASSWORD','unit-test-only')
 class FakeAdapter:
  def run(self,seconds,terminals,config):
   throughput=50.0 if config['random_page_cost']==1.0 else 120.0
   return {'throughput_rps':throughput,'latency':{'95th Percentile Latency (microseconds)':100.0},'summary_file':str(tmp_path/'raw'/'summary.json')}
  def stop(self):return {}
  def restore_snapshot(self):return {}
 tuner=LHSGPTuner('robust','tpcc',root=tmp_path)
 tuner.adapter=FakeAdapter();(tuner.dir/'baseline.json').write_text(json.dumps({'throughput_rps':100.0,'p95_us':100.0}))
 base=dict(workload='tpcc',normalized={},started_at='now',duration_seconds=5,terminals=1,valid=True,p95_us=100.0,artifact='x')
 rows=[TrialRecord(trial_id=1,phase='gp',config={'random_page_cost':1.0},throughput_rps=200.0,objective=2.0,**base),TrialRecord(trial_id=2,phase='gp',config={'random_page_cost':2.0},throughput_rps=120.0,objective=1.2,**base)]
 tuner.trials_path.write_text('\n'.join(x.model_dump_json() for x in rows)+'\n')
 result=tuner.select_reference(repeats=3,top_k=2)
 assert result['selected_trial_id']==2
 assert result['selection_method'].startswith('top_2')
