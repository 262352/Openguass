from __future__ import annotations
import json,re
from datetime import datetime,timezone
from pathlib import Path

def report(root=Path('.'),out=Path('artifacts/model_migration_report.json')):
    violations=[]; historical=[]
    pattern=r'(?i)(gpt-[\w.-]+|openai_api_key|chatcompletion\.create)'
    for path in root.rglob('*'):
        if path.resolve()==Path(__file__).resolve(): continue
        if not path.is_file() or any(x in path.parts for x in ('.git','.benchbase','artifacts','__pycache__')): continue
        if path.suffix not in ('.py','.toml','.yaml','.yml','.json','.md','.ipynb'): continue
        try: text=path.read_text(errors='ignore')
        except OSError: continue
        hits=sorted(set(re.findall(pattern,text)))
        if hits:
            item={'path':str(path),'tokens':hits}
            (historical if path.suffix in ('.md','.ipynb') else violations).append(item)
    data={'generated_at':datetime.now(timezone.utc).isoformat(),'status':'passed' if not violations else 'failed','required_runtime':{'provider':'DeepSeek','model':'deepseek-flash','base_url':'https://api.deepseek.com','key_source':'DEEPSEEK_API_KEY'},'executable_legacy_violations':violations,'historical_references':historical,'migrated_executable_files':['core/reasoner.py','app/diagnosis/reasoner.py','app/lab/llm.py'],'policy':'The OpenAI-compatible SDK may call DeepSeek. Executable legacy GPT model names, OPENAI_API_KEY, and legacy ChatCompletion.create are violations. Historical papers and notebooks remain unchanged.'}
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(data,indent=2));return data
