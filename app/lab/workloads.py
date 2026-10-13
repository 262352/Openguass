from __future__ import annotations
import hashlib,os,subprocess,time
from pathlib import Path
from app.benchmark.benchbase import BenchBaseRunner
from app.lab.space import session_settings
from app.phase2.space import FullKnobSpace

ROOT=Path(__file__).resolve().parents[2]
class PostgresBenchBaseAdapter:
    def __init__(self,name,runner=None):
        if name not in ('tpcc','twitter','ycsb'):raise ValueError(name)
        self.name=name;self.runner=runner or BenchBaseRunner();self._active={};self.snapshot=ROOT/'artifacts/snapshots'/f'{name}.dump'
    def _env(self):
        env=os.environ.copy();env['PGPASSWORD']=self.runner.target.password;env['LANG']='C';env['LC_ALL']='C';env.pop('LANGUAGE',None);env.pop('LC_CTYPE',None);return env
    def prepare(self,reload_data=False):
        self.health_check()
        valid_snapshot=self.snapshot.is_file() and self.snapshot.stat().st_size > 0
        reused=bool(reload_data and valid_snapshot)
        if reload_data and not valid_snapshot:
            if self.snapshot.exists():self.snapshot.unlink()
            self.snapshot.parent.mkdir(parents=True,exist_ok=True);temporary=self.snapshot.with_suffix('.dump.tmp');started=time.monotonic()
            print(f'[snapshot] creating {self.name}: {self.snapshot}',flush=True)
            try:
                subprocess.run(['pg_dump','-Fc','-h',self.runner.target.host,'-p',str(self.runner.target.port),'-U',self.runner.target.user,'-d',self.name,'-f',str(temporary)],env=self._env(),check=True)
                temporary.replace(self.snapshot)
            finally:
                if temporary.exists():temporary.unlink()
            print(f'[snapshot] created {self.name} in {time.monotonic()-started:.1f}s',flush=True)
        elif reload_data:
            print(f'[snapshot] reusing existing {self.name}: {self.snapshot}',flush=True)
        return {'prepared':True,'snapshot':str(self.snapshot) if self.snapshot.exists() else None,'snapshot_reused':reused}
    def restore_snapshot(self):
        if not self.snapshot.is_file() or self.snapshot.stat().st_size == 0:raise RuntimeError(f'missing or empty snapshot {self.snapshot}; run workload prepare --reload')
        started=time.monotonic();print(f'[snapshot] restoring {self.name} (transactional): {self.snapshot}',flush=True)
        subprocess.run(['pg_restore','--clean','--if-exists','--no-owner','--no-privileges','--single-transaction','--exit-on-error','-h',self.runner.target.host,'-p',str(self.runner.target.port),'-U',self.runner.target.user,'-d',self.name,str(self.snapshot)],env=self._env(),check=True,stdout=subprocess.DEVNULL)
        elapsed=time.monotonic()-started;print(f'[snapshot] restored {self.name} in {elapsed:.1f}s',flush=True)
        return {'restored':True,'snapshot':str(self.snapshot),'elapsed_seconds':round(elapsed,3),'transactional':True}
    def warmup(self,seconds=5,terminals=1,seed=None):return self.run(seconds,terminals,seed=seed)
    def run(self,seconds=15,terminals=2,config=None,seed=None):
        self._active=dict(config or {})
        settings = None
        if self._active:
            if "effective_cache_size_mb" in self._active:
                settings = session_settings(self._active)
            else:
                dimensions = {item.name: item for item in FullKnobSpace().dimensions}
                settings = {name: str(value) for name, value in self._active.items()
                            if name in dimensions and dimensions[name].execution_class == "session"}
        return self.runner.run(self.name,seconds,terminals,False,settings,verbose=False,random_seed=seed)
    def stop(self):self._active={};return {'stopped':True,'session_settings_cleared':True}
    def parse_result(self,result):
        latency=result['latency']
        return {'throughput_rps':float(result['throughput_rps']),
                'p95_us':float(latency['95th Percentile Latency (microseconds)']),
                'average_us':float(latency.get('Average Latency (microseconds)',0) or 0),
                'p99_us':float(latency.get('99th Percentile Latency (microseconds)',0) or 0),
                'max_us':float(latency.get('Maximum Latency (microseconds)',0) or 0),
                'transaction_latency':result.get('transaction_latency',{})}
    def fingerprint(self):
        path=ROOT/f'benchmarks/postgres/{self.name}.xml';return {'workload':self.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'config':str(path.relative_to(ROOT))}
    def health_check(self):self.runner.health();return {'healthy':True}
