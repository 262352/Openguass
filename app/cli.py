from __future__ import annotations
import argparse
import json
from pathlib import Path
from app.config import load_settings
from app.retrieval import LocalKnowledgeRetriever
from app.telemetry import DeterministicTelemetryAnalyzer

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Andromeda PostgreSQL configuration debugger")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config-check", help="validate local configuration without calling the API")
    retrieve = sub.add_parser("retrieve", help="search the bundled legacy knowledge base")
    retrieve.add_argument("query")
    analyze = sub.add_parser("analyze", help="analyze two counter snapshots")
    analyze.add_argument("before", type=Path)
    analyze.add_argument("after", type=Path)
    analyze.add_argument("--seconds", type=float, required=True)
    args = parser.parse_args()

    if args.command == "config-check":
        settings = load_settings()
        print(json.dumps({"configured": True, "base_url": settings.base_url, "model": settings.model, "api_key": settings.masked_key}, ensure_ascii=False))
    elif args.command == "retrieve":
        docs = LocalKnowledgeRetriever(ROOT / "knowledge/mysql_official.jsonl").search(args.query)
        print(json.dumps([doc.model_dump() for doc in docs], ensure_ascii=False, indent=2))
    else:
        before = json.loads(args.before.read_text(encoding="utf-8"))
        after = json.loads(args.after.read_text(encoding="utf-8"))
        result = DeterministicTelemetryAnalyzer().analyze(before, after, args.seconds)
        print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
