import json
from openai import OpenAI
from app.config import load_settings

class Reasoner:
    """Compatibility wrapper for the original Andromeda retrieval pipeline.

    Runtime model/provider settings are centralized in app.config; the dataset and
    retrieval algorithm remain those of the public prototype.
    """
    def __init__(self,dataset,data_path,gpt_model_version=None):
        settings=load_settings(); self.client=OpenAI(api_key=settings.api_key,base_url=settings.base_url,timeout=settings.timeout_seconds,max_retries=settings.max_retries)
        self.model=settings.model; self.data_path=data_path; self.dataset=dataset; self.database=dataset.split('_')[0]
        with open(f'{self.data_path}/dataset/manuals/{self.database}_manuals_data.json') as f:self.manuals_data=json.load(f)
        with open(f'{self.data_path}/dataset/historical_questions/{self.dataset}_retrieval_data.json') as f:self.historical_questions_data=json.load(f)
    def generate_prompt(self,query,retrieved_docs):
        q_text='';m_text='';candidates=set()
        for d_index in retrieved_docs:
            if d_index>=len(self.manuals_data):
                qid=list(self.historical_questions_data)[d_index-len(self.manuals_data)]; item=self.historical_questions_data[qid]; knobs=item['parameter']; candidates.update(knobs); q_text+=f"Question: {item['question']}. Parameters: {knobs}; "
            else:
                mid=list(self.manuals_data)[d_index];item=self.manuals_data[mid];knobs=item['parameters'];candidates.update(knobs);m_text+=f"Manual: {item['text']}. Parameters: {knobs}; "
        return f"Assume you are a DBA. Reference questions: {q_text}. Manuals: {m_text}. Candidate parameters: {sorted(candidates)}. Recommend only referenced parameters for: {query}. Return a JSON array of parameter names."
    def apply(self,prompt):
        response=self.client.chat.completions.create(model=self.model,messages=[{'role':'user','content':prompt}])
        return response.choices[0].message.content
