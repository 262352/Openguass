from __future__ import annotations
import hashlib,json,os,platform,subprocess
from datetime import datetime,timezone
from pathlib import Path
from app.lab.workloads import PostgresBenchBaseAdapter

def command(args):
 p=subprocess.run(args,text=True,capture_output=True); return p.stdout.strip() if p.returncode==0 else None

def inspect(out=Path('artifacts/environment')):
 out.mkdir(parents=True,exist_ok=True)
 mem={}
 for line in Path('/proc/meminfo').read_text().splitlines():
  if ':' in line:
   k,v=line.split(':',1); mem[k]=v.strip()
 hardware={'captured_at':datetime.now(timezone.utc).isoformat(),'hostname':platform.node(),'kernel':platform.release(),'machine':platform.machine(),'cpu_count':os.cpu_count(),'cpu':command(['lscpu','-J']),'memory':mem,'block_devices':command(['lsblk','-J','-o','NAME,TYPE,SIZE,ROTA,MODEL,MOUNTPOINTS'])}
 pg_version=command(['runuser','-u','postgres','--','psql','-XAt','-d','postgres','-c','select version()'])
 settings=command(['runuser','-u','postgres','--','psql','-XAt','-d','postgres','-c',"select name||'='||setting||case when unit is null then '' else ' '||unit end from pg_settings where name in ('shared_buffers','effective_cache_size','work_mem','effective_io_concurrency','max_parallel_workers_per_gather','random_page_cost') order by name"])
 cgroup='none'; memory_max=Path('/sys/fs/cgroup/memory.max')
 if memory_max.exists(): cgroup=memory_max.read_text().strip()
 fingerprints={w:PostgresBenchBaseAdapter(w).fingerprint() for w in ('tpcc','twitter','ycsb')}
 env={'database':'PostgreSQL','version':pg_version,'settings':settings.splitlines() if settings else [],'benchbase_commit':'33c00473807ebd49304d114a6d769d2d2b2bbb34','resource_scope':{'cgroup_memory_max':cgroup,'effective_source':'host resources' if cgroup in ('none','max') else 'cgroup limit'},'workloads':fingerprints,'formal_budget':{'tpcc':{'lhs':15,'gp':35},'twitter':{'lhs':15,'gp':35},'ycsb':'adapter only; excluded from formal reference tuning'}}
 (out/'hardware_profile.json').write_text(json.dumps(hardware,indent=2)); (out/'environment_profile.json').write_text(json.dumps(env,indent=2))
 report=f"""# Environment report\n\nGenerated: {hardware['captured_at']}\n\n- Database: PostgreSQL (the user-selected runnable target; the supplied prompt's MySQL wording is adapted)\n- Version: {pg_version}\n- CPU count visible to process: {hardware['cpu_count']}\n- Memory: {mem.get('MemTotal','unknown')}\n- Effective resource source: {env['resource_scope']['effective_source']}\n- BenchBase commit: `{env['benchbase_commit']}`\n- Formal reference budget: exactly 15 valid LHS + 35 valid GP trials for TPCC and Twitter\n- YCSB: fully registered and runnable, excluded from the formal 50-trial reference phase\n\nWorkload XML SHA-256 fingerprints are in `environment_profile.json`. Raw `lscpu` and `lsblk` output is preserved in `hardware_profile.json`.\n"""
 (out/'environment_report.md').write_text(report); return {'hardware_profile':str(out/'hardware_profile.json'),'environment_profile':str(out/'environment_profile.json'),'environment_report':str(out/'environment_report.md')}
