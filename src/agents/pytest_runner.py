"""Run pytest once and export case results and coverage from the same session."""

import argparse
import json
import os
import sys
from contextlib import chdir
from pathlib import Path

import pytest


def without_cov_options(args: list[str]) -> list[str]:
    """Remove pytest-cov options when pytest has no plugin to parse them."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--cov", nargs="?")
    for name in ("report", "config", "fail-under", "precision", "context"):
        parser.add_argument(f"--cov-{name}")
    for flag in ("--cov-reset", "--cov-append", "--cov-branch", "--no-cov", "--no-cov-on-fail"):
        parser.add_argument(flag, action="store_true")
    return parser.parse_known_args(args)[1]


class CaseReporter:
    def __init__(self, report_path: Path):
        self.report_path = report_path
        self.workspace = Path.cwd()
        self.pending: dict[str, str] = {}
        self.cases: dict[str, str] = {}
        self.collection_problem: str | None = None
        self.deselected = False

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.collection_problem = "collection_failed"
        elif report.skipped and self.collection_problem is None:
            self.collection_problem = "collection_skipped"

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        self.deselected |= bool(items)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        node_id = report.nodeid
        if report.when == "setup":
            self.pending.pop(node_id, None)
            self.cases.pop(node_id, None)
        if report.failed:
            self.pending[node_id] = "failed"
        elif not hasattr(report, "context"):  # A skipped subtest does not skip its parent item.
            if report.skipped and self.pending.get(node_id) != "failed":
                self.pending[node_id] = "skipped"
            elif report.when == "call" and report.passed:
                self.pending.setdefault(node_id, "passed")
        if report.when == "teardown" and node_id in self.pending:
            self.cases[node_id] = self.pending.pop(node_id)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        selected = {item.nodeid for item in session.items}
        # Pytest can discover a root above the temporary copy. Use paths within
        # the copy so node IDs do not change with its randomly generated name.
        cases = {}
        for node_id, outcome in self.cases.items():
            filename, separator, suffix = node_id.partition("::")
            path = (session.config.rootpath / filename).resolve()
            if path.is_relative_to(self.workspace):
                relative_id = path.relative_to(self.workspace).as_posix() + separator + suffix
                cases[relative_id] = outcome

        reason = self.collection_problem
        if reason is None:
            if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
                reason = "no_tests_collected"
            elif exitstatus not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
                reason = "session_interrupted"
            elif self.deselected:
                reason = "tests_deselected"
            elif len(selected) != len(session.items):
                reason = "duplicate_node_ids"
            elif not selected or selected != self.cases.keys():
                reason = "incomplete_execution"
            elif len(cases) != len(self.cases):
                reason = "unresolved_node_ids"

        status = "complete"
        if reason:
            status = "incomplete" if cases else "unavailable"
        data = {
            "format": "pytest-nodeid-v1",
            "status": status,
            "reason": reason,
            "selected_count": len(session.items) if session.items else None,
            "cases": [
                {"node_id": node_id, "outcome": outcome}
                for node_id, outcome in sorted(cases.items())
            ],
        }
        temporary = self.report_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
            temporary.replace(self.report_path)
        except OSError as exc:
            print(f"Could not write pytest case report: {exc}", file=sys.stderr)


class CoverageReporter:
    def __init__(self, report_path: Path):
        self.report_path = report_path
        self.workspace = Path.cwd()
        self.tracer = None

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_load_initial_conftests(self, early_config, parser, args):
        self.configure_coverage(early_config, parser, args)
        return (yield)

    def configure_coverage(self, early_config, parser, args):
        options = early_config.known_args_namespace
        has_cov_options = hasattr(options, "cov_source")
        if not has_cov_options:
            test_args = without_cov_options(args)
            if test_args != args:
                args[:] = test_args
                options.file_or_dir = parser.parse_known_args(args).file_or_dir
                print("Coverage unavailable: pytest-cov is not loaded; skipping its options.", file=sys.stderr)
                return
        elif options.no_cov:
            return

        plugin_traces = has_cov_options and bool(options.cov_source)
        try:
            from coverage import Coverage
        except ImportError:
            print("Coverage unavailable: coverage.py could not be imported.", file=sys.stderr)
            if plugin_traces:
                options.no_cov = True
                args.append("--no-cov")
            return

        if plugin_traces:
            # Isolate even projects that request --cov-append or configure a data file.
            os.environ["COVERAGE_FILE"] = str(self.report_path.with_suffix(".data"))
            return

        try:
            tracer = Coverage(data_file=None)
            # The launcher is outside the tested project but can be traced in editable installs.
            omit = tracer.get_option("run:omit") or []
            tracer.set_option("run:omit", [*omit, str(Path(__file__).resolve())])
            tracer.start()
            self.tracer = tracer
        except Exception as exc:
            print(f"Could not start coverage: {exc}", file=sys.stderr)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtestloop(self, session):
        result = yield
        # pytest-cov has combined its data and applied the project's threshold.
        plugin = session.config.pluginmanager.get_plugin("_cov")
        if (plugin is not None and plugin.cov_controller is not None
                and not (plugin.failed and plugin.options.no_cov_on_fail)):
            self.write_report(plugin.cov_controller.cov)
        return result

    def write_report(self, tracer):
        try:
            temporary_report = self.report_path.with_suffix(".tmp")
            with chdir(self.workspace):
                tracer.json_report(outfile=str(temporary_report), ignore_errors=True)
            temporary_report.replace(self.report_path)
        except Exception as exc:
            print(f"Could not write coverage report: {exc}", file=sys.stderr)

    def finish(self):
        if self.tracer is not None:
            try:
                self.tracer.stop()
            except Exception as exc:
                print(f"Could not stop coverage: {exc}", file=sys.stderr)
            else:
                self.write_report(self.tracer)


def main() -> int:
    report_path = Path(sys.argv[1])
    cases = CaseReporter(Path(sys.argv[2]))
    # Match python -m pytest: resolve project imports from the test workspace.
    sys.path[0] = str(Path.cwd())
    reporter = CoverageReporter(report_path)
    try:
        return pytest.main(sys.argv[3:], plugins=[reporter, cases])
    finally:
        reporter.finish()


if __name__ == "__main__":
    raise SystemExit(main())
