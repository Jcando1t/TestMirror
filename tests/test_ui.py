"""Contract checks for the zero-build local page.

The page must not change the already-accepted API contract: the OpenAPI paths
stay the same, and the page is served only from ``/`` and ``/static``.
"""

from fastapi.testclient import TestClient

from config.settings import Settings
from src.api.fastapi_server import create_app


def test_page_and_assets_are_served(tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "ui.db"))
    with TestClient(create_app(settings)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        for marker in ("eval-form", "project-path", "run-button",
                       "exclude-options", "exclude-dirs", "exclude-hint",
                       "latest-chart", "run-dialog", "history-search", "compare-view",
                       "history-list", "report-panel", "comparison-panel"):
            assert marker in page.text
        for asset in ("/static/styles.css", "/static/app.js"):
            response = client.get(asset)
            assert response.status_code == 200
            assert response.text


def test_page_keeps_api_contract_and_required_wording(tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "ui.db"))
    with TestClient(create_app(settings)) as client:
        paths = set(client.get("/openapi.json").json()["paths"])
        assert paths == {
            "/health", "/workflow", "/evaluations", "/evaluations/{evaluation_id}", "/evaluation-comparisons",
        }
        script = client.get("/static/app.js").text
        for marker in (
            'method: "POST"', '"/evaluations?limit=20"', '"/evaluations"',
            '"/evaluations/"', "正在运行", "用例失败", "执行失败",
            "parseExcludeDirs", "exclude_dirs: parseExcludeDirs()",
            "项目路径无效", "排除目录无效",
            "未取得测试计数", "未采集",
            '"/evaluation-comparisons?"', "暂无法判断用例增删",
            "test_case_results", "用例结果", "aria-pressed",
        ):
            assert marker in script


def test_exclude_dirs_and_history_accessibility_contract(tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "ui.db"))
    with TestClient(create_app(settings)) as client:
        page = client.get("/").text
        assert '<details class="exclude-options">' in page
        assert "<summary>排除目录（可选）</summary>" in page
        assert 'for="exclude-dirs"' in page
        assert 'aria-describedby="exclude-hint"' in page
        assert '<label for="compare-baseline">基线报告</label>' in page
        assert '<label for="compare-current">新报告</label>' in page
        assert '<h2 id="comparison-title" tabindex="-1">评测对比</h2>' in page

        styles = client.get("/static/styles.css").text
        assert "button:not(.history-item):hover:not(:disabled)" in styles
        assert ".history-item:hover:not(:disabled)" in styles
        assert ".history-item:focus-visible" in styles
        assert "outline: 3px solid var(--accent)" in styles
