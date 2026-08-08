const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
const state = { entries: [], schedules: [], settings: {} };

const labels = {
  active: "生效", trial: "试用", draft: "草稿", suspended: "暂停", archived: "归档",
  succeeded: "成功", running: "运行中", deferred: "待重试", failed: "失败", partial: "部分完成",
  scheduled: "定时", catch_up: "补跑", manual: "手动",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

function showToast(message, error = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { toast.className = "toast"; }, 3600);
}

async function api(method, endpoint, body) {
  const fn = method === "GET" ? bridge.apiGet : bridge.apiPost;
  return fn(endpoint, body);
}

function formatTime(value) {
  if (!value) return "未运行";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value).replace("T", " ").replace("Z", "") : date.toLocaleString("zh-CN", { hour12: false });
}

function renderMetrics(data) {
  const counts = data.counts || {};
  const settings = state.settings || {};
  const usedBudget = data.budget || {};
  const budget = `${usedBudget.request_count || 0}/${settings.daily_request_budget ?? 8} req`;
  const rows = [
    ["学习目标", counts.learning_targets || 0, data.snapshot?.capture_enabled ? "捕获开关开启" : "捕获开关关闭"],
    ["原始消息", counts.conversation_messages || 0, "保留 14 天"],
    ["待处理 anchor", counts.pending_anchors ?? counts.trigger_anchors ?? 0, "问答证据"],
    ["记忆条目", counts.entries || 0, "按层级注入"],
    ["学习运行", counts.learning_runs || 0, "含失败和补跑"],
    ["今日预算", budget, `${(usedBudget.input_tokens_estimated || 0).toLocaleString()}/${(settings.daily_input_token_budget ?? 16000).toLocaleString()} tok`],
  ];
  $("metrics").innerHTML = rows.map(([label, value, note]) => `<div class="metric"><span class="metric-label">${escapeHtml(label)}</span><strong class="metric-value">${escapeHtml(value)}</strong><span class="metric-note">${escapeHtml(note)}</span></div>`).join("");
  const pill = $("health-pill");
  if (data.degraded) { pill.textContent = "采集降级"; pill.className = "pill error"; }
  else if (!data.snapshot?.capture_enabled) { pill.textContent = "捕获已关闭"; pill.className = "pill warn"; }
  else { pill.textContent = data.queue_depth ? `队列 ${data.queue_depth}` : "运行正常"; pill.className = "pill"; }
  const alert = $("alert");
  if (data.degraded || data.last_error) {
    alert.hidden = false;
    alert.textContent = data.last_error ? `最近错误: ${data.last_error}` : "捕获缓冲区曾经满载, 新学习任务已暂停, 普通聊天不受影响.";
  } else alert.hidden = true;
}

function renderTargets(rows) {
  $("targets").innerHTML = rows.length ? rows.map((row) => `<div class="list-row"><div class="list-main"><strong>${escapeHtml(row.chat_type === "group" ? "QQ群聊" : "QQ私聊")} · ${escapeHtml(row.peer_id)}</strong><small>${escapeHtml(row.target_key)}${row.label ? ` · ${escapeHtml(row.label)}` : ""}</small></div><div class="row-actions"><span class="${row.enabled ? "state-on" : "state-off"}">${row.enabled ? "学习中" : "已停止"}</span><button class="minor target-toggle" data-id="${escapeHtml(row.target_id)}" data-enabled="${row.enabled ? "0" : "1"}">${row.enabled ? "停止" : "开启"}</button></div></div>`).join("") : '<p class="empty">还没有学习目标. 先添加一个低风险会话.</p>';
}

function renderSchedules(rows) {
  state.schedules = rows;
  $("schedules").innerHTML = rows.length ? rows.map((row) => `<div class="list-row"><div class="list-main"><strong>${escapeHtml(row.local_time)}</strong><small>${escapeHtml(row.timezone)}</small></div><div class="row-actions"><span class="${row.enabled ? "state-on" : "state-off"}">${row.enabled ? "启用" : "停用"}</span><button class="minor schedule-edit" data-id="${escapeHtml(row.schedule_id)}">编辑</button><button class="minor schedule-toggle" data-id="${escapeHtml(row.schedule_id)}" data-enabled="${row.enabled ? "0" : "1"}">${row.enabled ? "停用" : "启用"}</button><button class="minor danger schedule-delete" data-id="${escapeHtml(row.schedule_id)}"${rows.length === 1 ? ' disabled title="至少保留一个时间点, 不需要定时学习时请停用"' : ""}>删除</button></div></div>`).join("") : '<p class="empty">没有时间点.</p>';
}

