from __future__ import annotations
import hashlib,os,subprocess,time
from pathlib import Path
from app.benchmark.benchbase import BenchBaseRunner
from app.lab.space import session_settings

ROOT=Path(__file__).resolve().parents[2]
class PostgresBenchBaseAdapter:
    def __init__(self,name,runner=None):
        if name not in ('tpcc','twitter','ycsb'):raise ValueError(name)
        self.name=name;self.runner=runner or BenchBaseRunner();self._active={};self.snapshot=ROOT/'artifacts/snapshots'/f'{name}.dump'
    def _env(self):
        env=os.environ.copy();env['PGPASSWORD']=self.runner.target.password;env['LANG']='C';env['LC_ALL']='C';env.pop('LANGUAGE',None);env.pop('LC_CTYPE',None);return env
    def prepare(self,reload_data=False):
        self.health_check()
        reused=bool(reload_data and self.snapshot.exists())
        if reload_data and not self.snapshot.exists():
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
        if not self.snapshot.exists():raise RuntimeError(f'missing snapshot {self.snapshot}; run workload prepare --reload')
        started=time.monotonic();print(f'[snapshot] restoring {self.name} (transactional): {self.snapshot}',flush=True)
        subprocess.run(['pg_restore','--clean','--if-exists','--no-owner','--no-privileges','--single-transaction','--exit-on-error','-h',self.runner.target.host,'-p',str(self.runner.target.port),'-U',self.runner.target.user,'-d',self.name,str(self.snapshot)],env=self._env(),check=True,stdout=subprocess.DEVNULL)
        elapsed=time.monotonic()-started;print(f'[snapshot] restored {self.name} in {elapsed:.1f}s',flush=True)
        return {'restored':True,'snapshot':str(self.snapshot),'elapsed_seconds':round(elapsed,3),'transactional':True}
    def warmup(self,seconds=5,terminals=1):return self.run(seconds,terminals)
    def run(self,seconds=15,terminals=2,config=None):
        self._active=dict(config or {});return self.runner.run(self.name,seconds,terminals,False,session_settings(self._active) if self._active else None,verbose=False)
    def stop(self):self._active={};return {'stopped':True,'session_settings_cleared':True}
    def parse_result(self,result):return {'throughput_rps':float(result['throughput_rps']),'p95_us':float(result['latency']['95th Percentile Latency (microseconds)'])}
    def fingerprint(self):
        path=ROOT/f'benchmarks/postgres/{self.name}.xml';return {'workload':self.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'config':str(path.relative_to(ROOT))}
    def health_check(self):self.runner.health();return {'healthy':True}
