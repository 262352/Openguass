from __future__ import annotations
import csv,json
from datetime import datetime,timezone
from pathlib import Path

FIELDS=('timestamp','step','workload','phase','index','total','status','throughput_rps','p95_us','objective','knob','old_value','new_value','message','artifact')
class ExperimentReporter:
    def __init__(self,experiment_id,root=Path('artifacts/experiments')):
        self.id=experiment_id;self.dir=root/experiment_id;self.dir.mkdir(parents=True,exist_ok=True);self.csv=self.dir/'progress.csv';self.status_path=self.dir/'live_status.json';self.md=self.dir/'live_progress.md'
    def initialize(self,mode='formal',lhs=15,gp=35,workloads=('tpcc','twitter')):
        if self.status_path.exists():
            data=json.loads(self.status_path.read_text());data['run_state']='resumed';data['updated_at']=self.now()
        else:
            data={'experiment_id':self.id,'mode':mode,'run_state':'initialized','started_at':self.now(),'updated_at':self.now(),'plan':{'workloads':list(workloads),'lhs_per_workload':lhs,'gp_per_workload':gp,'tuning_trials_per_workload':lhs+gp,'total_tuning_trials':(lhs+gp)*len(workloads)},'current':{},'completed_valid_trials':{},'failed_trials':{}}
        self._write_status(data);self.render();return data
    def now(self):return datetime.now(timezone.utc).isoformat(timespec='seconds')
    def event(self,step,workload='',phase='',index='',total='',status='running',throughput_rps='',p95_us='',objective='',knob='',old_value='',new_value='',message='',artifact='',print_line=True):
        row=dict.fromkeys(FIELDS,'');row.update({'timestamp':self.now(),'step':step,'workload':workload,'phase':phase,'index':index,'total':total,'status':status,'throughput_rps':throughput_rps,'p95_us':p95_us,'objective':objective,'knob':knob,'old_value':old_value,'new_value':new_value,'message':message,'artifact':artifact})
        new=not self.csv.exists();
        with self.csv.open('a',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader() if new else None;w.writerow(row)
        data=json.loads(self.status_path.read_text()) if self.status_path.exists() else self.initialize()
        data['updated_at']=row['timestamp'];data['run_state']='running';data['current']={k:row[k] for k in ('step','workload','phase','index','total','status','message')}
        if step=='trial_result':
            bucket='completed_valid_trials' if status=='valid' else 'failed_trials';data.setdefault(bucket,{});data[bucket][workload]=data[bucket].get(workload,0)+1
            if status=='valid':
                best=data.setdefault('best',{}).get(workload);candidate={'phase':phase,'index':index,'throughput_rps':throughput_rps,'p95_us':p95_us,'objective':objective,'artifact':artifact}
                if best is None or float(objective)>float(best['objective']):data['best'][workload]=candidate
        self._write_status(data);self.render()
        if print_line:
            prefix=f'[{step}]'+(f'[{workload}]' if workload else '')+(f'[{phase} {index}/{total}]' if phase and total else '')
            metrics=' '.join(x for x in (f'throughput={float(throughput_rps):.2f}' if throughput_rps!='' else '',f'p95={float(p95_us):.0f}us' if p95_us!='' else '',f'objective={float(objective):.4f}' if objective!='' else '') if x)
            print(f'{prefix} {status} {metrics} {message}'.strip(),flush=True)
        return row
    def finish(self,state='completed',message=''):
        data=json.loads(self.status_path.read_text());data['run_state']=state;data['updated_at']=self.now();data['final_message']=message;self._write_status(data);self.render();return data
    def _write_status(self,data):
        tmp=self.status_path.with_suffix('.json.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.replace(self.status_path)
    def rows(self):
        if not self.csv.exists():return []
        with self.csv.open(encoding='utf-8') as f:return list(csv.DictReader(f))
    def render(self):
        data=json.loads(self.status_path.read_text()) if self.status_path.exists() else {};rows=self.rows();plan=data.get('plan',{});current=data.get('current',{})
        lines=[f"# Experiment {self.id}",'',f"- State: **{data.get('run_state','unknown')}**",f"- Mode: `{data.get('mode','unknown')}`",f"- Updated: {data.get('updated_at','')}",f"- Current: `{current.get('step','-')}` / `{current.get('workload','-')}` / `{current.get('phase','-')}` {current.get('index','')}/{current.get('total','')}",'', '## Tuning progress','', '| Workload | Valid | Failed | Planned | Best objective | Best throughput | Best p95 |','|---|---:|---:|---:|---:|---:|---:|']
        for w in plan.get('workloads',[]):
            best=data.get('best',{}).get(w,{});lines.append(f"| {w} | {data.get('completed_valid_trials',{}).get(w,0)} | {data.get('failed_trials',{}).get(w,0)} | {plan.get('tuning_trials_per_workload','')} | {best.get('objective','')} | {best.get('throughput_rps','')} | {best.get('p95_us','')} |")
        lines+=['','## Latest steps','', '| Time | Step | Workload | Phase | Progress | Status | Throughput | p95 | Objective | Change |','|---|---|---|---|---:|---|---:|---:|---:|---|']
        for row in rows[-30:]:
            change=f"{row['knob']}: {row['old_value']} → {row['new_value']}" if row['knob'] else row['message'];lines.append(f"| {row['timestamp']} | {row['step']} | {row['workload']} | {row['phase']} | {row['index']}/{row['total']} | {row['status']} | {row['throughput_rps']} | {row['p95_us']} | {row['objective']} | {change} |")
        self.md.write_text('\n'.join(lines)+'\n',encoding='utf-8')
