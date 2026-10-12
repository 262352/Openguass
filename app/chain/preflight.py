from __future__ import annotations

import shutil
import time
from pathlib import Path

from openai import APIConnectionError, OpenAI
from pydantic import BaseModel

from app.config import load_settings
from app.lab.workloads import PostgresBenchBaseAdapter
from app.phase2.executor import PostgreSQLKnobExecutor
from app.phase2.space import FullKnobSpace
from app.phase2.telemetry import DiagnosticSampler


class _Reply(BaseModel):
    status: str


def chain_preflight(root: Path = Path(".")) -> dict:
    checks: dict[str, dict] = {}
    for workload in ("tpcc", "twitter"):
        snapshot = root / "artifacts" / "snapshots" / f"{workload}.dump"
        checks[f"snapshot_{workload}"] = {"ok": snapshot.is_file() and snapshot.stat().st_size > 0, "path": str(snapshot)}
    try:
        adapter = PostgresBenchBaseAdapter("tpcc")
        checks["postgresql_benchbase"] = {"ok": bool(adapter.health_check().get("healthy"))}
    except Exception as error:
        checks["postgresql_benchbase"] = {"ok": False, "error_type": type(error).__name__}
    try:
        space = FullKnobSpace()
        checks["knob_catalog"] = {"ok": len(space.names) == 60, "count": len(space.names)}
    except Exception as error:
        checks["knob_catalog"] = {"ok": False, "error_type": type(error).__name__}
    try:
        sampler = DiagnosticSampler("tpcc", interval=.2).start(); time.sleep(.3); samples = sampler.stop()
        checks["telemetry"] = {"ok": len(samples) >= 2, "sample_count": len(samples)}
    except Exception as error:
        checks["telemetry"] = {"ok": False, "error_type": type(error).__name__}
    free = shutil.disk_usage(root).free
    checks["disk_space"] = {"ok": free >= 2 * 1024**3, "free_bytes": free, "minimum_bytes": 2 * 1024**3}
    try:
        executor = PostgreSQLKnobExecutor(root / "artifacts" / "v3_v4" / "preflight" / "system")
        captured = executor.capture()
        checks["rollback_backup"] = {"ok": len(captured) == 60 and executor.backup.is_file(), "captured_knobs": len(captured)}
    except Exception as error:
        checks["rollback_backup"] = {"ok": False, "error_type": type(error).__name__}
    try:
        settings = load_settings()
        if settings.model != "deepseek-flash" or settings.base_url.rstrip("/") != "https://api.deepseek.com":
            raise RuntimeError("chain model endpoint must be deepseek-flash at https://api.deepseek.com")
        client = OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=30, max_retries=0)
        response = None
        attempts = 0
        for attempts in range(1, 4):
            try:
                response = client.chat.completions.create(
                    model=settings.model,
                    messages=[{"role":"system","content":"Return strict JSON immediately. Do not analyze."},{"role":"user","content":"Return exactly this JSON object: {\"status\":\"ok\"}"}],
                    response_format={"type":"json_object"}, max_tokens=1024,
                )
                break
            except APIConnectionError:
                if attempts == 3:
                    raise
                time.sleep(attempts)
        assert response is not None
        raw = response.choices[0].message.content or ""
        parsed = _Reply.model_validate_json(raw)
        checks["deepseek_structured"] = {"ok": parsed.status == "ok", "model": settings.model, "base_url": settings.base_url,
                                         "attempts": attempts}
    except Exception as error:
        checks["deepseek_structured"] = {"ok": False, "error_type": type(error).__name__, "http_status": getattr(error, "status_code", None)}
    ok = all(item.get("ok", False) for item in checks.values())
    return {"status": "passed" if ok else "preflight_failed", "ok": ok, "checks": checks}
