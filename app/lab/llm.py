from __future__ import annotations
import json
from pathlib import Path
from typing import Literal
from openai import OpenAI
from pydantic import BaseModel,Field,model_validator
from app.config import load_settings

class ComposedQuestion(BaseModel):
    workload_transition:str
    observations:list[str]
    question:str
    evidence_ids:list[str]

class SafeRecommendation(BaseModel):
    decision:Literal['recommend_change']='recommend_change'
    knob:str|None=None
    recommended_value:float|int|None=None
    rationale:str
    confidence:float=Field(ge=0,le=1)
    evidence_ids:list[str]
    @model_validator(mode='after')
    def require_change(self):
        if self.knob is None or self.recommended_value is None:
            raise ValueError('the controlled transition experiment requires exactly one knob and value')
        return self

class TransitionLLM:
    def __init__(self,artifact_dir:Path):
        self.settings=load_settings();self.client=OpenAI(api_key=self.settings.api_key,base_url=self.settings.base_url,timeout=self.settings.timeout_seconds,max_retries=self.settings.max_retries);self.dir=artifact_dir;self.dir.mkdir(parents=True,exist_ok=True)
    def _call(self,stage,payload,schema):
        prompt='You are the PostgreSQL diagnosis component in Andromeda. Use only the supplied evidence. Do not infer or request any hidden target reference, target optimum, historical trial, label, or ground truth. Return one JSON object matching this schema: '+json.dumps(schema.model_json_schema())+'\nINPUT:\n'+json.dumps(payload,sort_keys=True)
        resp=self.client.chat.completions.create(model=self.settings.model,messages=[{'role':'user','content':prompt}],response_format={'type':'json_object'})
        raw=resp.choices[0].message.content or '{}';parsed=schema.model_validate_json(raw)
        record={'model':self.settings.model,'base_url':self.settings.base_url,'prompt':prompt,'raw_response':raw,'parsed':parsed.model_dump(mode='json'),'usage':resp.usage.model_dump() if resp.usage else {}}
        (self.dir/f'{stage}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2));return parsed
    def compose(self,payload):return self._call('question_composer',payload,ComposedQuestion)
    def diagnose(self,payload):return self._call('diagnosis',payload,SafeRecommendation)
