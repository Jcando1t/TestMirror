"""Classification Agent: detect project type, framework, test tool and language.

This is the first node of the TestMirror workflow.  Its output contract is the
input contract of the Planning Agent:

    {project_type, framework, test_tool, language}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from src.models.state import WorkflowState
from src.utils.helpers import utc_now
from src.utils.logging_config import get_logger
from src.utils.security import COPY_EXCLUDES, find_files

logger = get_logger(__name__)

# Marker file -> (project_type, framework, test_tool, language)
MARKERS: Dict[str, Tuple[str, str, str, str]] = {
    "pyproject.toml": ("python", "generic", "pytest", "python"),
    "pytest.ini": ("python", "generic", "pytest", "python"),
    "tox.ini": ("python", "generic", "pytest", "python"),
    "setup.cfg": ("python", "generic", "pytest", "python"),
    "requirements.txt": ("python", "generic", "pytest", "python"),
    "package.json": ("node", "generic", "jest", "javascript"),
    "tsconfig.json": ("node", "generic", "jest", "typescript"),
    "pom.xml": ("java", "maven", "maven-surefire", "java"),
    "build.gradle": ("java", "gradle", "gradle-test", "java"),
    "build.gradle.kts": ("java", "gradle", "gradle-test", "kotlin"),
    "go.mod": ("go", "generic", "go-test", "go"),
    "Cargo.toml": ("rust", "generic", "cargo-test", "rust"),
    "Gemfile": ("ruby", "rails", "rspec", "ruby"),
    "composer.json": ("php", "generic", "phpunit", "php"),
    "*.csproj": ("dotnet", "generic", "dotnet-test", "csharp"),
}

FRAMEWORK_HINTS: Dict[str, str] = {
    "django": "django", "manage.py": "django",
    "flask": "flask", "fastapi": "fastapi",
    "react": "react", "vue": "vue", "angular": "angular", "next": "next.js",
    "express": "express", "spring": "spring", "springboot": "spring-boot",
    "gin": "gin", "echo": "echo", "actix": "actix", "rocket": "rocket",
}

TEST_TOOL_HINTS: Dict[str, str] = {
    "jest": "jest", "vitest": "vitest", "mocha": "mocha", "jasmine": "jasmine",
    "pytest": "pytest", "unittest": "unittest", "nose": "nose",
    "surefire": "maven-surefire", "junit": "junit",
    "rspec": "rspec", "phpunit": "phpunit", "xunit": "xunit",
}


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _detect_from_manifest(root: Path, evidence: List[str]) -> Dict[str, Any] | None:
    """Inspect package.json / pyproject.toml for concrete framework + test tool."""
    package_json = root / "package.json"
    if package_json.is_file():
        manifest = json.loads(_read_text(package_json) or "{}")
        deps = {
            **manifest.get("dependencies", {}),
            **manifest.get("devDependencies", {}),
        }
        scripts = manifest.get("scripts", {})
        found: Dict[str, Any] = {}
        for name in deps:
            key = name.lower()
            for hint, value in FRAMEWORK_HINTS.items():
                if hint in key and "framework" not in found:
                    found["framework"] = value
                    evidence.append(f"package.json dependency: {name}")
            for hint, value in TEST_TOOL_HINTS.items():
                if hint in key and "test_tool" not in found:
                    found["test_tool"] = value
                    evidence.append(f"package.json dependency: {name}")
        test_script = str(scripts.get("test", ""))
        if not test_script:
            pass
        elif "vitest" in test_script:
            found["test_tool"] = "vitest"
            evidence.append("package.json scripts.test uses vitest")
        elif "jest" in test_script:
            found["test_tool"] = "jest"
            evidence.append("package.json scripts.test uses jest")
        elif "mocha" in test_script:
            found["test_tool"] = "mocha"
            evidence.append("package.json scripts.test uses mocha")
        found["language"] = "typescript" if (root / "tsconfig.json").exists() else "javascript"
        found["project_type"] = "node"
        found.setdefault("framework", "generic")
        found.setdefault("test_tool", "npm-test")
        return found

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        content = _read_text(pyproject).lower()
        found = {"project_type": "python", "language": "python"}
        for hint, value in FRAMEWORK_HINTS.items():
            if hint in content:
                found["framework"] = value
                evidence.append(f"pyproject.toml mentions {hint}")
                break
        found.setdefault("framework", "generic")
        if "unittest" in content and "pytest" not in content:
            found["test_tool"] = "unittest"
        else:
            found["test_tool"] = "pytest"
        evidence.append("pyproject.toml detected")
        return found
    return None


def classify_project(project_path: str) -> Dict[str, Any]:
    """Pure classification function (easy to unit-test in isolation)."""
    root = Path(project_path).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Project directory not found: {root}")

    evidence: List[str] = []
    result = _detect_from_manifest(root, evidence) or {
        "project_type": "generic", "framework": "generic",
        "test_tool": "generic", "language": "unknown",
    }

    # Marker files refine or confirm the manifest-based result.
    markers_found: List[str] = []
    for marker, defaults in MARKERS.items():
        matches = find_files(root, marker, max_depth=1) if "*" in marker else (
            [root / marker] if (root / marker).exists() else []
        )
        if not matches:
            continue
        markers_found.append(marker)
        evidence.append(f"marker file: {marker}")
        for key, value in zip(
            ("project_type", "framework", "test_tool", "language"), defaults
        ):
            if value and (key not in result or result[key] in {"generic", "unknown"}):
                result[key] = value

    # A source checkout may have tests but no root packaging files.
    if result["project_type"] == "generic":
        for folder in (root, *(p for p in root.iterdir()
                               if not p.name.startswith(".") and p.name not in COPY_EXCLUDES and p.is_dir())):
            test = next((p for p in folder.glob("test_*.py") if p.is_file()), None)
            if test is not None:
                result.update(project_type="python", language="python", test_tool="pytest")
                evidence.append(f"Python test file: {test.relative_to(root).as_posix()}")
                break

    # Test directory hints.
    for dirname, tool in (("tests", None), ("test", None), ("__tests__", "jest")):
        if (root / dirname).is_dir():
            evidence.append(f"test directory: {dirname}")
            if tool and result.get("test_tool") in {"generic", "unknown", None}:
                result["test_tool"] = tool

    confidence = min(0.95, 0.55 + 0.08 * len(evidence))
    result["confidence"] = round(confidence, 2)
    result["evidence"] = evidence
    result["markers"] = markers_found
    if result.get("project_type") in {"generic", "unknown"} and markers_found:
        result["project_type"] = "generic"
    return result


def classification_node(state: WorkflowState) -> Dict[str, Any]:
    """LangGraph node: classify the target project and update the shared state."""
    logger.info("Classifying project: %s", state.get("project_path"))
    started = __import__("time").perf_counter()
    try:
        result = classify_project(state["project_path"])
        duration = __import__("time").perf_counter() - started
        logger.info(
            "Classification done: type=%s tool=%s language=%s (%.3fs)",
            result["project_type"], result["test_tool"], result["language"], duration,
        )
        return {
            "project_type": result["project_type"],
            "framework": result["framework"],
            "test_tool": result["test_tool"],
            "language": result["language"],
            "classification_confidence": result["confidence"],
            "classification_evidence": result["evidence"],
            "current_agent": "classifier",
            "logs": [f"[{utc_now()}] Classified project as {result['project_type']}/{result['test_tool']}"],
        }
    except Exception as exc:  # noqa: BLE001 - node-level boundary
        logger.exception("Classification failed")
        raise
