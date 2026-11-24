/* TestMirror local UI: evaluation, history, and saved report comparison. */
(function () {
  "use strict";

  const state = { running: false, currentId: null, reportRequest: 0 };
  const byId = (id) => document.getElementById(id);
  const baselineSelect = byId("compare-baseline");
  const currentSelect = byId("compare-current");
  const runDialog = byId("run-dialog");
  const search = byId("history-search");
  const statusFilter = byId("history-status");
  let historyRows = [];
  let view = "overview";
  let historyReady = false;
  const views = {
    overview: ["评测总览", "查看最近的测试结果和运行记录。"],
    runs: ["运行记录", "按项目、报告 ID 或运行状态查找评估。"],
    compare: ["回归对比", "选择基线与新报告，查看逐用例变化。"],
  };

  function showView(next, focus = true) {
    state.reportRequest++;
    view = next;
    const heading = byId("start-title");
    heading.textContent = views[next]?.[0] || "评估报告";
    byId("view-description").textContent = views[next]?.[1] || "";
    show("view-description", next !== "report");
    show("overview-panel", next === "overview");
    show("history-panel", next === "overview" || next === "runs");
    show("compare-view", next === "compare");
    show("report-panel", next === "report");
    document.querySelectorAll(".workspace-nav a").forEach((link) => {
      if (link.hash === "#" + next) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    if (historyReady && (next === "overview" || next === "runs")) renderHistory(historyRows);
    if (focus) {
      window.scrollTo(0, 0);
      heading.focus({ preventScroll: true });
    }
  }

  function route(focus) {
    const hash = location.hash.slice(1);
    showView(Object.hasOwn(views, hash) ? hash : "overview", focus);
  }

  window.addEventListener("hashchange", () => route(true));
  document.querySelectorAll('.workspace-nav a, .brand, #all-runs').forEach((link) => {
    link.addEventListener("click", () => { if (location.hash === link.hash) route(true); });
  });
  byId("open-run").addEventListener("click", () => runDialog.showModal());
  byId("close-run").addEventListener("click", () => runDialog.close());
  runDialog.addEventListener("cancel", (event) => { if (state.running) event.preventDefault(); });
  search.addEventListener("input", () => renderHistory(historyRows));
  statusFilter.addEventListener("change", () => renderHistory(historyRows));
  const runTimeFormat = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function show(id, visible) { byId(id).hidden = !visible; }

  function validationMessage(detail) {
    if (!Array.isArray(detail)) return null;
    const messages = detail.map((item) => {
      const location = item && Array.isArray(item.loc) ? item.loc : [];
      const field = location[location.length - 1];
      if (field === "project_path") {
        return "项目路径无效，请输入已存在的本地目录。";
      }
      if (field === "exclude_dirs") {
        return "排除目录无效：每行只能是项目内的相对目录，不能包含绝对路径或 ..。";
      }
      return item && item.msg ? String(item.msg) : "请求字段无效，请检查输入。";
    });
    return [...new Set(messages)].join(" ");
  }

  async function apiJson(path, options) {
    const response = await fetch(path, options);
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const body = await response.json();
        if (body && body.detail) {
          if (typeof body.detail === "string") {
            detail = body.detail;
          } else if (Array.isArray(body.detail)) {
            detail = validationMessage(body.detail) || "请求字段无效，请检查输入。";
          } else if (body.detail.msg) {
            detail = String(body.detail.msg);
          } else {
            detail = "请求未完成，请检查输入后重试。";
          }
        }
      } catch (_) { /* keep the HTTP status as the fallback */ }
      throw new Error(detail);
    }
    return response.json();
  }

  function setRunning(running) {
    state.running = running;
    byId("run-button").disabled = running;
    byId("project-path").disabled = running;
    byId("exclude-dirs").disabled = running;
    byId("close-run").disabled = running;
    byId("run-button").classList.toggle("is-running", running);
    byId("run-button").textContent = running ? "正在运行…" : "开始评估";
    const status = byId("run-status");
    status.hidden = !running;
    status.textContent = running
      ? "正在运行：测试评估已提交，请等待结果，不要重复提交。" : "";
  }

  function formError(message) {
    const box = byId("form-error");
    box.hidden = !message;
    box.textContent = message || "";
  }

  // ---- 1. 输入本地项目路径并开始评估 ----------------------------------
  function parseExcludeDirs() {
    return byId("exclude-dirs").value
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
  }

  byId("eval-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    if (state.running) return; // 再次点击或回车不会重复提交
    const projectPath = byId("project-path").value.trim();
    if (!projectPath) {
      formError("请输入本地项目路径。");
      byId("project-path").focus();
      return;
    }
    formError("");
    setRunning(true);
    try {
      const final = await apiJson("/evaluations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_path: projectPath,
          exclude_dirs: parseExcludeDirs(),
        }),
      });
      state.currentId = final.evaluation_id;
      // 从已验收的报告接口读取，和“打开旧报告”使用同一渲染路径。
      const report = await apiJson("/evaluations/" + encodeURIComponent(final.evaluation_id));
      runDialog.close();
      renderReport(report, "本次报告");
      await loadHistory();
    } catch (error) {
      formError("评估未完成：" + error.message);
    } finally {
      setRunning(false);
    }
  });

  // ---- 2. 从历史列表打开旧报告 ----------------------------------------
  async function loadHistory() {
    try {
      const rows = await apiJson("/evaluations?limit=20");
      historyRows = rows || [];
      historyReady = true;
      renderLatest(historyRows);
      if (view === "runs" || view === "overview") renderHistory(historyRows);
      populateComparisonOptions(historyRows);
      byId("history-error").hidden = true;
    } catch (error) {
      byId("history-error").hidden = false;
      byId("history-error").textContent = "历史记录加载失败：" + error.message;
      byId("latest-message").textContent = "暂时无法读取评估记录。";
      byId("compare-empty").textContent = "暂时无法读取评估记录。";
    }
  }

  function historyLabel(status) {
    if (status === "passed") return "通过";
    if (status === "test_failed") return "用例失败";
    if (status === "execution_failed") return "执行失败";
    return "结果未知";
  }

  function historyBadge(row) {
    const tones = { passed: "pass", test_failed: "fail", execution_failed: "exec" };
    const blocked = row.status === "passed" && row.gate_passed === false;
    return el("span", "badge badge-" + (blocked ? "fail" : tones[row.status] || "unknown"),
      blocked ? "门禁拦截" : historyLabel(row.status));
  }

  function formatRunTime(timestamp) {
    if (!timestamp) return "时间未采集";
    const date = new Date(timestamp);
    return Number.isNaN(date.valueOf()) ? timestamp : runTimeFormat.format(date);
  }

  async function openSavedReport(id, errorId) {
    if (state.running) return;
    const request = ++state.reportRequest;
    show(errorId, false);
    try {
      const report = await apiJson("/evaluations/" + encodeURIComponent(id));
      if (request === state.reportRequest && !state.running) renderReport(report, "历史报告");
    } catch (error) {
      if (request !== state.reportRequest) return;
      byId(errorId).textContent = "打开报告失败：" + error.message;
      show(errorId, true);
    }
  }

  const openLatest = byId("latest-open");
  openLatest.addEventListener("click", () => openSavedReport(openLatest.dataset.id, "latest-error"));

  function renderLatest(rows) {
    const row = rows[0];
    show("latest-content", Boolean(row));
    show("latest-message", !row);
    if (!row) {
      byId("latest-message").textContent = "运行一次评估后，这里会显示测试结果。";
      return;
    }
    byId("latest-name").textContent = row.project_name || "未命名项目";
    byId("latest-time").textContent = formatRunTime(row.started_at);
    byId("latest-status").replaceChildren(historyBadge(row));
    openLatest.dataset.id = row.evaluation_id;
    const rate = row.pass_rate;
    const hasRate = Number.isFinite(rate) && rate >= 0 && rate <= 1;
    byId("latest-rate").textContent = formatRate(hasRate ? rate : null);
    const chart = byId("latest-chart");
    chart.replaceChildren(...rows.slice(0, 12).reverse().map((run) => {
      const bar = el("span", "chart-bar");
      const amount = run.pass_rate;
      const valid = Number.isFinite(amount) && amount >= 0 && amount <= 1;
      bar.style.setProperty("--level", valid ? amount * 100 + "%" : "0%");
      bar.dataset.state = valid ? run.status : "unavailable";
      const label = [run.project_name || "未命名项目", formatRunTime(run.started_at), formatRate(valid ? amount : null)].join(" / ");
      bar.setAttribute("role", "img");
      bar.setAttribute("aria-label", label);
      bar.title = label;
      return bar;
    }));
  }

  function renderHistory(rows) {
    const list = byId("history-list");
    list.replaceChildren();
    const overview = view === "overview";
    const query = search.value.trim().toLowerCase();
    const status = statusFilter.value;
    const visible = overview ? rows.slice(0, 5) : rows.filter((row) => {
      const result = row.status === "passed" && row.gate_passed === false ? "blocked" : row.status;
      return (!status || result === status)
        && [row.project_name, row.evaluation_id].some((value) => value?.toLowerCase().includes(query));
    });
    byId("history-title").textContent = overview ? "最近运行" : "历史评估";
    byId("history-count").textContent = overview ? "最近 " + visible.length + " 条" : visible.length + " / " + rows.length + " 条近期记录";
    show("history-count", rows.length > 0);
    show("history-tools", !overview && rows.length > 0);
    show("all-runs", overview && rows.length > 0);
    show("history-columns", visible.length > 0);
    show("history-empty", visible.length === 0);
    byId("history-empty").textContent = rows.length ? "没有符合条件的记录。" : "暂无评估记录。运行一次项目评估后，报告会出现在这里。";
    visible.forEach((row) => {
      const item = el("button", "history-item");
      item.type = "button";
      item.dataset.id = row.evaluation_id;
      const project = el("span", "history-project");
      project.append(el("span", "history-name", row.project_name || "未命名项目"),
        el("span", "history-id mono", row.evaluation_id));
      const runTime = el("span", "history-meta", formatRunTime(row.started_at));
      if (row.started_at) runTime.title = row.started_at;
      const rate = el("span", "history-rate");
      rate.append(el("span", "sr-only", "通过率 "), document.createTextNode(formatRate(row.pass_rate)));
      if (Number.isFinite(row.pass_rate) && row.pass_rate >= 0 && row.pass_rate <= 1) {
        const bar = el("meter");
        bar.max = 1;
        bar.value = row.pass_rate;
        bar.setAttribute("aria-hidden", "true");
        rate.appendChild(bar);
      }
      item.append(project, runTime, rate, historyBadge(row));
      item.addEventListener("click", () => openSavedReport(row.evaluation_id, "history-error"));
      const entry = el("li");
      entry.appendChild(item);
      list.appendChild(entry);
    });
  }

  function populateComparisonOptions(rows) {
    const ids = new Set(rows.map((row) => row.evaluation_id));
    [[baselineSelect, rows[1]], [currentSelect, rows[0]]].forEach(([select, fallback]) => {
      const previous = select.value;
      select.replaceChildren(...rows.map((row) => {
        const text = [row.project_name || "未命名项目", row.evaluation_id, formatRunTime(row.started_at)].join(" / ");
        const option = el("option", "", text);
        option.value = row.evaluation_id;
        return option;
      }));
      select.value = ids.has(previous) ? previous : fallback?.evaluation_id ?? "";
    });
    show("compare-form", rows.length >= 2);
    show("compare-empty", rows.length < 2);
    byId("compare-empty").textContent = "保存至少两份评估报告后即可对比。";
  }

  function comparisonError(message = "") {
    byId("compare-error").textContent = message;
    show("compare-error", Boolean(message));
  }

  [baselineSelect, currentSelect].forEach((select) => select.addEventListener("change", () => {
    comparisonError();
    show("comparison-panel", false);
  }));

  byId("compare-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = byId("compare-button");
    if (button.disabled) return;
    const baseline = baselineSelect.value;
    const current = currentSelect.value;
    if (baseline === current) {
      comparisonError("请选择两份不同的报告。");
      currentSelect.focus();
      return;
    }
    comparisonError();
    show("comparison-panel", false);
    byId("compare-status").textContent = "正在读取评测对比…";
    show("compare-status", true);
    button.disabled = baselineSelect.disabled = currentSelect.disabled = true;
    try {
      const query = new URLSearchParams({ baseline_id: baseline, current_id: current });
      const result = await apiJson("/evaluation-comparisons?" + query);
      if (baselineSelect.value === baseline && currentSelect.value === current) {
        renderComparison(result);
      }
    } catch (error) {
      comparisonError("无法读取评测对比：" + error.message);
    } finally {
      show("compare-status", false);
      button.disabled = baselineSelect.disabled = currentSelect.disabled = false;
    }
  });

  const comparisonGroups = [
    ["new_failures", "新失败"], ["fixed", "已修复"], ["added", "新增用例"],
    ["removed", "移除用例"], ["skip_changes", "跳过变化"],
  ];
  const comparisonWarnings = {
    project_path_changed: "项目路径不同",
    runtime_changed: "运行环境不同",
    exclude_dirs_changed: "排除目录不同",
    quality_gate_policy_changed: "门禁规则不同",
  };
  const caseReasons = {
    case_data_missing: "未保存逐用例结果",
    case_data_invalid: "逐用例结果格式无效",
    collection_failed: "pytest 收集失败",
    collection_skipped: "pytest 跳过了部分收集",
    no_tests_collected: "未收集到用例",
    session_interrupted: "测试会话中断",
    tests_deselected: "部分用例被筛选",
    duplicate_node_ids: "存在重复的用例 ID",
    incomplete_execution: "部分用例未跑完",
    unresolved_node_ids: "部分用例路径无法解析",
    timeout: "测试超时",
    runner_unavailable: "测试运行器不可用",
    case_report_missing_or_invalid: "用例采集文件缺失或无效",
    multiple_test_commands: "评测包含多个测试命令",
  };
  const caseLabels = { passed: "通过", failed: "失败", skipped: "跳过" };

  function comparisonTable(style, headings, rows) {
    const table = el("table", "comparison-table " + style);
    const header = table.createTHead().insertRow();
    headings.forEach((heading) => {
      const cell = el("th", "", heading);
      cell.scope = "col";
      header.appendChild(cell);
    });
    const body = table.createTBody();
    rows.forEach(([label, before, after]) => {
      const row = body.insertRow();
      const heading = el("th", "", label);
      heading.scope = "row";
      row.appendChild(heading);
      [before, after].forEach((value) => {
        const cell = row.insertCell();
        if (value instanceof Node) cell.appendChild(value);
        else cell.textContent = value ?? "未采集";
      });
    });
    return table;
  }

  function comparisonContext(baseline, current) {
    const fields = [
      ["运行时间", (run) => formatRunTime(run.started_at)],
      ["项目路径", (run) => run.project_path || null],
      ["测试结果", (run) => {
        const counts = countsOf(run);
        return counts ? `${counts.passed} 通过 / ${counts.failed} 失败 / ${counts.skipped} 跳过` : historyLabel(run.outcome);
      }],
      ["通过率", (run) => formatRate(run.pass_rate)],
      ["覆盖率", (run) => formatCoverage(run.coverage_percent)],
      ["逐用例数据", (run) => {
        if (run.case_status === "complete") return `完整（${run.recorded_count} 条）`;
        const stateLabel = run.case_status === "incomplete" ? `不完整（${run.recorded_count} 条）` : "不可用";
        return stateLabel + " · " + (caseReasons[run.case_reason] || "采集状态未知");
      }],
      ["代码指纹", (run) => run.snapshot?.sha256 ? el("code", "", run.snapshot.sha256) : null],
      ["Git 版本", (run) => {
        if (!run.git) return null;
        const status = run.git.dirty == null ? "" : run.git.dirty ? "（有未提交改动）" : "（干净）";
        return run.git.commit + status;
      }],
      ["运行环境", (run) => {
        const runtime = run.runtime;
        if (!runtime) return null;
        const python = runtime.python;
        const platform = [runtime.os, runtime.os_release, runtime.architecture].filter(Boolean).join(" ");
        const packages = Object.entries(python?.installed_packages || {}).map(([name, version]) => name + " " + version);
        return [platform, python?.version && "Python " + python.version, ...packages].filter(Boolean).join(" · ");
      }],
      ["Python 路径", (run) => run.runtime?.python?.executable],
      ["排除目录", (run) => run.exclude_dirs?.join("、") || (run.exclude_dirs ? "无" : null)],
      ["门禁规则", (run) => {
        const gate = run.quality_gate;
        if (!gate) return null;
        const threshold = gate.min_coverage == null ? "无覆盖率阈值" : "最低覆盖率 " + gate.min_coverage + "%";
        return [gate.policy_version != null && "v" + gate.policy_version, threshold,
          gate.passed ? "放行" : "拦截"].filter(Boolean).join(" · ");
      }],
    ];
    return comparisonTable("comparison-context", ["项目", "基线报告", "新报告"],
      fields.map(([label, format]) => [label, format(baseline), format(current)]));
  }

  function renderComparison(result) {
    const root = byId("comparison-content");
    root.replaceChildren();
    const runs = el("div", "comparison-runs");
    [["基线报告", result.baseline], ["新报告", result.current]].forEach(([label, run]) => {
      const runCard = el("div", "comparison-run");
      runCard.append(el("span", "metric-label", label),
        el("strong", "", run.project_name || "未命名项目"),
        el("span", "mono muted", run.evaluation_id));
      runs.appendChild(runCard);
    });
    root.appendChild(runs);
    const limits = {
      partial: "至少一份报告采集不完整。仅比较两份报告都记录的用例，暂无法判断用例增删。",
      unavailable: "至少一份报告缺少可用的逐用例结果。下方仍可查看评测汇总和采集原因。",
    };
    if (limits[result.status]) root.appendChild(el("p", "note", limits[result.status]));
    if (result.warnings.length) {
      const warnings = result.warnings.map((warning) => comparisonWarnings[warning] || warning);
      root.appendChild(el("p", "note", warnings.join("，") + "。"));
    }

    if (result.status !== "unavailable") {
      root.appendChild(el("p", "hint", "匹配到 " + result.matched_count + " 条用例"));
      const grid = el("div", "metrics comparison-counts");
      comparisonGroups.forEach(([key, label]) => {
        const unknown = result.status !== "complete" && (key === "added" || key === "removed");
        const tile = metric(label, unknown ? "无法判断" : result.counts[key], unknown);
        if (result.counts[key] > 0) tile.dataset.change = key;
        grid.appendChild(tile);
      });
      root.appendChild(grid);
      comparisonGroups.forEach(([key, label]) => {
        const items = result.changes[key];
        if (!items.length) return;
        const group = el("details", "comparison-group");
        group.dataset.change = key;
        group.open = true;
        group.appendChild(el("summary", "", label + "（" + items.length + "）"));
        group.appendChild(comparisonTable("comparison-cases", ["用例 ID", "基线", "新报告"],
          items.map(({ node_id, before, after }) => [
            node_id,
            el("span", "compare-" + before, caseLabels[before] || "—"),
            el("span", "compare-" + after, caseLabels[after] || "—"),
          ])));
        root.appendChild(group);
      });
    }

    const context = el("details", "section comparison-evidence");
    context.appendChild(el("summary", "", "评测依据"));
    context.appendChild(comparisonContext(result.baseline, result.current));
    root.appendChild(context);
    show("comparison-panel", true);
    if (view === "compare") byId("comparison-title").focus();
  }

  // ---- 3. 报告渲染 -----------------------------------------------------
  function extractReport(payload) {
    const reporting = (payload.results || [])
      .filter((row) => row.stage === "reporting")
      .slice(-1)[0];
    const output = (reporting && reporting.output_data) || {};
    return {
      metrics: output.metrics || payload.metrics || {},
      summary: output.summary || payload.summary || "",
      score: output.overall_score !== undefined
        ? output.overall_score : payload.overall_score,
      recommendations: output.recommendations || payload.recommendations || [],
      detailed: output.detailed_report || payload.detailed_report || {},
      errors: payload.errors || [],
      evaluation: payload.evaluation || {},
    };
  }

  function countsOf(metrics) {
    const raw = metrics.test_counts;
    if (!raw || typeof raw !== "object") return null;
    const values = ["passed", "failed", "skipped"].map((key) => Number(raw[key]));
    if (values.some((value) => !Number.isFinite(value))) return null;
    // 全零同“未取得测试计数”，不能当作真实的 0 个用例展示。
    if (values.every((value) => value === 0)) return null;
    return { passed: values[0], failed: values[1], skipped: values[2] };
  }

  function formatRate(value) {
    return typeof value === "number" && Number.isFinite(value)
      ? (value * 100).toFixed(1) + "%" : "未采集";
  }

  function formatCoverage(value) {
    return typeof value === "number" && Number.isFinite(value)
      ? value.toFixed(1) + "%" : "未采集";
  }

  function metric(label, value, missing) {
    const box = el("div", "metric");
    box.appendChild(el("span", "metric-label", label));
    box.appendChild(el("span", "metric-value" + (missing ? " missing" : ""), value));
    return box;
  }

  function outcomeOf(metrics, evaluation) {
    if (metrics.outcome) return metrics.outcome;
    if (evaluation && evaluation.status) {
      if (evaluation.status === "passed") return "passed";
      if (evaluation.status === "test_failed") return "test_failed";
      if (evaluation.status === "execution_failed") return "execution_failed";
    }
    return "unknown";
  }

  function appendFindings(root, heading, items = [], summary = "") {
    if (!items.length && !summary) return;
    const section = el("div", "section");
    section.appendChild(el("h3", "", heading));
    if (summary) section.appendChild(el("p", "summary", summary));
    if (items.length) {
      const list = el("ul", "details");
      items.forEach((item) => {
        const title = item.title || item.message || "未采集";
        list.appendChild(el("li", "", title + (item.location ? " · " + item.location : "")));
      });
      section.appendChild(list);
    }
    root.appendChild(section);
  }

  function appendTestCases(root, caseReport, evidence) {
    if (!caseReport || !Array.isArray(caseReport.cases) || caseReport.cases.length === 0) return;

    const outcomes = [
      ["all", "全部", caseReport.cases.length],
      ["failed", "失败", caseReport.cases.filter((test) => test.outcome === "failed").length],
      ["passed", "通过", caseReport.cases.filter((test) => test.outcome === "passed").length],
      ["skipped", "跳过", caseReport.cases.filter((test) => test.outcome === "skipped").length],
    ].filter(([key, , count]) => key === "all" || count > 0);
    const section = el("section", "case-section");
    const heading = el("div", "case-heading");
    heading.append(el("h3", "", "用例结果"),
      el("span", "case-total", caseReport.cases.length + " 条"));
    section.appendChild(heading);

    const filters = el("div", "case-filters");
    const list = el("ul", "case-list");
    const entries = caseReport.cases.map((test) => {
      const item = el("li", "case-entry");
      const label = caseLabels[test.outcome] || "未知";
      const findings = test.outcome === "failed" ? (evidence || []).filter((item) =>
        item?.title?.startsWith(test.node_id + " - ")) : [];
      const summary = el(findings.length ? "summary" : "div", "test-case-summary");
      summary.append(el("span", "case-state case-state-" + test.outcome, label),
        el("code", "case-id", test.node_id));
      if (findings.length) {
        const detail = el("details", "test-case");
        detail.open = true;
        const notes = el("div", "case-evidence");
        findings.forEach((finding) => notes.appendChild(el("p", "", finding.title.slice(test.node_id.length + 3))));
        detail.append(summary, notes);
        item.appendChild(detail);
      } else {
        item.appendChild(summary);
      }
      item.dataset.outcome = test.outcome;
      list.appendChild(item);
      return item;
    });

    outcomes.forEach(([key, label, count], index) => {
      const filter = el("button", "case-filter", label + " " + count);
      filter.type = "button";
      filter.setAttribute("aria-pressed", String(index === 0));
      filter.addEventListener("click", () => {
        filters.querySelectorAll("button").forEach((button) => button.setAttribute("aria-pressed", "false"));
        filter.setAttribute("aria-pressed", "true");
        entries.forEach((item) => { item.hidden = key !== "all" && item.dataset.outcome !== key; });
      });
      filters.appendChild(filter);
    });
    section.append(filters, list);
    if (caseReport.status !== "complete") {
      section.appendChild(el("p", "note", "逐用例数据不完整。该列表只显示本次实际采集到的用例。"));
    }
    root.appendChild(section);
  }

  function renderReport(payload, title) {
    const report = extractReport(payload);
    const metrics = report.metrics;
    const counts = countsOf(metrics);
    const evaluationId = report.evaluation.id || state.currentId || "未采集";

    byId("report-title").textContent = title;
    byId("report-id").textContent = evaluationId;
    byId("report-meta").textContent = [report.evaluation.project_name,
      report.evaluation.started_at && formatRunTime(report.evaluation.started_at)].filter(Boolean).join(" / ");

    const root = byId("report-content");
    root.replaceChildren();

    const statusLine = el("div", "status-line");
    const outcome = outcomeOf(metrics, report.evaluation);
    const outcomes = {
      passed: ["评估通过", "pass"],
      test_failed: ["用例失败", "fail"],
      execution_failed: ["执行失败", "exec"],
    };
    const badge = outcomes[outcome] || ["结果未知", "unknown"];
    statusLine.appendChild(el("span", "badge badge-" + badge[1], badge[0]));
    const gate = metrics.quality_gate;
    if (gate && outcome === "passed" && gate.min_coverage != null) {
      const label = gate.passed ? "覆盖率门禁通过" : gate.reason === "coverage_unavailable"
        ? "门禁拦截 · 覆盖率未采集" : "门禁拦截 · 覆盖率低于 " + gate.min_coverage + "%";
      statusLine.appendChild(el("span", "badge badge-" + (gate.passed ? "pass" : "fail"), label));
    }
    if (!counts) {
      statusLine.appendChild(el("span", "badge badge-unknown", "未取得测试计数"));
    }
    root.appendChild(statusLine);

    root.appendChild(el("p", "summary", report.summary || "报告摘要未采集。"));

    const grid = el("div", "metrics report-metrics");
    grid.appendChild(metric("通过", counts ? counts.passed : "未采集", !counts));
    grid.appendChild(metric("失败", counts ? counts.failed : "未采集", !counts));
    grid.appendChild(metric("跳过", counts ? counts.skipped : "未采集", !counts));
    const rate = counts && typeof metrics.pass_rate === "number" ? metrics.pass_rate : null;
    grid.appendChild(metric("通过率", formatRate(rate), rate === null));
    grid.appendChild(metric("覆盖率",
      formatCoverage(metrics.coverage_percent),
      typeof metrics.coverage_percent !== "number"));
    grid.appendChild(metric("综合参考分",
      typeof report.score === "number" ? report.score + " / 100" : "未采集",
      typeof report.score !== "number"));
    root.appendChild(grid);

    const notes = [];
    if (!counts) {
      notes.push("测试计数未采集：通过／失败／跳过与通过率显示为“未采集”，并非 0%。");
    }
    if (typeof metrics.coverage_percent !== "number") {
      notes.push("覆盖率未采集：显示为“未采集”，并非 0%。");
    }
    notes.forEach((text) => root.appendChild(el("p", "note", text)));

    const analysis = report.detailed.analysis || payload.analysis || {};
    appendTestCases(root, report.detailed.execution?.test_case_results, analysis.test_evidence);
    if (analysis.test_evidence) {
      appendFindings(root, "测试输出证据", analysis.test_evidence);
      const model = analysis.model_diagnosis;
      if (model) {
        appendFindings(root, "DeepSeek 推断（待核实）", model.issues, model.root_cause);
      }
      const workflowIssues = (analysis.issues || []).filter((item) => item.agent);
      appendFindings(root, "执行诊断", workflowIssues);
    } else {
      appendFindings(root, "失败线索", analysis.issues);
    }

    const adviceSection = el("div", "section");
    adviceSection.appendChild(el("h3", "", "建议"));
    const recommendations = report.recommendations;
    if (recommendations && recommendations.length) {
      const list = el("ul", "advice");
      recommendations.forEach((item) => {
        const li = el("li", "priority-" + (item.priority || "medium"));
        const source = item.source === "deepseek" ? "DeepSeek 建议 · " : "";
        li.appendChild(el("div", "advice-title", source + (item.title || "未命名建议")));
        li.appendChild(el("div", "advice-msg", item.message || ""));
        list.appendChild(li);
      });
      adviceSection.appendChild(list);
    } else {
      adviceSection.appendChild(el("p", "empty", "未采集"));
    }
    root.appendChild(adviceSection);

    const detailSection = el("div", "section");
    detailSection.appendChild(el("h3", "", "执行信息"));
    const details = el("ul", "details");
    const project = report.detailed.project || {};
    const addDetail = (label, value) => details.appendChild(
      el("li", "", label + "：" + (value === undefined || value === null || value === ""
        ? "未采集" : String(value))));
    addDetail("项目路径", project.path);
    addDetail("测试工具", project.test_tool);
    addDetail("退出码", metrics.exit_code);
    addDetail("执行耗时", typeof metrics.execution_time === "number"
      ? metrics.execution_time + " 秒" : null);
    addDetail("覆盖率来源", metrics.coverage_source);
    const execution = report.detailed.execution || {};
    const provenance = execution.provenance;
    if (provenance) {
      const snapshot = provenance.snapshot;
      addDetail("测试副本 SHA-256", snapshot.sha256);
      addDetail("副本文件", snapshot.files + " 个，" + snapshot.bytes + " 字节");
      if (provenance.git) {
        addDetail("源目录 Git 提交", provenance.git.commit);
        addDetail("Git 工作区", provenance.git.dirty === null ? null
          : provenance.git.dirty ? "有未提交改动" : "干净");
      }
      const runtime = provenance.runtime;
      addDetail("运行平台", [runtime.os, runtime.os_release, runtime.architecture].filter(Boolean).join(" "));
      if (runtime.python) {
        const packages = Object.entries(runtime.python.installed_packages).map(([key, value]) => key + " " + value);
        addDetail("Python 环境", ["Python " + runtime.python.version, ...packages].join(" · "));
      }
      (execution.commands || []).forEach((entry) => {
        const timeout = entry.timeout_seconds == null ? "" : "（超时 " + entry.timeout_seconds + " 秒）";
        addDetail("执行参数", JSON.stringify(entry.command) + timeout);
      });
      if (gate && gate.policy_version != null) {
        const threshold = gate.min_coverage == null ? "未设置覆盖率阈值" : "最低覆盖率 " + gate.min_coverage + "%";
        addDetail("门禁规则", "v" + gate.policy_version + " · " + threshold);
      }
    }
    detailSection.appendChild(details);
    root.appendChild(detailSection);

    if (report.errors && report.errors.length) {
      const errorSection = el("div", "section");
      errorSection.appendChild(el("h3", "", "工作流错误"));
      const list = el("ul", "details");
      report.errors.forEach((item) => list.appendChild(el("li", "",
        (item.agent || "unknown") + "：" + (item.message || "未采集"))));
      errorSection.appendChild(list);
      root.appendChild(errorSection);
    }

    if (outcome !== "passed" && (execution.stdout || execution.stderr)) {
      const output = el("details", "section report-output");
      output.appendChild(el("summary", "", "查看测试执行日志"));
      if (execution.stdout) output.appendChild(el("pre", "", execution.stdout));
      if (execution.stderr) output.appendChild(el("pre", "", execution.stderr));
      root.appendChild(output);
    }

    showView("report", false);
    window.scrollTo(0, 0);
    byId("report-title").focus({ preventScroll: true });
  }

  route(false);
  loadHistory();
})();
