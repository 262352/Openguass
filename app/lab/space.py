from __future__ import annotations
import math, random
from dataclasses import dataclass

@dataclass(frozen=True)
class Knob:
    name: str
    low: float
    high: float
    scale: str='linear'
    integer: bool=False
    unit: str=''
    def decode(self,x:float):
        x=min(1.0,max(0.0,x))
        if self.scale=='log2':
            value=2**(math.log2(self.low)+x*(math.log2(self.high)-math.log2(self.low)))
        else: value=self.low+x*(self.high-self.low)
        return int(round(value)) if self.integer else round(value,4)

KNOBS=(
    Knob('random_page_cost',1.0,8.0),
    Knob('effective_cache_size_mb',128,4096,'log2',True,'MB'),
    Knob('work_mem_mb',1,64,'log2',True,'MB'),
    Knob('effective_io_concurrency',0,200,'linear',True),
    Knob('max_parallel_workers_per_gather',0,4,'linear',True),
)

def decode(point:list[float])->dict[str,int|float]:
    return {k.name:k.decode(x) for k,x in zip(KNOBS,point)}

def session_settings(config:dict[str,int|float|str])->dict[str,str]:
    return {
      'random_page_cost':str(config['random_page_cost']),
      'effective_cache_size':f"{config['effective_cache_size_mb']}MB",
      'work_mem':f"{config['work_mem_mb']}MB",
      'effective_io_concurrency':str(config['effective_io_concurrency']),
      'max_parallel_workers_per_gather':str(config['max_parallel_workers_per_gather']),
    }

def maximin_lhs(n:int,seed:int,candidates:int=64)->list[list[float]]:
    if n<1: raise ValueError('n must be positive')
    rng=random.Random(seed); best=None; best_distance=-1.0
    for _ in range(candidates):
        columns=[]
        for _k in KNOBS:
            col=[(i+rng.random())/n for i in range(n)]; rng.shuffle(col); columns.append(col)
        design=[[columns[j][i] for j in range(len(KNOBS))] for i in range(n)]
        distance=min((sum((a-b)**2 for a,b in zip(design[i],design[j])) for i in range(n) for j in range(i)),default=1.0)
        if distance>best_distance: best,best_distance=design,distance
    return best or []
