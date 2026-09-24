from __future__ import annotations
import json
FORBIDDEN=('target_reference','target_config','target_optimum','ground_truth','hidden_reference','historical_trials','c_b','d_b')
def assert_no_leakage(payload):
 text=json.dumps(payload,sort_keys=True).lower()
 hits=[x for x in FORBIDDEN if x in text]
 if hits: raise AssertionError(f'leakage guard rejected fields/tokens: {hits}')
 return True
