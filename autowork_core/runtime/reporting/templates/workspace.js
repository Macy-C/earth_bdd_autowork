(() => {
  const originalRender = render;
  const originalSetView = setView;
  const esc = escapeHtml;
  const icon = name => `<i data-lucide="${name}" aria-hidden="true"></i>`;
  const allRows = flattenRows();
  const units = allRows.filter(row => row.rowType !== "outline");
  const attention = units.filter(row => ["failed", "unknown"].includes(normalizeStatus(row.status)));
  let savedTheme = "light";
  try { savedTheme = localStorage.getItem("autowork_spark_theme") || "light"; } catch (_) {}
  const workspace = {selected: null, query: "", status: "all", feature: "all", expanded: new Set(), memories: new Map(), material: null, logLevel: "all", scale: 1, theme: savedTheme === "dark" ? "dark" : "light"};
  const current = () => allRows.find(row => row.id === workspace.selected);
  const picker = document.createElement("dialog");
  const evidenceDialog = document.createElement("dialog");
  const toastStack = document.createElement("div");
  toastStack.className = "rp-toast-stack";
  toastStack.id = "rpToastStack";
  picker.className = "rp-picker";
  picker.setAttribute("aria-label", "选择执行场景");
  evidenceDialog.className = "rp-evidence-dialog";
  evidenceDialog.setAttribute("aria-label", "证据查看器");
  document.body.append(picker, evidenceDialog, toastStack);
  document.documentElement.setAttribute("data-theme", workspace.theme);
  document.body.setAttribute("data-theme", workspace.theme);
  let evidenceTrigger = null;
  const runDisclosures = new Set();

  function showReportToast(text, kind = "pass") {
    const pill = document.createElement("div");
    pill.className = `rp-toast-pill ${kind}`;
    pill.textContent = text;
    toastStack.appendChild(pill);
    setTimeout(() => {
      pill.classList.add("out");
      setTimeout(() => pill.remove(), 180);
    }, 1650);
  }

  function triggerReportSpotlight(el) {
    if (!el) return;
    el.classList.remove("rp-spotlight-flash");
    void el.offsetWidth;
    el.classList.add("rp-spotlight-flash");
  }

  function decorate() {
    window.lucide?.createIcons({attrs: {"stroke-width": 1.7}});
  }

  function disclosure(id, title, meta, body) {
    return `<details class="rd-disclosure" data-rd-disclosure="${escapeAttr(id)}"><summary><span class="rd-chevron">${icon("chevron-right")}</span><span class="rd-disclosure-title">${esc(title)}</span>${meta ? `<span class="rd-disclosure-meta">${esc(meta)}</span>` : ""}<span class="rd-toggle-state"><span class="rd-when-closed">展开</span><span class="rd-when-open">收起</span></span></summary><div class="rd-disclosure-body">${body}</div></details>`;
  }

  function reading(value, id, label, limit = 260) {
    const text = String(value || "未提供");
    const long = text.length > limit;
    return `<div class="rd-reading"><p>${esc(long ? text.slice(0, limit) + "..." : text)}</p></div>${long ? disclosure(id, label, `${text.length} 字符`, `<div class="rd-reading"><p>${esc(text)}</p></div>`) : ""}`;
  }

  function sectionHeading(title, symbol, suffix = "") {
    return `<div class="rd-section-heading"><div class="rd-section-title">${icon(symbol)}<h3>${esc(title)}</h3>${suffix}</div></div>`;
  }

  function factLabel(fact) {
    let detail = {};
    try { detail = JSON.parse(fact.text); } catch (_) {}
    const position = String(fact.scopeId || "").match(/\/step-(\d+)/);
    const names = {step: "步骤事实", traceback: "异常堆栈", action: "动作记录", log: "关联日志", image: "图片记录", execution: "执行信息"};
    const operation = fact.kind === "action" ? detail.operation : fact.kind === "step" ? detail.name : "";
    return [names[fact.kind] || "引用记录", position ? `Step ${position[1]}` : "", conciseText(operation, 28)].filter(Boolean).join(" · ");
  }

  function references(ids, diagnosis) {
    const facts = [...new Set(ids || [])].map(id => (diagnosis.evidence || []).find(fact => fact.id === id)).filter(Boolean);
    if (!facts.length) return "";
    return `<div class="rd-evidence-caption">判断依据</div><div class="rd-references">${facts.map(fact => `<button class="rd-reference" data-r-material="${escapeAttr(fact.id)}" aria-label="查看 ${escapeAttr(factLabel(fact))}">${icon(materialIcon(fact.kind === "traceback" ? "error" : "fact"))}<span>${esc(factLabel(fact))}</span><b>查看${icon("panel-right-open")}</b></button>`).join("")}</div>`;
  }

  function analysisSection(row) {
    const diagnosis = row.diagnosis || (reportData.diagnosis?.enabled === false || reportData.diagnosis?.status === "disabled" ? {status: "disabled"} : null);
    const heading = sectionHeading("原因分析", "sparkles", `<span class="rd-analysis-state">${icon("circle-dot")}${esc(diagnosis?.status === "completed" ? "AI 原因候选 · 已生成" : diagnosis?.status === "disabled" ? "未启用" : diagnosisStatus(diagnosis?.status || "not_analyzed"))}</span>`);
    if (!diagnosis || diagnosis.status !== "completed") return `<section class="rd-section" id="rp-analysis">${heading}<div class="rd-empty-note"><strong>${diagnosis?.status === "disabled" ? "本场景未启用 AI 诊断" : "本场景没有已完成的 AI 诊断"}</strong>${esc(diagnosis?.reason || "执行结果、步骤与证据可独立查看。")}</div></section>`;
    const primary = (diagnosis.causes || []).find(cause => !/^(?:[（(]排除项[)）]|需排除的替代假设)/.test(cause.reason || ""));
    const immediate = (diagnosis.recommendations || [])[0];
    const supplemental = (diagnosis.causes || []).filter(cause => cause !== primary);
    const image = diagnosis.imageSelection;
    const imageText = image?.enabled === false ? "图片未用于分析" : Number(diagnosis.budget?.imageCount || 0) > 0 ? `已发送 ${diagnosis.budget.imageCount} 张证据图片` : diagnosis.budget?.imageCount != null ? "本次分析未发送图片" : "图片使用情况未记录";
    const details = `<h4>模型摘要</h4>${reading(diagnosis.summary || primary?.reason, "model-summary", "完整模型摘要")}<h4>其他候选与建议</h4>${supplemental.length ? supplemental.map(cause => `${reading(cause.reason, "supplemental-" + (diagnosis.causes || []).indexOf(cause), "完整候选")}<p class="rd-uncertainty">${esc(cause.uncertainty || "未单独提供不确定性")}</p>${references(cause.evidence_ids, diagnosis)}`).join("") : `<div class="rd-reading"><p>没有其他原因候选。</p></div>`}${(diagnosis.recommendations || []).slice(1).map((value, index) => reading(value.action, `recommendation-${index}`, "完整建议")).join("")}<h4>材料范围</h4><div class="rd-reading"><p>${esc([...(diagnosis.missing || []), ...(diagnosis.missing_evidence || [])].join("；") || "未记录额外的材料缺口。")}</p></div>`;
    const facts = (diagnosis.evidence || []).map(fact => `<article class="rd-fact-reference"><div class="rd-fact-reference-head"><strong>${esc(factLabel(fact))}</strong><button class="rp-button" data-r-material="${escapeAttr(fact.id)}">${icon("panel-right-open")}查看记录</button></div></article>`).join("");
    return `<section class="rd-section rd-analysis" id="rp-analysis">${heading}<div class="rd-analysis-block"><h4 class="rd-subheading">${icon("message-square-text")}主要判断</h4>${reading(primary?.reason || diagnosis.summary, "primary-reason", "完整判断")}${primary?.uncertainty ? `<p class="rd-uncertainty">待核验：${esc(primary.uncertainty)}</p>` : ""}${references(primary?.evidence_ids, diagnosis)}</div>${immediate ? `<div class="rd-analysis-block"><h4 class="rd-subheading">${icon("list-checks")}建议核查</h4>${reading(immediate.action, "immediate-action", "完整建议", 210)}${references(immediate.evidence_ids, diagnosis)}<div class="rd-result-actions">${command("jump-failure", "list-tree", "定位关联步骤")}</div></div>` : ""}<div class="rd-analysis-meta"><span>${icon("bot")}${esc(diagnosis.model || "模型未记录")}</span>${diagnosis.durationSeconds != null ? `<span>${icon("clock-3")}${Number(diagnosis.durationSeconds).toFixed(2)} 秒</span>` : ""}<span>${icon("image")}${esc(imageText)}</span><span>AI 推断不改变测试结果</span></div>${disclosure("analysis-details", "完整分析与材料范围", "补充候选、建议与限制", details)}${facts ? disclosure("analysis-facts", `分析引用记录 (${diagnosis.evidence.length})`, "原始输入记录", facts) : ""}</section>`;
  }

  function command(action, symbol, label = "", extra = "") {
    return `<button type="button" class="rp-button ${label ? "" : "rp-icon"} ${action === "open-picker" ? "rp-open-picker" : ""}" data-r="${action}" ${extra}>${icon(symbol)}${label ? `<span>${label}</span>` : ""}</button>`;
  }

  function badge(status) {
    const value = normalizeStatus(status);
    return `<span class="rp-status ${value}">${icon(({passed: "circle-check", failed: "circle-x", skipped: "circle-minus", unknown: "circle-help"})[value])}${esc(statusLabel(value))}</span>`;
  }

  function stepIdentity(row, step, index) {
    return step.evidenceId || `${row.id}/step-${index + 1}`;
  }

  function memory(row = current()) {
    if (!row) return {steps: new Set(), scroll: 0, filter: "all"};
    if (!workspace.memories.has(row.id)) {
      const index = (row.steps || []).findIndex(step => normalizeStatus(step.status) === "failed");
      workspace.memories.set(row.id, {steps: new Set(index >= 0 ? [stepIdentity(row, row.steps[index], index)] : []), disclosures: new Set(), scroll: 0, filter: "all"});
    }
    return workspace.memories.get(row.id);
  }

  function savePosition() {
    const scroller = document.querySelector(".rp-case-scroll");
    if (current() && scroller) memory().scroll = scroller.scrollTop;
  }

  function matches(row) {
    const text = [row.name, row.parentName, row.feature, ...(row.tags || []), ...(row.steps || []).map(step => step.name)].join(" ").toLowerCase();
    return (workspace.feature === "all" || row.featureId === workspace.feature) &&
      (workspace.status === "all" || normalizeStatus(row.status) === workspace.status) &&
      (!workspace.query || text.includes(workspace.query.toLowerCase()));
  }

  function visibleRows() {
    return allRows.filter(row => row.rowType === "outline"
      ? allRows.some(child => child.parentId === row.id && matches(child))
      : matches(row));
  }

  function navigation() {
    return `<div class="rp-nav-heading"><span>执行场景</span><span class="rp-nav-count"></span></div>
      <label class="rp-query">${icon("search")}<input type="search" data-r-field="query" aria-label="搜索执行场景" placeholder="搜索场景、步骤或标签"></label>
      <div class="rp-nav-filters"><label>Feature<select data-r-field="feature" aria-label="筛选 Feature"><option value="all">全部 Feature</option>${reportData.features.map((feature, index) => `<option value="${index}">${esc(feature.name)}</option>`).join("")}</select></label>
      <label>状态<select data-r-field="status" aria-label="筛选场景状态">${[["all", "全部状态"], ["failed", "需跟进"], ["unknown", "状态未知"], ["passed", "通过"], ["skipped", "未执行"]].map(([value, name]) => `<option value="${value}">${name}</option>`).join("")}</select></label></div>
      <div class="rp-nav-list"></div>`;
  }

  function updateNavigation() {
    const rows = visibleRows();
    document.querySelectorAll(".rp-nav-count").forEach(node => node.textContent = `${rows.filter(row => row.rowType !== "outline").length} / ${units.length}`);
    document.querySelectorAll("[data-r-field]").forEach(input => {
      const value = workspace[input.dataset.rField];
      if (value !== undefined && input.value !== value) input.value = value;
    });
    document.querySelectorAll(".rp-nav-list").forEach(list => {
      list.innerHTML = rows.map(row => {
        if (row.parentId && !workspace.expanded.has(row.parentId) && !workspace.query && workspace.status === "all") return "";
        return `<div class="rp-case-entry ${row.parentId ? "is-example" : ""}">${row.rowType === "outline" ? `<button class="rp-outline-toggle" data-r-outline="${row.id}" aria-label="展开或收起 ${escapeAttr(row.name)}" aria-expanded="${workspace.expanded.has(row.id)}">${icon(workspace.expanded.has(row.id) ? "chevron-down" : "chevron-right")}</button>` : ""}<button class="rp-case-choice ${row.id === workspace.selected ? "selected" : ""}" data-r-case="${row.id}" aria-current="${row.id === workspace.selected}"><span class="rp-choice-meta">${esc(row.rowType === "outline" ? "参数组" : row.rowType === "example" ? `Example ${row.exampleId}` : row.feature)}</span><strong>${esc(row.name)}</strong><span class="rp-choice-bottom">${badge(row.status)}<span>${esc(row.duration || "-")}</span></span></button></div>`;
      }).join("") || `<div class="rp-empty">${icon("search-x")}<strong>没有匹配的场景</strong><span>当前筛选下有 0 个执行单元</span><button class="rp-text-button" data-r="reset-filters">清除筛选</button></div>`;
    });
    decorate();
  }

  function materials(row = current()) {
    const items = [];
    const identities = new Set();
    const append = item => { if (!identities.has(item.id)) { identities.add(item.id); items.push(item); } };
    const attachments = (node, scope, stepId = null) => (node?.attachments || []).forEach(item => append({
      ...item, id: item.artifact_id || `${stepId || row.id}/${item.path}`, kind: item.type || "file", scope, stepId,
      state: item.status === "missing" ? "文件缺失" : "附件已登记",
    }));
    if (!row) return items;
    attachments(row, "场景附件");
    (row.steps || []).forEach((step, index) => {
      const stepId = stepIdentity(row, step, index);
      const scope = `Step ${index + 1} · ${step.name}`;
      attachments(step, scope, stepId);
      if (step.error || step.errorDetail) append({id: `${stepId}/error`, name: "异常与堆栈", kind: "error", scope, stepId, state: "异常已记录", text: step.errorDetail || step.error});
      if (step.logs?.length) append({id: `${stepId}/logs`, name: "关联日志", kind: "log", scope, stepId, state: `${step.logs.length} 条记录`, logs: step.logs});
    });
    (row.diagnosis?.evidence || []).forEach(fact => append({id: fact.id, name: ({step: "步骤事实", traceback: "分析使用的堆栈", action: "动作事实", log: "日志事实", image: "图片事实", execution: "执行信息"})[fact.kind] || "分析引用", kind: "fact", scope: fact.scopeId || "场景", stepId: fact.scopeId?.includes("/step-") ? fact.scopeId.split("/action-")[0] : null, text: fact.text, state: "已用于分析"}));
    return items;
  }

  const materialIcon = kind => ({image: "image", video: "film", error: "file-warning", log: "scroll-text", fact: "quote"})[kind] || "file";
  const materialLabel = kind => ({image: "图片", video: "视频", error: "异常", log: "日志", fact: "分析引用"})[kind] || "文件";
  const mediaSource = item => safeAttachmentHref(item.path) === "#" ? "" : String(item.path).trim();

  function failure(row) {
    return (row?.steps || []).find(step => normalizeStatus(step.status) === "failed");
  }

  function materialButton(item, compact = false) {
    return `<button class="rp-button ${compact ? "rp-material-chip" : ""}" data-r-material="${escapeAttr(item.id)}">${icon(materialIcon(item.kind))}<span>查看${esc(item.name)}</span></button>`;
  }

  function resultSection(row) {
    const failed = failure(row);
    const status = normalizeStatus(row.status);
    const related = materials(row).filter(item => item.kind === "image");
    related.sort((left, right) => Number(right.stepId === failed?.evidenceId) - Number(left.stepId === failed?.evidenceId));
    const main = failed ? conciseText(failed.diagnostic?.summary || failed.error || "失败步骤未提供异常摘要", 340) :
      status === "passed" ? `${(row.steps || []).filter(step => normalizeStatus(step.status) === "passed").length} 个步骤记录为通过。` :
      status === "skipped" ? row.skipReason || "该执行单元未执行。" : status === "failed" ? conciseText(row.error || "场景记录为失败，未提供步骤级异常。", 340) : "该执行单元没有确定的最终结果。";
    return `<section class="rd-section" id="rp-result">${sectionHeading("执行结果", "clipboard-check", badge(row.status))}<div class="rd-fact ${status}">${failed ? `<div class="rd-kicker">${icon("crosshair")}Step ${row.steps.indexOf(failed) + 1} · ${esc(failed.name)}</div>` : ""}<p class="rd-fact-lead">${esc(main)}</p></div><div class="rd-result-actions">${failed ? `<button class="rp-button rd-primary" data-r="jump-failure">${icon("list-tree")}定位失败步骤</button>` : ""}${related[0] ? materialButton(related[0], true) : ""}${!failed && materials(row).length ? `<a class="rp-button" href="#rp-materials" data-rd-section="rp-materials">${icon("files")}查看证据材料</a>` : ""}</div></section>`;
  }

  function stepSection(row) {
    const steps = row.steps || [];
    return `<section class="rd-section rd-steps" id="rp-steps">${sectionHeading("执行步骤", "list-tree", `<span class="rd-disclosure-meta">${steps.length} 个步骤</span>`)}${steps.length ? steps.map((step, index) => {
      const identity = stepIdentity(row, step, index);
      const opened = memory(row).steps.has(identity);
      const status = normalizeStatus(step.status);
      const summary = conciseText(step.diagnostic?.summary || step.error || ({passed: "该步骤记录为通过。", skipped: "该步骤未执行。", unknown: "没有确定的步骤结果。", failed: "该步骤失败，未提供异常摘要。"})[status], 360);
      const related = materials(row).filter(item => item.stepId === identity && item.kind !== "fact");
      const actions = (step.actions || []).length ? `<table class="rd-action-table"><thead><tr><th>动作</th><th>目标</th><th>结果</th></tr></thead><tbody>${step.actions.map(action => `<tr><td><code>${esc(action.operation || action.name || "action")}</code></td><td>${esc(action.owner?.view || action.target || "未记录")}</td><td>${esc(action.outcome === "exception" ? "抛出异常" : action.outcome === "returned_none" ? "调用返回" : action.outcome || action.status || "未提供")}</td></tr>`).join("")}</tbody></table>` : "";
      return `<article class="rp-step ${status}" id="rp-step-${escapeAttr(identity)}"><button class="rp-step-heading" data-r-step="${escapeAttr(identity)}" aria-expanded="${opened}" aria-controls="rp-step-body-${escapeAttr(identity)}"><span class="rp-step-chevron">${icon(opened ? "chevron-down" : "chevron-right")}</span><span class="rp-step-index">${String(index + 1).padStart(2, "0")}</span><span class="rp-step-name"><small>${esc(step.keyword || "Step")} · ${esc(step.duration || "耗时未记录")}</small><strong>${esc(step.name)}</strong></span>${badge(step.status)}<span class="rd-step-toggle-label">${opened ? "收起" : "展开"}</span></button><div class="rp-step-body" id="rp-step-body-${escapeAttr(identity)}" ${opened ? "" : "hidden"}><p>${esc(summary)}</p>${actions}<div class="rd-step-materials">${related.map(item => materialButton(item, true)).join("")}</div></div></article>`;
    }).join("") : `<div class="rd-empty-note"><strong>没有已记录的执行步骤</strong>该执行单元的状态与记录保持原样。</div>`}</section>`;
  }

  function materialSection(row) {
    const items = materials(row);
    const filter = memory(row).filter;
    const list = filter === "all" ? items : items.filter(item => item.kind === filter);
    return `<section class="rd-section" id="rp-materials"><div class="rd-section-heading"><div class="rd-section-title">${icon("files")}<h3>证据材料</h3><span class="rd-disclosure-meta">${items.length} 项</span></div><label class="rp-material-filter">类型<select data-r-field="material-filter" aria-label="筛选证据类型">${[["all", "全部材料"], ["image", "图片"], ["video", "视频"], ["log", "日志"], ["error", "异常"], ["fact", "分析引用"]].map(([value, label]) => `<option value="${value}" ${value === filter ? "selected" : ""}>${label}</option>`).join("")}</select></label></div><div class="rd-material-list">${list.map(item => `<div class="rd-material-row">${icon(materialIcon(item.kind))}<div><strong>${esc(item.name)}</strong><small>${esc(item.scope)}</small></div><span class="rd-material-state">${esc(item.state)}</span><button class="rp-button" data-r-material="${escapeAttr(item.id)}" aria-label="查看 ${escapeAttr(item.name)}">${icon("eye")}查看</button></div>`).join("") || `<div class="rd-empty-note"><strong>${items.length ? "没有符合当前筛选的材料" : "没有已记录的证据材料"}</strong>${items.length ? "原始记录未被删除。" : "未记录不等于文件已采集。"}</div>`}</div></section>`;
  }

  function outlineSection(row) {
    const examples = allRows.filter(item => item.parentId === row.id);
    return `<section class="rp-section"><div class="rp-section-heading"><h3>Examples</h3><span>${examples.length} 个执行单元</span></div>${examples.map(example => `<button class="rp-example-row" data-r-case="${example.id}"><span><strong>Example ${esc(example.exampleId)}</strong><small>${esc(Object.entries(example.params || {}).map(([key, value]) => `${key}=${value}`).join(" · "))}</small></span>${badge(example.status)}${icon("arrow-right")}</button>`).join("")}</section>`;
  }

  function mountDetails() {
    if (document.getElementById("rpDetailLayout")) return;
    document.getElementById("detailView").innerHTML = `<div class="rp-detail-layout" id="rpDetailLayout"><nav class="rp-case-nav" aria-label="执行场景导航">${navigation()}</nav><div class="rp-pane-resizer" id="rpNavResizer" title="左右拖拽调节场景列表宽度"></div><section class="rp-case-panel" aria-label="场景执行详情"><div id="rpCaseHeader"></div><nav class="rp-section-links" aria-label="本页区段"></nav><div class="rp-case-scroll" tabindex="0" aria-label="场景记录"></div></section><div class="rp-pane-resizer" id="rpInspectorResizer" title="左右拖拽调节证据查看器宽度" hidden></div><aside class="rp-inline-inspector" aria-label="证据查看器" hidden></aside></div>`;
    picker.innerHTML = `<header class="rp-picker-heading"><h2>选择执行场景</h2>${command("close-picker", "x", "", 'aria-label="关闭场景导航" title="关闭场景导航"')}</header><nav aria-label="场景选择">${navigation()}</nav>`;
    setupDetailResizers();
  }

  function setupDetailResizers() {
    const layout = document.getElementById("rpDetailLayout");
    const navResizer = document.getElementById("rpNavResizer");
    const inspResizer = document.getElementById("rpInspectorResizer");
    if (!layout) return;
    let mode = null;
    if (navResizer) {
      navResizer.addEventListener("mousedown", e => {
        mode = "nav";
        navResizer.classList.add("dragging");
        e.preventDefault();
      });
    }
    if (inspResizer) {
      inspResizer.addEventListener("mousedown", e => {
        mode = "inspector";
        inspResizer.classList.add("dragging");
        e.preventDefault();
      });
    }
    window.addEventListener("mousemove", e => {
      if (!mode) return;
      const rect = layout.getBoundingClientRect();
      if (mode === "nav") {
        const w = Math.min(420, Math.max(185, e.clientX - rect.left));
        layout.style.setProperty("--rp-nav-width", `${w}px`);
      } else if (mode === "inspector") {
        const w = Math.min(Math.max(280, rect.right - e.clientX), Math.max(360, rect.width - 360));
        layout.style.setProperty("--rp-inspector-width", `${w}px`);
      }
    });
    window.addEventListener("mouseup", () => {
      if (!mode) return;
      mode = null;
      navResizer?.classList.remove("dragging");
      inspResizer?.classList.remove("dragging");
    });
  }

  function renderCase() {
    const row = current();
    const header = document.getElementById("rpCaseHeader");
    const scroller = document.querySelector(".rp-case-scroll");
    if (!header || !scroller) return;
    const list = visibleRows().filter(item => item.rowType !== "outline");
    const position = list.findIndex(item => item.id === row?.id);
    header.innerHTML = `<div class="rp-case-toolbar">${command("open-picker", "panel-left", "场景")}<span>${row ? esc(row.feature) : "当前筛选"}</span><div class="rp-case-arrows">${command("previous", "chevron-left", "", `aria-label="上一个场景" title="上一个场景" ${position <= 0 ? "disabled" : ""}`)}${command("next", "chevron-right", "", `aria-label="下一个场景" title="下一个场景" ${position < 0 || position >= list.length - 1 ? "disabled" : ""}`)}</div></div>${row ? `<div class="rp-case-title"><h2 tabindex="-1">${esc(row.name)}</h2>${badge(row.status)}</div><div class="rp-case-meta"><span>${icon("clock-3")}${esc(row.duration || "-")}</span><span>${row.rowType === "example" ? `Example ${esc(row.exampleId)}` : row.rowType === "outline" ? "Scenario Outline" : "Scenario"}</span>${(row.tags || []).filter(tag => !String(tag).startsWith("step:")).map(tag => `<span class="rp-tag">@${esc(tag)}</span>`).join("")}</div>` : ""}`;
    const sectionNavigation = document.querySelector(".rp-section-links");
    sectionNavigation.hidden = !row || row.rowType === "outline";
    sectionNavigation.innerHTML = `<span class="rd-nav-label">本页</span>${[["rp-result", "结果", "clipboard-check"], ["rp-analysis", "分析", "sparkles"], ["rp-steps", "步骤", "list-tree"], ["rp-materials", "材料", "files"]].map(([id, label, symbol]) => `<a class="rd-section-link" href="#${id}" data-rd-section="${id}">${icon(symbol)}${label}</a>`).join("")}`;
    if (!row) {
      scroller.innerHTML = `<div class="rp-empty rp-empty-main">${icon("list-filter")}<strong>当前筛选没有匹配的场景</strong><span>没有显示筛选范围之外的执行结果</span>${command("reset-filters", "filter-x", "清除筛选")}</div>`;
    } else {
      scroller.innerHTML = row.rowType === "outline" ? outlineSection(row) : `${resultSection(row)}${analysisSection(row)}${stepSection(row)}${materialSection(row)}${disclosure("source-information", "来源与执行信息", "规格、执行身份与标签", `<dl class="rp-source-details"><dt>规格文件</dt><dd>${esc(row.file || "未提供")}</dd><dt>执行身份</dt><dd>${esc(row.executionId || reportData.executionSummary?.executionId || "旧记录未提供")}</dd><dt>标签</dt><dd>${esc((row.tags || []).join(" · ") || "无")}</dd><dt>项目</dt><dd>${esc(reportData.project || "未提供")}</dd></dl>`)}`;
    }
    scroller.querySelectorAll("[data-rd-disclosure]").forEach(details => details.open = Boolean(memory(row).disclosures?.has(details.dataset.rdDisclosure)));
    scroller.scrollTop = row ? memory(row).scroll : 0;
    scroller.onscroll = () => { if (current()) memory().scroll = scroller.scrollTop; syncSections(); };
    decorate();
    syncSections();
    renderInspector();
  }

  function syncSections() {
    const scroller = document.querySelector(".rp-case-scroll");
    if (!scroller) return;
    const sections = [...scroller.querySelectorAll(".rd-section[id]")];
    const top = scroller.getBoundingClientRect().top + 64;
    let active = sections[0]?.id;
    sections.forEach(section => { if (section.getBoundingClientRect().top <= top) active = section.id; });
    if (scroller.scrollHeight > scroller.clientHeight + 1 && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 2) active = sections.at(-1)?.id;
    document.querySelectorAll(".rd-section-link").forEach(link => {
      if (link.dataset.rdSection === active) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    });
  }

  function enhanceOverview() {
    document.querySelectorAll(".feature-tree-row").forEach(button => {
      const row = document.createElement("div");
      row.className = button.className;
      row.innerHTML = button.innerHTML;
      const toggle = row.querySelector(".feature-tree-chevron");
      if (toggle) toggle.outerHTML = `<button class="rd-feature-toggle" data-rd-feature="${button.dataset.toggleFeature}" aria-expanded="${button.getAttribute("aria-expanded")}" aria-label="${button.getAttribute("aria-expanded") === "true" ? "收起" : "展开"} ${escapeAttr(button.querySelector(".feature-tree-name")?.textContent || "Feature")}">${icon(button.getAttribute("aria-expanded") === "true" ? "chevron-down" : "chevron-right")}</button>`;
      const last = row.querySelector(".execution-row-arrow");
      if (last) last.textContent = "";
      button.replaceWith(row);
    });
    document.querySelectorAll(".scenario-execution-row .execution-row-arrow").forEach(node => node.innerHTML = `<span class="rd-open-case">查看${icon("arrow-right")}</span>`);
    const kpiFilterMap = {1: "all", 3: "passed", 4: "failed", 5: "unknown", 6: "skipped"};
    document.querySelectorAll("#overviewSummaryLine .overview-summary-item").forEach((card, idx) => {
      if (kpiFilterMap[idx]) {
        card.setAttribute("data-kpi-filter", kpiFilterMap[idx]);
        card.title = "点击按此状态筛选下方场景清单";
      }
    });
  }

  function updateRoute(replace = false) {
    const params = new URLSearchParams({view: state.view});
    if (state.view === "details" && workspace.selected) params.set("case", workspace.selected);
    const hash = `#${params}`;
    if (location.hash !== hash) history[replace ? "replaceState" : "pushState"](null, "", hash);
  }

  function renderShared() {
    document.body.dataset.reportView = state.view;
    document.querySelectorAll(".rp-global-nav [data-r]").forEach(button => {
      const active = button.dataset.r === (state.view === "overview" ? "home" : "resume");
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    const index = attention.findIndex(row => row.id === workspace.selected);
    document.querySelector(".rp-breadcrumb").textContent = state.view === "overview" ? "运行报告 / 执行概览" : `运行报告 / ${current()?.feature || "场景详情"}`;
    document.getElementById("rpAttentionCount").textContent = state.view === "details" && index >= 0 ? `关注项 ${index + 1} / ${attention.length}` : `${attention.length} 项需关注`;
    document.querySelector('[data-r="attention"]').disabled = !attention.length || (state.view === "details" && attention.length === 1 && index === 0);
    const summary = reportData.executionSummary;
    const diagnosis = reportData.diagnosis;
    const diagnoses = diagnosis?.items?.length ? diagnosis.items : units.map(row => row.diagnosis).filter(Boolean);
    const diagnosisLabel = diagnosis?.enabled === false || diagnosis?.status === "disabled" ? "未启用" :
      diagnosis ? ({mixed_sources: "按来源提供"})[diagnosis.status] || diagnosisStatus(diagnosis.status) : diagnoses.length ? "按场景提供" : "未提供";
    const captureLabel = ({complete: "完整", incomplete: "不完整", unavailable: "不可用"})[summary?.captureStatus] || summary?.captureStatus || "未提供";
    const outcomeLabel = ({aborted: "运行中断", mixed_sources: "多来源结果"})[summary?.outcome];
    const errors = [...(summary?.exceptions || []), ...(summary?.errors || [])];
    const runDiagnoses = diagnoses.filter(item => item.kind === "run");
    document.getElementById("runEvidenceSummary").innerHTML = `<div class="rp-run-strip"><span>${icon("activity")}运行结果 ${summary ? outcomeLabel ? esc(outcomeLabel) : badge(summary.outcome) : "旧报告未提供"}</span><span>${icon("database")}采集${esc(captureLabel)}</span><span>${icon("sparkles")}AI 诊断 ${esc(diagnosisLabel)}${diagnoses.length ? ` · ${diagnoses.filter(item => item.status === "completed").length}/${diagnoses.length} 项已生成` : ""}</span></div>${errors.length ? disclosure("run-issues", `运行问题 (${errors.length})`, "生命周期异常", `<pre>${esc(errors.map(error => `${error.stage || ""} ${error.type || ""}: ${error.message || ""}`).join("\n"))}</pre>`) : ""}${runDiagnoses.map((item, index) => disclosure(`run-analysis-${index}`, `运行级 AI 诊断 · ${diagnosisStatus(item.status)}`, "诊断记录", renderAIDiagnosis(item))).join("")}${diagnosis?.reason ? disclosure("diagnosis-information", "诊断说明", "记录的范围与限制", `<p class="rd-reading">${esc(diagnosis.reason)}</p>`) : ""}`;
    document.querySelectorAll("#runEvidenceSummary [data-rd-disclosure]").forEach(details => details.open = runDisclosures.has(details.dataset.rdDisclosure));
    document.getElementById("reportTitle").textContent = "执行概览";
    document.getElementById("reportMeta").textContent = `${reportData.features.length} 个 Feature · ${units.length} 个执行单元`;
    document.querySelector(".execution-toolbar-title").textContent = "场景清单";
    document.getElementById("sidebarProject").textContent = reportData.project || "BDD Autowork";
    document.getElementById("sidebarRunMeta").textContent = String(reportData.startedAt || "").slice(0, 10);
    const scroller = document.querySelector(".execution-scroller");
    if (scroller) scroller.setAttribute("aria-label", "Feature 分组执行表");
    enhanceOverview();
    decorate();
  }

  setView = function (view) {
    originalSetView(view);
    renderShared();
  };

  render = function () {
    if (state.view === "overview") originalRender();
    else {
      mountDetails();
      originalSetView("details");
      updateNavigation();
      renderCase();
    }
    renderShared();
  };

  function choose(id, route = true) {
    savePosition();
    closeEvidence(false);
    workspace.selected = id || null;
    state.selectedId = workspace.selected;
    state.view = "details";
    const row = current();
    if (row?.parentId) workspace.expanded.add(row.parentId);
    if (picker.open) picker.close();
    render();
    if (route) updateRoute();
  }

  openDetails = function ({rowId = null, filter = null} = {}) {
    workspace.query = "";
    workspace.feature = "all";
    workspace.status = filter || "all";
    choose(rowId || visibleRows().find(row => row.rowType !== "outline")?.id);
  };

  function home(route = true) {
    savePosition();
    closeEvidence(false);
    if (picker.open) picker.close();
    state.view = "overview";
    render();
    if (route) updateRoute();
  }

  function applyFilters() {
    const rows = visibleRows();
    if (!rows.some(row => row.id === workspace.selected)) {
      savePosition();
      closeEvidence(false);
      workspace.selected = rows.find(row => row.rowType !== "outline")?.id || null;
      state.selectedId = workspace.selected;
      renderCase();
      updateRoute(true);
    }
    updateNavigation();
    const selectable = rows.filter(row => row.rowType !== "outline");
    const position = selectable.findIndex(row => row.id === workspace.selected);
    const previous = document.querySelector('[data-r="previous"]');
    const next = document.querySelector('[data-r="next"]');
    if (previous) previous.disabled = position <= 0;
    if (next) next.disabled = position < 0 || position >= selectable.length - 1;
    renderShared();
  }

  function jump(id) {
    const target = document.getElementById(id);
    const scroller = document.querySelector(".rp-case-scroll");
    if (!target || !scroller) return;
    scroller.scrollTop += target.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 14;
    const focus = target.querySelector("button, summary, h3") || target;
    if (!focus.hasAttribute("tabindex")) focus.setAttribute("tabindex", "-1");
    focus.focus({preventScroll: true});
    triggerReportSpotlight(target);
    syncSections();
  }

  function closeEvidence(restore = true) {
    workspace.material = null;
    if (evidenceDialog.open) evidenceDialog.close();
    evidenceDialog.innerHTML = "";
    document.getElementById("rpDetailLayout")?.classList.remove("has-inspector");
    const inspResizer = document.getElementById("rpInspectorResizer");
    if (inspResizer) inspResizer.hidden = true;
    const inspector = document.querySelector(".rp-inline-inspector");
    if (inspector) { inspector.hidden = true; inspector.innerHTML = ""; }
    if (restore && evidenceTrigger?.isConnected) evidenceTrigger.focus({preventScroll: true});
  }

  function inspectorContent(item) {
    const row = current();
    const failed = failure(row);
    const original = mediaSource(item);
    const logs = (item.logs || []).filter(entry => workspace.logLevel === "all" || entry.level === workspace.logLevel);
    let text = item.text || "未提供文本";
    if (item.kind === "fact") { try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (_) {} }
    const body = item.kind === "image" ? `<div class="rp-image-stage"><img src="${escapeAttr(original)}" alt="${escapeAttr(item.name)}" style="width:${workspace.scale * 100}%;max-width:${workspace.scale > 1 ? "none" : "100%"}"></div>` : item.kind === "video" ? `<video controls preload="metadata" src="${escapeAttr(original)}"></video>` : item.kind === "log" ? `<div class="rp-log-list">${logs.map(entry => `<div class="rp-log-line"><span>${esc(String(entry.observed_at || "").split("T")[1]?.slice(0, 8) || "-")}</span><b class="${entry.level === "ERROR" ? "rp-error-text" : ""}">${esc(entry.level)}</b><pre>${esc(entry.message)}</pre></div>`).join("") || `<div class="rp-inline-empty">此级别没有日志记录</div>`}</div>` : `<pre class="rp-raw-text">${esc(text)}</pre>`;
    return `<header class="rp-inspector-header"><div><span class="rp-eyebrow">${materialLabel(item.kind)} · ${esc(item.state)}</span><h2>${icon(materialIcon(item.kind))}${esc(item.name)}</h2></div>${command("close-evidence", "x", "", 'aria-label="关闭证据查看器" title="关闭证据查看器"')}</header><div class="rp-inspector-context"><strong>${esc(row?.name || "")}</strong><span>${esc(item.scope)}</span>${failed ? `<p><b>记录的差异</b>${esc(conciseText(failed.diagnostic?.summary || failed.error, 190))}</p>` : ""}</div><div class="rp-inspector-toolbar">${item.kind === "image" ? `${command("zoom-out", "zoom-out", "", 'aria-label="缩小图片" title="缩小图片"')}<span class="rp-zoom-badge">${Math.round(workspace.scale * 100)}%</span>${command("zoom-in", "zoom-in", "", 'aria-label="放大图片" title="放大图片"')}${command("fit", "scan", "", 'aria-label="适应宽度" title="适应宽度"')}` : item.kind === "video" ? "" : command("copy", "copy", "复制")}${item.kind === "log" ? `<select data-r-field="log-level" aria-label="筛选日志级别">${["all", "ERROR", "WARNING", "INFO", "DEBUG"].map(level => `<option value="${level}" ${workspace.logLevel === level ? "selected" : ""}>${level === "all" ? "全部级别" : level}</option>`).join("")}</select>` : ""}<span></span>${item.stepId ? command("locate-step", "list-tree", "定位步骤") : ""}${item.path && original ? `<a class="rp-button rp-icon" href="${escapeAttr(original)}" target="_blank" rel="noopener noreferrer" aria-label="打开原文件" title="打开原文件">${icon("external-link")}</a>` : ""}</div><div class="rp-inspector-body">${body}</div><footer class="rp-inspector-footer" role="status">${item.kind === "log" ? `${logs.length} / ${(item.logs || []).length} 条日志` : "已有记录 · 不重新采集"}</footer>`;
  }

  function renderInspector() {
    if (!workspace.material) return;
    const item = materials().find(entry => entry.id === workspace.material);
    if (!item) { closeEvidence(false); return; }
    const mobile = matchMedia("(max-width: 760px)").matches;
    const inline = document.querySelector(".rp-inline-inspector");
    const inspResizer = document.getElementById("rpInspectorResizer");
    const target = mobile ? evidenceDialog : inline;
    if (!target) return;
    if (!mobile && evidenceDialog.open) evidenceDialog.close();
    inline.hidden = mobile;
    if (inspResizer) inspResizer.hidden = mobile;
    document.getElementById("rpDetailLayout").classList.toggle("has-inspector", !mobile);
    target.innerHTML = inspectorContent(item);
    if (mobile && !evidenceDialog.open) evidenceDialog.showModal();
    const media = target.querySelector("img, video");
    if (media) media.onerror = () => { target.querySelector(".rp-inspector-body").innerHTML = `<div class="rp-empty"><strong>材料无法在浏览器中读取</strong><span>附件路径已记录，但不代表文件仍可用</span></div>`; };
    if (media && !mediaSource(item)) {
      media.removeAttribute("src");
      target.querySelector(".rp-inspector-body").innerHTML = `<div class="rp-empty"><strong>附件路径不可用</strong><span>只支持报告中登记的相对文件路径</span></div>`;
    }
    decorate();
  }

  function openEvidence(id, trigger) {
    if (!materials().some(item => item.id === id)) return;
    workspace.material = id;
    workspace.logLevel = "all";
    workspace.scale = 1;
    evidenceTrigger = trigger;
    renderInspector();
    (evidenceDialog.open ? evidenceDialog : document.querySelector(".rp-inline-inspector"))?.querySelector('[data-r="close-evidence"]')?.focus({preventScroll: true});
  }

  function restoreRoute() {
    const params = new URLSearchParams(location.hash.slice(1));
    if (params.get("view") === "details") {
      const id = params.get("case");
      workspace.query = ""; workspace.status = "all"; workspace.feature = "all";
      choose(allRows.some(row => row.id === id) ? id : attention[0]?.id || units[0]?.id, false);
    } else home(false);
  }

  document.body.classList.add("report-workspace", "report-controls");
  document.querySelector(".shell > .sidebar").innerHTML = `<div class="rp-brand"><span class="rp-brand-mark">${icon("workflow")}</span><div><strong>BDD Autowork</strong><span>执行报告</span></div></div><span class="rp-nav-label">报告</span><nav class="rp-global-nav" aria-label="报告导航"><button data-r="home" title="执行概览">${icon("layout-dashboard")}<span>执行概览</span><small>${units.length}</small></button><button data-r="resume" title="场景详情">${icon("list-tree")}<span>场景详情</span></button></nav><div class="rp-side-summary"><span>执行状态</span>${[["passed", "通过"], ["failed", "需跟进"], ["unknown", "状态未知"], ["skipped", "未执行"]].map(([status, label]) => `<div><i class="${status}"></i><span>${label}</span><strong>${units.filter(row => normalizeStatus(row.status) === status).length}</strong></div>`).join("")}</div><div class="rp-side-footer"><span class="rp-local-dot"></span><div><strong id="sidebarProject"></strong><span id="sidebarRunMeta"></span></div></div>`;
  document.querySelector(".content").insertAdjacentHTML("afterbegin", `<header class="rp-report-header"><div><div class="rp-breadcrumb">运行报告 / 执行概览</div><div class="rp-report-title"><h1>执行报告</h1></div></div><div class="rp-header-actions"><span id="rpAttentionCount"></span>${command("attention", "arrow-right", "下个关注项", 'title="下一个失败或状态未知的执行单元"')}${command("toggle-theme", "sun", "", 'aria-label="切换深色 / 浅色外观" title="切换深色 / 浅色外观"')}</div></header>`);

  document.addEventListener("click", event => {
    const kpi = event.target.closest("[data-kpi-filter]");
    if (kpi && state.view === "overview") {
      const status = kpi.dataset.kpiFilter;
      if (state.executionStatus !== status) collapseAllFeatures();
      state.executionStatus = status;
      render();
      triggerReportSpotlight(document.getElementById("featureExecutionTree"));
      showReportToast(`已按“${kpi.childNodes[0]?.textContent || status}”筛选场景清单`, "pass");
      return;
    }
    const link = event.target.closest("[data-rd-section]");
    if (link) { event.preventDefault(); jump(link.dataset.rdSection); return; }
    const feature = event.target.closest("[data-rd-feature]");
    if (feature) { const id = Number(feature.dataset.rdFeature); state.collapsedFeatures.has(id) ? state.collapsedFeatures.delete(id) : state.collapsedFeatures.add(id); render(); return; }
  });
  document.addEventListener("toggle", event => {
    const details = event.target.closest?.("[data-rd-disclosure]");
    if (!details) return;
    const set = details.closest("#runEvidenceSummary") ? runDisclosures : current() ? memory().disclosures : null;
    if (!set) return;
    details.open ? set.add(details.dataset.rdDisclosure) : set.delete(details.dataset.rdDisclosure);
    syncSections();
  }, true);

  document.addEventListener("click", event => {
    const reference = event.target.closest(".rp-ai-section [data-diagnosis-ref]");
    if (!reference) return;
    if (reference.dataset.diagnosisRef.startsWith("step-")) {
      const id = reference.dataset.diagnosisRef.slice(5);
      if (!(current()?.steps || []).some((step, index) => stepIdentity(current(), step, index) === id)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      memory().steps.add(id);
      renderCase();
      jump("rp-step-" + id);
      return;
    }
    const fact = [...(current()?.diagnosis?.evidence || [])].sort((left, right) => right.id.length - left.id.length).find(item => reference.dataset.diagnosisRef.endsWith("-" + item.id));
    if (!fact) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    openEvidence(fact.id, reference);
  }, true);

  document.addEventListener("click", event => {
    const target = event.target.closest("[data-r], [data-r-case], [data-r-step], [data-r-material], [data-r-jump], [data-r-outline]");
    if (!target || target.disabled) return;
    if (target.dataset.rCase) { choose(target.dataset.rCase); document.querySelector(".rp-case-title h2")?.focus({preventScroll: true}); return; }
    if (target.dataset.rMaterial) { openEvidence(target.dataset.rMaterial, target); return; }
    if (target.dataset.rJump) { jump(target.dataset.rJump); return; }
    if (target.dataset.rOutline) { const id = target.dataset.rOutline; workspace.expanded.has(id) ? workspace.expanded.delete(id) : workspace.expanded.add(id); updateNavigation(); return; }
    if (target.dataset.rStep) {
      const id = target.dataset.rStep;
      memory().steps.has(id) ? memory().steps.delete(id) : memory().steps.add(id);
      document.getElementById("rp-step-body-" + id).hidden = !memory().steps.has(id);
      target.setAttribute("aria-expanded", String(memory().steps.has(id)));
      target.querySelector(".rp-step-chevron").innerHTML = icon(memory().steps.has(id) ? "chevron-down" : "chevron-right");
      target.querySelector(".rd-step-toggle-label").textContent = memory().steps.has(id) ? "收起" : "展开";
      decorate(); return;
    }
    const action = target.dataset.r;
    if (action === "home") home();
    if (action === "resume") choose(workspace.selected || attention[0]?.id || units[0]?.id);
    if (action === "reset-filters") { workspace.query = ""; workspace.status = "all"; workspace.feature = "all"; applyFilters(); showReportToast("✓ 已重置筛选条件", "pass"); }
    if (action === "open-picker") { picker.showModal(); picker.querySelector("input")?.focus(); }
    if (action === "close-picker") picker.close();
    if (action === "close-evidence") closeEvidence();
    if (action === "toggle-theme") {
      workspace.theme = workspace.theme === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", workspace.theme);
      document.body.setAttribute("data-theme", workspace.theme);
      try { localStorage.setItem("autowork_spark_theme", workspace.theme); } catch (_) {}
      showReportToast(workspace.theme === "dark" ? "已切换为深色外观" : "已切换为浅色外观", "pass");
    }
    if (action === "attention") {
      const index = attention.findIndex(row => row.id === workspace.selected);
      workspace.query = ""; workspace.status = "all"; workspace.feature = "all";
      choose(attention[state.view === "overview" ? 0 : (index + 1) % attention.length]?.id);
      showReportToast("已定位下个关注场景", "warn");
    }
    if (action === "previous" || action === "next") { const list = visibleRows().filter(row => row.rowType !== "outline"); const index = list.findIndex(row => row.id === workspace.selected); if (list[index + (action === "next" ? 1 : -1)]) choose(list[index + (action === "next" ? 1 : -1)].id); }
    if (action === "jump-failure") {
      const row = current();
      const index = (row?.steps || []).findIndex(step => normalizeStatus(step.status) === "failed");
      if (index >= 0) {
        const id = stepIdentity(row, row.steps[index], index);
        memory(row).steps.add(id);
        renderCase();
        jump("rp-step-" + id);
      }
    }
    if (action === "locate-step") { const item = materials().find(entry => entry.id === workspace.material); if (item?.stepId) { memory().steps.add(item.stepId); if (evidenceDialog.open) closeEvidence(false); renderCase(); jump("rp-step-" + item.stepId); } }
    if (["zoom-in", "zoom-out", "fit"].includes(action)) { workspace.scale = action === "fit" ? 1 : Math.max(.5, Math.min(4, workspace.scale + (action === "zoom-in" ? .25 : -.25))); renderInspector(); }
    if (action === "copy") {
      const panel = evidenceDialog.open ? evidenceDialog : document.querySelector(".rp-inline-inspector");
      const feedback = panel.querySelector(".rp-inspector-footer");
      if (!navigator.clipboard) feedback.textContent = "复制不可用，可选择正文复制";
      else navigator.clipboard.writeText(panel.querySelector(".rp-inspector-body").innerText).then(() => { feedback.textContent = "文本已复制"; showReportToast("✓ 已复制证据内容到剪贴板", "pass"); }).catch(() => feedback.textContent = "未获复制权限，可选择正文复制");
    }
  });

  document.addEventListener("input", event => {
    if (event.target.dataset.rField !== "query") return;
    workspace.query = event.target.value;
    applyFilters();
  });
  document.addEventListener("change", event => {
    const field = event.target.dataset.rField;
    if (field === "status" || field === "feature") { workspace[field] = event.target.value; applyFilters(); }
    if (field === "material-filter") { memory().filter = event.target.value; savePosition(); renderCase(); jump("rp-materials"); }
    if (field === "log-level") { workspace.logLevel = event.target.value; renderInspector(); }
  });
  evidenceDialog.addEventListener("cancel", event => { event.preventDefault(); closeEvidence(); });
  picker.addEventListener("close", () => document.querySelector('[data-r="open-picker"]')?.focus({preventScroll: true}));
  document.addEventListener("keydown", event => { if (event.key === "Escape" && workspace.material && !picker.open) { event.preventDefault(); closeEvidence(); } });
  window.addEventListener("popstate", restoreRoute);
  window.addEventListener("hashchange", restoreRoute);
  matchMedia("(max-width: 760px)").addEventListener("change", renderInspector);
  restoreRoute();
  updateRoute(true);
})();