function renderRuns(rows) {
  if (!rows.length) { $("runs").innerHTML = '<p class="empty">还没有学习运行记录.</p>'; return; }
  $("runs").innerHTML = `<table><thead><tr><th>时间</th><th>类型</th><th>状态</th><th>请求</th><th>输入 Token</th><th>完成</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${escapeHtml(formatTime(row.created_at))}</td><td>${escapeHtml(labels[row.run_kind] || row.run_kind)}</td><td class="status-${escapeHtml(row.status)}">${escapeHtml(labels[row.status] || row.status)}</td><td>${escapeHtml(row.request_count)}</td><td>${escapeHtml(row.input_tokens_estimated)}</td><td>${escapeHtml(formatTime(row.completed_at))}</td></tr>`).join("")}</tbody></table>`;
}

function triggersOf(row) {
  try { return JSON.parse(row.triggers_json || "[]"); } catch { return []; }
}

function renderEntries() {
  const query = $("entry-search").value.trim().toLowerCase();
  const scope = $("entry-scope").value;
  const status = $("entry-status").value;
  const filtered = state.entries.filter((row) => {
    const triggerText = triggersOf(row).join(" ");
    return (!scope || row.scope_type === scope) && (!status || row.status === status) && (!query || `${row.content} ${triggerText} ${row.scope_key}`.toLowerCase().includes(query));
  });
  $("entries").innerHTML = filtered.length ? filtered.map((row) => `<article class="entry-card"><div class="entry-card-head"><strong>${escapeHtml(row.scope_type)} · ${escapeHtml(row.scope_key || "*")}</strong><span class="tag">${escapeHtml(labels[row.status] || row.status)}</span></div><p class="entry-content">${escapeHtml(row.content)}</p><div class="entry-meta"><span>${escapeHtml(row.kind)}</span><span>${escapeHtml(row.trust_level)}</span><span>v${escapeHtml(row.version)}</span><span>证据 ${escapeHtml(row.evidence_count || 0)} 条 / ${escapeHtml(row.evidence_days || 0)} 天</span>${triggersOf(row).length ? `<span>${escapeHtml(triggersOf(row).join(" / "))}</span>` : ""}</div><div class="entry-actions">${Number(row.version) > 1 ? `<button class="minor entry-rollback" data-id="${escapeHtml(row.entry_id)}">回滚版本</button>` : ""}<button class="minor entry-edit" data-id="${escapeHtml(row.entry_id)}">编辑</button></div></article>`).join("") : '<div class="empty">没有匹配的记忆条目.</div>';
}

function fillSettings(settings) {
  state.settings = settings || {};
  const form = $("settings-form");
  form.owner_identities.value = (settings.owner_identities || []).join("\n");
  form.capture_enabled.checked = Boolean(settings.capture_enabled);
  form.extractor_provider_id.value = settings.extractor_provider_id || "";
  form.reviewer_provider_id.value = settings.reviewer_provider_id || "";
  form.daily_request_budget.value = settings.daily_request_budget ?? 8;
  form.daily_input_token_budget.value = settings.daily_input_token_budget ?? 16000;
  form.injection_token_budget.value = settings.injection_token_budget ?? 800;
}

async function load() {
  const [dashboard, settings, entries, runs] = await Promise.all([api("GET", "state"), api("GET", "settings"), api("GET", "entries"), api("GET", "runs")]);
  state.entries = entries || [];
  fillSettings(settings);
  renderMetrics(dashboard);
  renderTargets(dashboard.targets || []);
  renderSchedules(dashboard.schedules || []);
  renderRuns(runs || []);
  renderEntries();
}

function openEntry(row = null) {
  const form = $("entry-form");
  form.reset();
  $("entry-dialog-title").textContent = row ? "编辑记忆条目" : "新建记忆条目";
  if (row) {
    form.entry_id.value = row.entry_id;
    form.scope_type.value = row.scope_type;
    form.kind.value = row.kind;
    form.scope_key.value = row.scope_key || "";
    form.status.value = row.status;
    form.triggers.value = triggersOf(row).join(", ");
    form.content.value = row.content;
    form.priority.value = row.priority || 0;
    form.visibility.value = row.visibility || "public";
  }
  $("entry-dialog").showModal();
}

async function openRollback(row) {
  const form = $("rollback-form");
  form.reset();
  form.entry_id.value = row.entry_id;
  const versions = await api("GET", `entries/${row.entry_id}/versions`);
  const candidates = (versions || []).filter((item) => Number(item.version) < Number(row.version));
  if (!candidates.length) throw new Error("当前条目没有可回滚的历史版本");
  form.version.innerHTML = candidates.map((item) => `<option value="${escapeHtml(item.version)}">v${escapeHtml(item.version)} · ${escapeHtml(item.actor_key)} · ${escapeHtml(formatTime(item.created_at))}</option>`).join("");
  $("rollback-entry-label").textContent = `${row.scope_type} · ${row.scope_key || "*"} · 当前 v${row.version}`;
  $("rollback-dialog").showModal();
}

$("target-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { const body = Object.fromEntries(new FormData(event.target)); body.enabled = true; await api("POST", "targets", body); event.target.reset(); showToast("学习目标已添加"); await load(); } catch (error) { showToast(String(error), true); }
});

$("schedule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const body = Object.fromEntries(new FormData(event.target));
    const scheduleId = body.schedule_id;
    delete body.schedule_id;
    body.enabled = scheduleId ? Boolean(state.schedules.find((row) => row.schedule_id === scheduleId)?.enabled) : true;
    await api("POST", scheduleId ? `schedules/${scheduleId}` : "schedules", body);
    event.target.reset();
    event.target.local_time.value = "03:00";
    event.target.timezone.value = "Asia/Shanghai";
    $("schedule-submit").textContent = "新增时间点";
    $("schedule-cancel").hidden = true;
    showToast(scheduleId ? "学习时间点已更新" : "学习时间点已添加");
    await load();
  } catch (error) { showToast(String(error), true); }
});

$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const form = event.target;
    const ownerIdentities = form.owner_identities.value.split(/[\n,]+/).map((item) => item.trim()).filter(Boolean);
    await api("POST", "settings", { owner_identities: ownerIdentities, capture_enabled: form.capture_enabled.checked, extractor_provider_id: form.extractor_provider_id.value.trim(), reviewer_provider_id: form.reviewer_provider_id.value.trim(), daily_request_budget: Number(form.daily_request_budget.value), daily_input_token_budget: Number(form.daily_input_token_budget.value), injection_token_budget: Number(form.injection_token_budget.value) });
    showToast("运行设置已保存");
    await load();
  } catch (error) { showToast(String(error), true); }
});

$("run-now").addEventListener("click", async () => {
  try { const result = await api("POST", "run-now", {}); showToast(`学习任务已提交, 处理 ${result.processed || 0} 个 anchor`); await load(); } catch (error) { showToast(String(error), true); }
});
$("refresh").addEventListener("click", () => load().then(() => showToast("已刷新")).catch((error) => showToast(String(error), true)));
$("entry-search").addEventListener("input", renderEntries);
$("entry-scope").addEventListener("change", renderEntries);
$("entry-status").addEventListener("change", renderEntries);
$("new-entry").addEventListener("click", () => openEntry());

$("targets").addEventListener("click", async (event) => {
  const button = event.target.closest(".target-toggle");
  if (!button) return;
  try { await api("POST", `targets/${button.dataset.id}`, { enabled: button.dataset.enabled === "1" }); showToast("学习目标状态已更新"); await load(); } catch (error) { showToast(String(error), true); }
});

$("schedules").addEventListener("click", async (event) => {
  const edit = event.target.closest(".schedule-edit");
  const toggle = event.target.closest(".schedule-toggle");
  const remove = event.target.closest(".schedule-delete");
  if (edit) {
    const row = state.schedules.find((item) => item.schedule_id === edit.dataset.id);
    if (!row) return;
    const form = $("schedule-form");
    form.schedule_id.value = row.schedule_id;
    form.local_time.value = row.local_time;
    form.timezone.value = row.timezone;
    $("schedule-submit").textContent = "保存时间点";
    $("schedule-cancel").hidden = false;
    form.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  try {
    if (toggle) await api("POST", `schedules/${toggle.dataset.id}`, { enabled: toggle.dataset.enabled === "1" });
    else if (remove) await api("POST", `schedules/${remove.dataset.id}`, { action: "delete" });
    else return;
    showToast(remove ? "学习时间点已删除" : "学习时间点状态已更新");
    await load();
  } catch (error) { showToast(String(error), true); }
});

$("schedule-cancel").addEventListener("click", () => {
  const form = $("schedule-form");
  form.reset();
  form.local_time.value = "03:00";
  form.timezone.value = "Asia/Shanghai";
  $("schedule-submit").textContent = "新增时间点";
  $("schedule-cancel").hidden = true;
});

$("entries").addEventListener("click", async (event) => {
  const edit = event.target.closest(".entry-edit");
  const rollback = event.target.closest(".entry-rollback");
  if (edit) openEntry(state.entries.find((row) => row.entry_id === edit.dataset.id));
  if (rollback) {
    const row = state.entries.find((item) => item.entry_id === rollback.dataset.id);
    if (row) openRollback(row).catch((error) => showToast(String(error), true));
  }
});

$("entry-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.submitter?.value === "cancel") { $("entry-dialog").close(); return; }
  try {
    const form = event.target;
    const data = Object.fromEntries(new FormData(form));
    const entryId = data.entry_id;
    const body = { scope_type: data.scope_type, scope_key: data.scope_key.trim(), kind: data.kind, status: data.status, triggers: data.triggers.split(",").map((item) => item.trim()).filter(Boolean), content: data.content.trim(), priority: Number(data.priority || 0), visibility: data.visibility, trust_level: "manual" };
    await api("POST", entryId ? `entries/${entryId}` : "entries", body);
    $("entry-dialog").close();
    showToast(entryId ? "记忆条目已更新" : "记忆条目已创建");
    await load();
  } catch (error) { showToast(String(error), true); }
});

$("rollback-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.submitter?.value === "cancel") { $("rollback-dialog").close(); return; }
  try {
    const form = event.target;
    const entryId = form.entry_id.value;
    const version = Number(form.version.value);
    if (!Number.isInteger(version) || version < 1) throw new Error("版本号必须是正整数");
    await api("POST", `entries/${entryId}/rollback`, { version });
    $("rollback-dialog").close();
    showToast(`已追加回滚版本 v${version}`);
    await load();
  } catch (error) { showToast(String(error), true); }
});

await bridge.ready();
load().catch((error) => showToast(String(error), true));
