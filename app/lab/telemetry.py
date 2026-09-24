from __future__ import annotations
import json, os, statistics, subprocess, threading, time
from pathlib import Path

FIELDS=('xact_commit','xact_rollback','blks_read','blks_hit','temp_bytes','deadlocks')
class PostgresSampler:
 def __init__(self,database,interval=1.0): self.database=database; self.interval=interval; self.samples=[]; self._stop=threading.Event(); self._thread=None
 def _once(self):
  env=os.environ.copy();
  if not env.get('PGPASSWORD') and env.get('POSTGRES_BENCH_PASSWORD'): env['PGPASSWORD']=env['POSTGRES_BENCH_PASSWORD']
  q="select extract(epoch from clock_timestamp()),xact_commit,xact_rollback,blks_read,blks_hit,temp_bytes,deadlocks from pg_stat_database where datname=current_database()"
  p=subprocess.run(['psql','-XAt','-h',env.get('PGHOST','127.0.0.1'),'-p',env.get('PGPORT','5432'),'-U',env.get('PGUSER','andromeda_bench'),'-d',self.database,'-c',q],env=env,text=True,capture_output=True,timeout=5)
  if p.returncode==0 and p.stdout.strip():
   v=p.stdout.strip().split('|'); self.samples.append({'timestamp':float(v[0]),**{k:float(x) for k,x in zip(FIELDS,v[1:])}})
 def _loop(self):
  while not self._stop.is_set(): self._once(); self._stop.wait(self.interval)
 def start(self): self._once(); self._thread=threading.Thread(target=self._loop,daemon=True); self._thread.start(); return self
 def stop(self):
  self._stop.set();
  if self._thread:self._thread.join(3)
  self._once(); return self.samples

def analyze(samples):
 if len(samples)<2:return {'sample_count':len(samples),'rates':{},'anomalies':['insufficient_telemetry_samples'],'method':'median-trend + MAD generalized-ESD screen'}
 dt=max(samples[-1]['timestamp']-samples[0]['timestamp'],1e-9); rates={k:max(0,samples[-1][k]-samples[0][k])/dt for k in FIELDS}
 rates['buffer_hit_ratio']=rates['blks_hit']/max(1,rates['blks_hit']+rates['blks_read'])
 anomalies=[]
 if rates['buffer_hit_ratio']<0.98 and rates['blks_read']>1: anomalies.append('buffer_hit_ratio_low')
 if rates['xact_rollback']>0.5: anomalies.append('rollback_rate_high')
 if rates['temp_bytes']>1024*1024: anomalies.append('temp_spill_rate_high')
 # Short-window STL analogue: centered median trend, MAD residual threshold (ESD-style robust screen).
 for key in FIELDS:
  vals=[s[key] for s in samples]; diffs=[vals[i]-vals[i-1] for i in range(1,len(vals))]
  if len(diffs)>=3:
   med=statistics.median(diffs); mad=statistics.median(abs(x-med) for x in diffs)
   if mad and max(abs(x-med) for x in diffs)>6*1.4826*mad: anomalies.append(f'{key}_residual_outlier')
 return {'sample_count':len(samples),'rates':rates,'anomalies':sorted(set(anomalies)),'method':'median-trend + MAD generalized-ESD screen','raw_samples':samples}
