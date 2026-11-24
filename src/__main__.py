"""Run a local evaluation and return a CI-friendly exit code."""

import argparse
import json
import sqlite3
import sys

from pydantic import ValidationError

from config.settings import Settings
from src.utils.logging_config import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m src", description=__doc__)
    parser.add_argument("project_path", help="Local project directory")
    parser.add_argument("--db", help="SQLite report path; defaults to TestMirror settings")
    parser.add_argument("--exclude-dir", action="append", default=[], help="Project-relative directory to skip")
    parser.add_argument("--min-coverage", type=float, help="Minimum coverage percentage (0–100)")
    args = parser.parse_args()

    setup_logging(stream=sys.stderr)
    from src.core.deepseek_client import DeepSeekClient
    from src.core.langgraph_orchestrator import WorkflowEngine, summarize

    try:
        settings = Settings()
        with WorkflowEngine(
            args.db if args.db is not None else settings.database_path,
            max_retries=settings.max_retries,
            deepseek_client=DeepSeekClient() if settings.use_deepseek else None,
        ) as engine:
            final = engine.run(
                args.project_path,
                exclude_dirs=args.exclude_dir,
                min_coverage=args.min_coverage,
            )
    except (ValidationError, OSError, sqlite3.Error) as exc:
        print(exc, file=sys.stderr)
        return 2

    print(json.dumps(summarize(final)))
    gate = final["metrics"]["quality_gate"]
    if not final.get("persisted") or gate["reason"] == "execution_failed":
        return 2
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
