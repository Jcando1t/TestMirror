"""Classification Agent (LLM-backed): detect project type and test framework.

This agent classifies a project directory by sending a compact digest of its
configuration files to the DeepSeek API and asking for a structured verdict.
It covers the MVP matrix:

    Python  -> pytest
    Node.js -> Jest
    Go      -> go test (``testing`` package)

Unlike :mod:`src.agents.classifier_agent` (which is purely rule-based), this
agent delegates the reasoning to an LLM.  When the LLM is unavailable, it falls
back to the deterministic classifier so the workflow can still make progress.

Output contract: ``{project_type, framework, test_tool, language,
confidence_score, evidence}``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.deepseek_client import DeepSeekClient, DeepSeekError, extract_json
from src.models.state import ClassificationResult, WorkflowState
from src.utils.helpers import truncate, utc_now
from src.utils.logging_config import get_logger

logger = get_logger(__name__)

# Manifest files worth showing the model (in priority order).
_MANIFEST_FILES = (
    "package.json",
    "requirements.txt",
    "go.mod",
    "go.sum",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "pytest.ini",
    "tox.ini",
    "jest.config.js",
    "jest.config.ts",
    "vitest.config.ts",
    "tsconfig.json",
)

_SYSTEM_PROMPT = (
    "You are TestMirror's project classification expert. Classify the given "
    "project into exactly one of the supported categories: python/pytest, "
    "node/jest, or go/testing. Respond with ONLY a JSON object (no markdown, "
    "no prose) with exactly these keys:\n"
    '{\n'
    '  "project_type": "<python|node|go|generic>",\n'
    '  "framework": "<django|flask|fastapi|express|react|vue|next|gin|echo|generic>",\n'
    '  "test_tool": "<pytest|jest|go-test|unittest|vitest|mocha|npm-test>",\n'
    '  "language": "<python|javascript|typescript|go>",\n'
    '  "confidence_score": <float between 0.0 and 1.0>,\n'
    '  "evidence": ["<short reason>", ...]\n'
    "}\n"
    "Base your decision on the configuration files shown; prefer the most "
    "specific test tool actually configured (e.g. jest over npm-test)."
)

_PROJECT_TYPE_ALIASES = {
    "python": "python", "py": "python",
    "node": "node", "nodejs": "node", "node.js": "node",
    "javascript": "node", "typescript": "node", "js": "node",
    "go": "go", "golang": "go",
    "generic": "generic", "unknown": "unknown", "": "unknown", None: "unknown",
}

_TEST_TOOL_ALIASES = {
    "pytest": "pytest", "unittest": "unittest", "nose": "nose",
    "jest": "jest", "vitest": "vitest", "mocha": "mocha", "jasmine": "jasmine",
    "go-test": "go-test", "go test": "go-test", "gotest": "go-test",
    "go/testing": "go-test", "testing": "go-test",
    "npm-test": "npm-test", "generic": "generic", "": "unknown", None: "unknown",
}

_LANGUAGE_ALIASES = {
    "python": "python", "py": "python",
    "javascript": "javascript", "js": "javascript",
    "typescript": "typescript", "ts": "typescript",
    "go": "go", "golang": "go",
    "": "unknown", None: "unknown",
}


def _read_text(path: Path, limit: int = 8_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


class ClassificationAgent:
    """Classify a project directory into type / framework / test tool / language."""

    def __init__(
        self,
        client: Optional[DeepSeekClient] = None,
        model: Optional[str] = None,
        *,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> None:
        self.client = client or DeepSeekClient(model=model)
        self.temperature = temperature
        self.max_tokens = max_tokens

    # ---- public API ------------------------------------------------------ #
    def classify(self, project_path: str) -> ClassificationResult:
        """Return the structured classification for ``project_path``.

        Never raises for recoverable LLM problems: it degrades to the
        rule-based classifier and logs a warning.
        """
        root = Path(project_path).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Project directory not found: {root}")

        fallback = self._rule_based(project_path)
        digest = self._collect_digest(root)
        try:
            text = self._generate(digest)
            result = self._parse(text, fallback)
            logger.info(
                "LLM classification: type=%s tool=%s language=%s (%.2f)",
                result["project_type"], result["test_tool"],
                result["language"], result["confidence_score"],
            )
            return result
        except (DeepSeekError, ValueError) as exc:
            logger.warning(
                "LLM classification failed (%s); falling back to rule-based classifier",
                type(exc).__name__,
            )
            return fallback

    def run(self, state: WorkflowState) -> Dict[str, Any]:
        """LangGraph node: classify and publish results to the shared state."""
        logger.info("Classifying project (LLM): %s", state.get("project_path"))
        result = self.classify(state["project_path"])
        return {
            "project_type": result["project_type"],
            "framework": result["framework"],
            "test_tool": result["test_tool"],
            "language": result["language"],
            "classification_confidence": result["confidence_score"],
            "classification_evidence": result["evidence"],
            "current_agent": "classifier",
            "logs": [
                f"[{utc_now()}] Classified project as "
                f"{result['project_type']}/{result['test_tool']} "
                f"(confidence={result['confidence_score']})"
            ],
        }

    # ---- internals ------------------------------------------------------- #
    def _collect_digest(self, root: Path) -> Dict[str, Any]:
        manifests: List[Dict[str, str]] = []
        for name in _MANIFEST_FILES:
            path = root / name
            if path.is_file():
                manifests.append({"file": name, "content": _read_text(path)})

        try:
            top_level = sorted(
                entry.name for entry in root.iterdir() if not entry.name.startswith(".")
            )[:200]
        except OSError:
            top_level = []

        return {
            "project_path": str(root),
            "top_level_entries": top_level,
            "config_files": manifests,
        }

    def _generate(self, digest: Dict[str, Any]) -> str:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": truncate(_json(digest), 24_000)},
        ]
        kwargs: Dict[str, Any] = {
            "json_mode": True,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        stream_method = getattr(self.client, "stream", None)
        if callable(stream_method):
            stream = stream_method(messages, **kwargs)
            chunks: List[str] = []
            for chunk in stream:
                if isinstance(chunk, str):
                    chunks.append(chunk)
            accumulated = getattr(stream, "text", None)
            return accumulated if accumulated is not None else "".join(chunks)

        response = self.client.chat(messages, **kwargs)
        content = getattr(response, "content", None)
        if content is not None:
            return str(content)
        return str(response)

    def _parse(self, text: str, fallback: ClassificationResult) -> ClassificationResult:
        try:
            data = extract_json(text)
        except ValueError:
            raise
        if not isinstance(data, dict):
            raise ValueError("Classification response is not a JSON object")

        project_type = _PROJECT_TYPE_ALIASES.get(str(data.get("project_type", "")).strip().lower(), "unknown")
        test_tool = _TEST_TOOL_ALIASES.get(str(data.get("test_tool", "")).strip().lower(), "unknown")
        language = _LANGUAGE_ALIASES.get(str(data.get("language", "")).strip().lower(), "unknown")

        # Prefer the concrete rule-based answer whenever the model is vague.
        if project_type in {"generic", "unknown"} and fallback.get("project_type") not in {"generic", "unknown"}:
            project_type = fallback["project_type"]
        if test_tool in {"generic", "unknown"}:
            test_tool = fallback.get("test_tool", "generic")
        if language in {"unknown"}:
            language = fallback.get("language", "unknown")

        evidence = data.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = [str(evidence)]
        evidence = [str(e) for e in evidence]

        return ClassificationResult(
            project_type=project_type,
            framework=str(data.get("framework") or fallback.get("framework", "generic")),
            test_tool=test_tool,
            language=language,
            confidence_score=self._clamp_confidence(data.get("confidence_score")),
            evidence=evidence or list(fallback.get("evidence", [])),
        )

    @staticmethod
    def _clamp_confidence(value: Any) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            score = 0.0
        return round(min(1.0, max(0.0, score)), 4)

    def _rule_based(self, project_path: str) -> ClassificationResult:
        from src.agents.classifier_agent import classify_project

        raw = classify_project(project_path)
        return ClassificationResult(
            project_type=raw.get("project_type", "generic"),
            framework=raw.get("framework", "generic"),
            test_tool=raw.get("test_tool", "generic"),
            language=raw.get("language", "unknown"),
            confidence_score=float(raw.get("confidence", 0.0)),
            evidence=list(raw.get("evidence", [])),
        )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def classification_node(state: WorkflowState, client: Optional[DeepSeekClient] = None) -> Dict[str, Any]:
    """Module-level LangGraph node that uses a default (env-configured) client."""
    return ClassificationAgent(client=client).run(state)
