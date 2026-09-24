from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from openai import BadRequestError, OpenAI
from pydantic import BaseModel, ValidationError

from app.config.settings import Settings
from app.schemas.models import KnobIdentification, ValueRecommendation

T = TypeVar("T", bound=BaseModel)
LOG = logging.getLogger(__name__)


class DeepSeekReasoner:
    def __init__(self, settings: Settings, artifact_dir: Path, client: OpenAI | None = None):
        self.settings = settings
        self.artifact_dir = artifact_dir
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.client = client or OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )

    def identify(self, payload: dict[str, Any]) -> KnobIdentification:
        return self._structured("knob_identification", payload, KnobIdentification)

    def recommend(self, payload: dict[str, Any]) -> ValueRecommendation:
        return self._structured("value_recommendation", payload, ValueRecommendation)

    def _structured(self, stage: str, payload: dict[str, Any], schema: type[T]) -> T:
        prompt = (
            "You are a cautious PostgreSQL configuration diagnostician. Return only one JSON object "
            f"that validates against this schema: {json.dumps(schema.model_json_schema())}. "
            "Use only supplied knowledge source IDs and evidence metric IDs. Never emit SQL.\nINPUT:\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        response, degraded = self._request(prompt, thinking=True)
        raw = response.choices[0].message.content or ""
        try:
            parsed = schema.model_validate_json(raw)
        except ValidationError as error:
            repair_prompt = prompt + f"\nYour prior output was invalid: {error}. Return corrected JSON only."
            response, repair_degraded = self._request(repair_prompt, thinking=False)
            degraded = degraded or repair_degraded
            raw = response.choices[0].message.content or ""
            parsed = schema.model_validate_json(raw)
        usage = response.usage.model_dump() if response.usage else {}
        record = {
            "stage": stage,
            "called_at": datetime.now(timezone.utc).isoformat(),
            "model": self.settings.model,
            "prompt": prompt,
            "raw_response": raw,
            "parsed": parsed.model_dump(mode="json"),
            "token_usage": usage,
            "compatibility_degraded": degraded,
        }
        (self.artifact_dir / f"{stage}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return parsed

    def _request(self, prompt: str, thinking: bool):
        kwargs = {
            "model": self.settings.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        if thinking:
            kwargs["reasoning_effort"] = "high"
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        try:
            return self.client.chat.completions.create(**kwargs), False
        except BadRequestError:
            if not thinking:
                raise
            LOG.warning("DeepSeek endpoint rejected thinking options; retrying without them")
            kwargs.pop("reasoning_effort", None)
            kwargs.pop("extra_body", None)
            return self.client.chat.completions.create(**kwargs), True
