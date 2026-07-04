/* PDF 双语翻译器 — 前端逻辑（v2：增量按需翻译）
 *
 * 流程：上传 → 后端秒级解析版式 → 立即进入预览；
 * 右栏译文按页向 /api/jobs/{id}/page/{n} 取单页 PDF，未译好显示占位并轮询；
 * 翻页时 POST /focus 让后端优先翻译浏览位置附近的页；
 * 点「下载」才 POST /finalize 全量翻译，按钮内显示进度，完成后自动下载。
 */
import * as pdfjsLib from "./vendor/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL("./vendor/pdf.worker.min.mjs", import.meta.url).href;

const $ = (id) => document.getElementById(id);

/* 取页重试指数退避：700ms 起，×1.5，上限 5000ms；页切换或取页成功后重置 */
const PAGE_RETRY_MIN_MS = 700;
const PAGE_RETRY_MAX_MS = 5000;
const PAGE_RETRY_FACTOR = 1.5;
/* postFocus 节流窗口（trailing：窗口内只保留最后一次上报的页码） */
const FOCUS_THROTTLE_MS = 150;

/* 仅用于标记「后端确证任务失败」（HTTP 409），与网络层瞬态异常区分 */
class PageTaskError extends Error {}

/* 服务端默认模型名（/api/health 返回），仅用于本机接口设置面板的占位符提示 */
let defaultModelName = "";

/* 本机翻译接口覆盖（v3 模型覆盖）：仅保存在浏览器 localStorage，随上传表单一次性提交，
 * 不经过服务端持久化，与「模型设置」弹层（PUT /api/config，改的是服务端 .env 默认值）
 * 是两套独立机制——本机覆盖只影响「本浏览器发起的下一次上传」。 */
const LOCAL_SETTINGS_KEY = "pdftr_settings";

function loadLocalSettings() {
  try {
    const raw = localStorage.getItem(LOCAL_SETTINGS_KEY);
    if (!raw) return { base_url: "", api_key: "", model: "" };
    const parsed = JSON.parse(raw);
    return {
      base_url: typeof parsed.base_url === "string" ? parsed.base_url : "",
      api_key: typeof parsed.api_key === "string" ? parsed.api_key : "",
      model: typeof parsed.model === "string" ? parsed.model : "",
    };
  } catch (_) {
    return { base_url: "", api_key: "", model: "" };
  }
}

function saveLocalSettings(settings) {
  localStorage.setItem(LOCAL_SETTINGS_KEY, JSON.stringify(settings));
}

function clearLocalSettings() {
  localStorage.removeItem(LOCAL_SETTINGS_KEY);
}

const els = {
  setupCard: $("setupCard"), progressCard: $("progressCard"), errorCard: $("errorCard"),
  previewSection: $("previewSection"), dropzone: $("dropzone"), fileInput: $("fileInput"),
  fileChip: $("fileChip"), fileName: $("fileName"), fileSize: $("fileSize"), clearFile: $("clearFile"),
  startBtn: $("startBtn"), thinkingToggle: $("thinkingToggle"), barFill: $("barFill"),
  progressMeta: $("progressMeta"), cancelWatch: $("cancelWatch"), errorMsg: $("errorMsg"),
  retryBtn: $("retryBtn"), prevPage: $("prevPage"), nextPage: $("nextPage"),
  pageInput: $("pageInput"), pageTotal: $("pageTotal"), downloadBtn: $("downloadBtn"),
  newTaskBtn: $("newTaskBtn"), viewer: $("viewer"), canvasL: $("canvasL"), canvasR: $("canvasR"),
  paneRTag: $("paneRTag"), paneRLoading: $("paneRLoading"),
  modelChip: $("modelChip"), modelName: $("modelName"), modeHint: $("modeHint"),
  useCache: $("useCache"),
  retransPageBtn: $("retransPageBtn"), retransAllBtn: $("retransAllBtn"),
  syncScroll: $("syncScroll"), syncScrollToggle: $("syncScrollToggle"),
  cacheChip: $("cacheChip"), cacheChipClose: $("cacheChipClose"),
  outlineToggle: $("outlineToggle"), outlinePanel: $("outlinePanel"), outlineTree: $("outlineTree"),
  zoomIn: $("zoomIn"), zoomOut: $("zoomOut"), zoomFitBtn: $("zoomFitBtn"), zoomLabel: $("zoomLabel"),
  settingsModal: $("settingsModal"), cfgBaseUrl: $("cfgBaseUrl"), cfgModel: $("cfgModel"),
  cfgApiKey: $("cfgApiKey"), cfgConcurrency: $("cfgConcurrency"), cfgThinking: $("cfgThinking"),
  cfgMsg: $("cfgMsg"), cfgSave: $("cfgSave"), cfgCancel: $("cfgCancel"),
  jobModelChip: $("jobModelChip"), jobModelName: $("jobModelName"),
  localSettingsBtn: $("localSettingsBtn"), localSettingsModal: $("localSettingsModal"),
  localCfgBaseUrl: $("localCfgBaseUrl"), localCfgApiKey: $("localCfgApiKey"),
  localCfgModel: $("localCfgModel"), localCfgMsg: $("localCfgMsg"),
  localCfgSave: $("localCfgSave"), localCfgCancel: $("localCfgCancel"), localCfgClear: $("localCfgClear"),
};

const state = {
  file: null,
  direction: "auto",
  mode: "translated",
  view: "compare",
  jobId: null,
  job: null,
  polling: false,
  originalDoc: null,
  pageDocs: new Map(),      // 页码(1-based) → 已译单页的 pdf.js 文档
  pageFetches: new Map(),   // 页码 → 取页中的 Promise（in-flight 去重，settle 后删除）
  page: 1,
  renderTasks: { L: null, R: null },     // 各 slot 当前活跃的 pdf.js RenderTask
  renderChain: { L: null, R: null },     // 各 slot 渲染串行链（保证同 canvas 不并发 render）
  renderSeq: 0,
  pageRetryTimer: null,     // 当前页未译好时的轮询定时器
  pageRetryDelay: PAGE_RETRY_MIN_MS,   // 下一次重试的退避延迟
  focusThrottleTimer: null,   // postFocus 节流定时器
  focusPendingPage: null,     // 节流窗口内待上报的最新页码
  finalizing: false,
  zoom: null,                 // null = 适应宽度（现行为）；数值 0.5~3.0 = 绝对比例（1.0 = 72dpi 原始大小）
  outlineLoaded: false,
  outlineCollapsed: true,     // 侧栏折叠状态（不持久化，随文档重新计算默认值）
  outlineDestCache: new WeakMap(),   // outline item → 已解析的 0-based 页码（null=解析失败），惰性填充
  outlineActiveEl: null,      // 当前高亮的目录条目 DOM 节点
  outlineClickSeq: 0,         // 目录点击单调世代：await 解析期间被更晚的点击取代则放弃导航
  syncScroll: true,           // 双栏滚动同步开关（仅 compare 视图生效），默认开
  syncingScroll: false,       // 「正在程序化同步」标志：置位期间跳过 other 的 scroll 回调，防止双向递归
};

const ZOOM_MIN = 0.5, ZOOM_MAX = 3, ZOOM_STEP = 0.25;

/* ---------- 工具 ---------- */

function show(section) {
  for (const s of [els.setupCard, els.progressCard, els.errorCard, els.previewSection]) {
    s.hidden = s !== section;
  }
}

function fmtSize(bytes) {
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

async function api(path, options) {
  const res = await fetch(`/api/jobs/${state.jobId}${path}`, options);
  return res;
}

/* ---------- 分段控件 ---------- */

document.querySelectorAll(".seg").forEach((seg) => {
  seg.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-v]");
    if (!btn) return;
    seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
    const name = seg.dataset.name;
    const v = btn.dataset.v;
    if (name === "direction") state.direction = v;
    if (name === "mode") {
      state.mode = v;
      els.modeHint.textContent = v === "interleaved"
        ? "影响下载的文件：每页原文后紧跟一页译文，适合双页对照阅读"
        : "影响下载的文件：仅含译文页，版式与原文一致";
    }
    if (name === "view") { state.view = v; applyView(); renderCurrent(); }
  });
});

/* ---------- 文件选择 ---------- */

function setFile(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
    alert("请选择 PDF 文件"); return;
  }
  if (file.size > 80 * 1024 * 1024) {
    alert("文件超过 80 MB 限制"); return;
  }
  state.file = file;
  els.fileName.textContent = file.name;
  els.fileSize.textContent = fmtSize(file.size);
  els.fileChip.hidden = false;
  els.startBtn.disabled = false;
}

els.dropzone.addEventListener("click", () => els.fileInput.click());
els.dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); els.fileInput.click(); }
});
els.fileInput.addEventListener("change", () => setFile(els.fileInput.files[0]));

["dragenter", "dragover"].forEach((ev) =>
  els.dropzone.addEventListener(ev, (e) => { e.preventDefault(); els.dropzone.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) =>
  els.dropzone.addEventListener(ev, (e) => { e.preventDefault(); els.dropzone.classList.remove("drag"); }));
els.dropzone.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));

els.clearFile.addEventListener("click", () => {
  state.file = null;
  els.fileInput.value = "";
  els.fileChip.hidden = true;
  els.startBtn.disabled = true;
});

/* ---------- 提交任务 ---------- */

els.startBtn.addEventListener("click", async () => {
  if (!state.file) return;
  els.startBtn.disabled = true;
  els.startBtn.textContent = "上传中…";
  try {
    const fd = new FormData();
    fd.append("file", state.file);
    fd.append("mode", state.mode);
    fd.append("direction", state.direction);
    fd.append("thinking", els.thinkingToggle.checked ? "true" : "false");
    fd.append("use_cache", els.useCache.checked ? "true" : "false");
    // 本机翻译接口覆盖：非空字段才随表单提交（空 = 沿用服务端默认）。
    const localSettings = loadLocalSettings();
    if (localSettings.base_url) fd.append("base_url", localSettings.base_url);
    if (localSettings.api_key) fd.append("api_key", localSettings.api_key);
    if (localSettings.model) fd.append("model", localSettings.model);
    const res = await fetch("/api/translate", { method: "POST", body: fd });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `上传失败 (HTTP ${res.status})`);
    }
    const { job_id } = await res.json();
    state.jobId = job_id;
    history.replaceState(null, "", `?job=${job_id}`);
    show(els.progressCard);
    startPolling();
  } catch (err) {
    showError(String(err.message || err));
  } finally {
    els.startBtn.disabled = false;
    els.startBtn.textContent = "开始翻译";
  }
});

/* ---------- 进入预览前的短暂等待（解析版式） ---------- */

const STAGE_LABEL = {
  queued: "正在排队…",
  extracting: "正在解析 PDF 版式…",
  serving: "版式解析完成，正在进入预览…",
};

function updateProgressUI(job) {
  const stageOrder = ["extracting", "serving"];
  document.querySelectorAll(".step").forEach((st) => {
    const s = st.dataset.s;
    st.classList.remove("active", "done");
    if (s === job.status) st.classList.add("active");
    else if (stageOrder.indexOf(s) < stageOrder.indexOf(job.status)) st.classList.add("done");
  });
  els.barFill.style.width = job.status === "extracting" ? "35%" : "80%";
  let meta = STAGE_LABEL[job.status] || "";
  if (job.status === "serving" && job.pages_done > 0) {
    meta = `已命中翻译缓存（${job.pages_done}/${job.page_count} 页已有译文），正在进入预览…`;
  } else if (job.page_count) {
    meta += `　共 ${job.page_count} 页`;
  }
  els.progressMeta.textContent = meta;
}

async function startPolling() {
  state.polling = true;
  const jobId = state.jobId;   // 捕获：防止轮询途中用户开新任务后仍以旧响应驱动 UI
  let failures = 0;
  while (state.polling) {
    try {
      const res = await api("");
      // await 期间用户可能已点「返回」或「新任务」——旧响应一律丢弃
      if (!state.polling || state.jobId !== jobId) return;
      if (res.status === 404) throw new Error("任务不存在");
      const job = await res.json();
      state.job = job;
      failures = 0;
      updateProgressUI(job);
      if (["serving", "finalizing", "rendering", "done"].includes(job.status)) {
        state.polling = false;
        await openPreview();
        return;
      }
      if (job.status === "error") { state.polling = false; showError(job.error || "未知错误"); return; }
    } catch (err) {
      if (++failures > 8) { state.polling = false; showError("与服务器失去连接：" + err.message); return; }
    }
    await new Promise((r) => setTimeout(r, 600));
  }
}

els.cancelWatch.addEventListener("click", () => { state.polling = false; show(els.setupCard); });

function showError(msg) {
  state.polling = false;
  els.errorMsg.textContent = msg;
  show(els.errorCard);
}
els.retryBtn.addEventListener("click", () => show(els.setupCard));

/* ---------- 预览（增量：右栏按页取译文） ---------- */

async function openPreview() {
  try {
    state.originalDoc = await pdfjsLib.getDocument({
      url: `/api/jobs/${state.jobId}/file/original`,
    }).promise;
    state.page = 1;
    state.zoom = null;
    state.pageDocs.clear();
    state.outlineLoaded = false;
    show(els.previewSection);
    maybeShowCacheChip();   // job.cache_hit 时给出一次性「已载入历史译文」提示
    updateJobModelChip();   // 显示 job.model（可能来自本机接口覆盖）
    applyView();
    syncZoomLabel();
    postFocus(0);
    loadOutline();   // 异步加载目录，不阻塞首屏
    await renderCurrent();
    syncDownloadBtn();
  } catch (err) {
    showError("预览加载失败：" + (err.message || err));
  }
}

/* ---------- 命中历史缓存的一次性提示 chip ---------- */

function maybeShowCacheChip() {
  els.cacheChip.hidden = !(state.job && state.job.cache_hit);
}
els.cacheChipClose.addEventListener("click", () => { els.cacheChip.hidden = true; });

/* ---------- 本任务实际使用的模型 chip（job.model，可能来自本机接口覆盖） ---------- */

function updateJobModelChip() {
  if (state.job && state.job.model) {
    els.jobModelName.textContent = state.job.model;
    els.jobModelChip.hidden = false;
  } else {
    els.jobModelChip.hidden = true;
  }
}

/* ---------- 重译（页级 / 全量，忽略缓存强制重走 LLM） ---------- *
 * 成功后使相应页缓存失效并立即 renderCurrent()（会拿到 202 → 占位轮询）；
 * 重译会把已 done 的任务在后端拨回 serving，故本地也把 job.status 复位为 serving，
 * 让下载按钮重新走 finalize 流程。
 *
 * v3.1 可用态：finalizing/rendering 期间后端对 /retranslate 返回 409（数据竞态保护，
 * 正确行为），但用户点了才弹 alert 体验为"报错"——改为在这两个状态下禁用按钮
 * （syncRetransButtons 统一刷新），且即便时序竞态仍收到 409 也只做温和 inline 提示，
 * 不 alert、不打断浏览；其它错误码（404/400 等）仍 alert。 */
const RETRANS_BUSY_TITLE = "正在生成完整译本，暂不可重译";
const RETRANS_TITLES = {
  page: "仅重新翻译当前页（忽略缓存）",
  all: "重置整篇译文：清空全部页并忽略缓存重新翻译",
};
// 原始按钮文案常量捕获（而非临时读 btn.textContent）：避免「稍后再试」的临时文案
// 在其 800ms 复原计时器触发前被用户再次点击时，被误当作"原始标签"记录下来。
const RETRANS_LABELS = {
  page: els.retransPageBtn.textContent,
  all: els.retransAllBtn.textContent,
};

/* 释放 pdf.js 文档资源的兼容辅助：本项目 vendored 的 pdf.js 版本中 PDFDocumentProxy
 * **没有** destroy() 方法（销毁入口在 doc.loadingTask.destroy()），直接调 doc.destroy()
 * 会抛 "doc.destroy is not a function"（重译路径曾因此弹「重译失败」）。此处兼容两种
 * API 形态并吞掉销毁期异常——销毁失败最多泄漏一点内存，绝不该打断用户操作。 */
function destroyPdfDoc(doc) {
  if (!doc) return;
  const warn = (err) => console.warn("释放 pdf.js 文档失败（忽略）：", err);
  try {
    // destroy() 是 async：同步 try/catch 接不住其 rejection（会成为未捕获异常），
    // 必须在返回的 Promise 上链 .catch 吞掉。
    let p = null;
    if (typeof doc.destroy === "function") p = doc.destroy();
    else if (doc.loadingTask && typeof doc.loadingTask.destroy === "function") {
      p = doc.loadingTask.destroy();
    }
    if (p && typeof p.catch === "function") p.catch(warn);
  } catch (err) {
    warn(err);
  }
}

/* 根据 state.job.status 统一刷新两个重译按钮的可用态 + tooltip；在所有状态变化点
 * （finalize 轮询循环、syncDownloadBtn、openPreview 经 syncDownloadBtn 间接触发、
 * renderCurrent 结束）调用，保证按钮状态与后端 job.status 及时同步。 */
function syncRetransButtons() {
  const busy = !!(state.job && (state.job.status === "finalizing" || state.job.status === "rendering"));
  els.retransPageBtn.disabled = busy;
  els.retransAllBtn.disabled = busy;
  els.retransPageBtn.title = busy ? RETRANS_BUSY_TITLE : RETRANS_TITLES.page;
  els.retransAllBtn.title = busy ? RETRANS_BUSY_TITLE : RETRANS_TITLES.all;
}

async function retranslate(scope) {
  if (!state.jobId) return;
  if (scope === "all" && !confirm("重置整篇译文：将清空全部页并忽略缓存重新翻译，确定继续？")) return;
  const jobId = state.jobId;
  // 发请求前就把目标页码捕获成常量（1-based，与 pageDocs 的 key 一致）：
  // 若成功回调里重新读 state.page，翻页期间发生的重译请求会失效错页的缓存。
  const targetPage = scope === "page" ? state.page : null;
  const btn = scope === "page" ? els.retransPageBtn : els.retransAllBtn;
  const label = RETRANS_LABELS[scope];
  btn.disabled = true;
  try {
    const body = scope === "page"
      ? { scope: "page", page: targetPage - 1 }
      : { scope: "all" };
    const res = await fetch(`/api/jobs/${jobId}/retranslate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      const httpErr = new Error(b.detail || `HTTP ${res.status}`);
      httpErr.status = res.status;
      throw httpErr;
    }
    if (state.jobId !== jobId) return;   // 期间已切换任务，结果作废
    if (scope === "page") {
      const doc = state.pageDocs.get(targetPage);
      destroyPdfDoc(doc);
      state.pageDocs.delete(targetPage);
      state.pageFetches.delete(targetPage);   // in-flight 去重项清理（旧 Promise 不再复用）
    } else {
      for (const d of state.pageDocs.values()) destroyPdfDoc(d);
      state.pageDocs.clear();
      state.pageFetches.clear();
    }
    state.pageRetryDelay = PAGE_RETRY_MIN_MS;
    // 重译把 done 任务拨回 serving：本地复位，使「下载」重新走 finalize。
    if (state.job) state.job.status = "serving";
    btn.textContent = label;
    renderCurrent();
  } catch (err) {
    if (err && err.status === 409) {
      // 时序竞态（按钮理应已禁用，但仍收到 409）：温和 inline 提示，不 alert、不打断浏览。
      btn.textContent = "稍后再试";
      setTimeout(() => {
        if (btn.textContent === "稍后再试") btn.textContent = label;
      }, 800);
    } else {
      btn.textContent = label;
      alert("重译失败：" + (err.message || err));
    }
  } finally {
    btn.disabled = false;
    syncRetransButtons();   // 按当前 job.status 重新决定是否应保持禁用
  }
}

els.retransPageBtn.addEventListener("click", () => retranslate("page"));
els.retransAllBtn.addEventListener("click", () => retranslate("all"));

/* ---------- 目录侧栏（PDF outline/书签） ----------
 * 递归渲染树：每层缩进 14px，非叶节点带 ▸/▾ 折叠开关，默认展开第一层、折叠更深层级。
 * dest 解析（字符串命名目标 → getDestination → getPageIndex）为惰性操作：只在用户点击
 * 该条目时才解析，解析结果按 outline item 对象缓存在 state.outlineDestCache（WeakMap），
 * 避免长目录一次性打开时对 pdf.js worker 发起成百上千次往返。解析失败静默忽略并 console.warn。 */

function setOutlineCollapsed(collapsed) {
  state.outlineCollapsed = collapsed;
  els.outlinePanel.hidden = collapsed;
  els.outlineToggle.classList.toggle("on", !collapsed);
}

async function resolveOutlineDest(item) {
  if (state.outlineDestCache.has(item)) return state.outlineDestCache.get(item);
  try {
    let dest = item.dest;
    if (typeof dest === "string") dest = await state.originalDoc.getDestination(dest);
    if (!Array.isArray(dest) || !dest.length) throw new Error("目录条目没有可跳转的目标");
    const pageIndex = await state.originalDoc.getPageIndex(dest[0]);
    state.outlineDestCache.set(item, pageIndex);
    return pageIndex;
  } catch (err) {
    console.warn(`目录条目「${item.title || ""}」解析失败：`, err);
    state.outlineDestCache.set(item, null);   // 缓存失败结果，避免重复点击反复报错
    return null;
  }
}

async function handleOutlineClick(item, labelEl) {
  // 单调世代 + jobId 捕获：await 解析期间可能被更晚的点击取代（快慢请求乱序 resolve），
  // 或任务已切换（旧文档 dest 引用查新文档页表可能命中错页）——两种情况都放弃导航。
  const seq = ++state.outlineClickSeq;
  const jobId = state.jobId;
  if (state.outlineActiveEl) state.outlineActiveEl.classList.remove("active");
  labelEl.classList.add("active");
  state.outlineActiveEl = labelEl;
  const pageIndex = await resolveOutlineDest(item);
  if (seq !== state.outlineClickSeq || state.jobId !== jobId) return;   // 已被取代或任务已切换
  if (pageIndex == null) return;   // 解析失败：静默忽略（已 console.warn）
  gotoPage(pageIndex + 1);
}

/* 递归渲染一层目录节点；depth 从 0 开始。非叶节点的子树容器默认展开当 depth===0
 * （即第一层子节点可见），更深层级默认折叠，符合 Acrobat 式「默认展开一级」。 */
function renderOutlineLevel(items, depth) {
  const ul = document.createElement("ul");
  ul.className = "outline-list";
  for (const item of items) {
    const li = document.createElement("li");
    li.className = "outline-node";

    const row = document.createElement("div");
    row.className = "outline-row";
    row.style.paddingLeft = depth * 14 + "px";

    const hasChildren = Array.isArray(item.items) && item.items.length > 0;
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "outline-toggle" + (hasChildren ? "" : " leaf");
    toggle.tabIndex = hasChildren ? 0 : -1;
    toggle.setAttribute("aria-label", hasChildren ? "展开/折叠" : "");
    if (hasChildren) toggle.textContent = depth === 0 ? "▾" : "▸";
    row.appendChild(toggle);

    const label = document.createElement("button");
    label.type = "button";
    label.className = "outline-item";
    label.textContent = item.title || "(未命名书签)";
    label.title = item.title || "";
    row.appendChild(label);
    li.appendChild(row);

    if (hasChildren) {
      const childUl = renderOutlineLevel(item.items, depth + 1);
      childUl.hidden = depth !== 0;
      li.appendChild(childUl);
      toggle.addEventListener("click", (e) => {
        e.stopPropagation();
        childUl.hidden = !childUl.hidden;
        toggle.textContent = childUl.hidden ? "▸" : "▾";
      });
    }

    label.addEventListener("click", () => handleOutlineClick(item, label));
    ul.appendChild(li);
  }
  return ul;
}

async function loadOutline() {
  const jobId = state.jobId;
  els.outlineTree.innerHTML = "";
  state.outlineDestCache = new WeakMap();
  state.outlineActiveEl = null;
  try {
    const outline = await state.originalDoc.getOutline();
    if (state.jobId !== jobId) return;   // 取回时任务已切换，结果作废
    state.outlineLoaded = true;
    if (!outline || !outline.length) {
      els.outlineTree.innerHTML = '<div class="outline-empty">本文档没有目录</div>';
      setOutlineCollapsed(true);
      return;
    }
    els.outlineTree.appendChild(renderOutlineLevel(outline, 0));
    // 窄屏（<900px）默认收起，避免把双栏预览挤破；否则默认展开供用户直接使用
    setOutlineCollapsed(window.innerWidth < 900);
  } catch (err) {
    if (state.jobId !== jobId) return;
    console.warn("目录加载失败", err);
    els.outlineTree.innerHTML = '<div class="outline-empty">目录加载失败</div>';
    setOutlineCollapsed(true);
  }
}

els.outlineToggle.addEventListener("click", () => {
  // 侧栏显隐改变视图宽度，重新渲染适配
  setOutlineCollapsed(!state.outlineCollapsed);
  renderCurrent();
});

function totalPages() {
  return state.originalDoc ? state.originalDoc.numPages : 1;
}

function postFocus(pageIndex0) {
  // 火后不理：告诉后端当前浏览位置，用于优先翻译附近页；150ms trailing 节流，
  // 窗口内只保留最新页码，避免连续翻页时请求堆积。
  state.focusPendingPage = pageIndex0;
  if (state.focusThrottleTimer) return;
  const jobId = state.jobId;   // 捕获：定时器触发时任务可能已切换
  state.focusThrottleTimer = setTimeout(() => {
    state.focusThrottleTimer = null;
    if (state.jobId !== jobId) return;
    const page = state.focusPendingPage;
    fetch(`/api/jobs/${jobId}/focus`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page }),
    }).catch(() => {});
  }, FOCUS_THROTTLE_MS);
}

/* 页缓存上限：超出后按插入序淘汰最旧（简易 LRU），防止大文档长会话内存无限增长；
 * 当前显示页与正在取页中的页永不驱逐，若剩余全是这些页则本次跳过驱逐。 */
const MAX_PAGE_DOCS = 40;

function evictPageDocIfNeeded() {
  if (state.pageDocs.size < MAX_PAGE_DOCS) return;
  const keep = new Set([state.page, ...state.pageFetches.keys()]);
  for (const key of state.pageDocs.keys()) {
    if (keep.has(key)) continue;
    destroyPdfDoc(state.pageDocs.get(key));
    state.pageDocs.delete(key);
    return;
  }
  // 剩余全是当前页/取页中的页，本次跳过驱逐
}

/* 实际发出第 p 页的取页请求。取页错误语义：
 * - HTTP 409（后端确证任务失败）→ 抛 PageTaskError，由调用方弹错误卡片；
 * - 其它任何异常（fetch 拒绝、arrayBuffer/getDocument 失败等瞬态问题）
 *   → console.warn 后按「未译好」返回 null，交由占位 + 重试路径处理。 */
async function fetchTranslatedPageDoc(p) {
  const jobId = state.jobId;   // 捕获：请求返回时若已切换任务，结果作废
  try {
    const res = await api(`/page/${p - 1}`);
    if (state.jobId !== jobId) return null;
    if (res.status === 409) {
      const body = await res.json().catch(() => ({}));
      throw new PageTaskError(body.error || "任务已出错");
    }
    if (res.status !== 200) return null;   // 202 = 翻译中
    const data = await res.arrayBuffer();
    const doc = await pdfjsLib.getDocument({ data }).promise;
    if (state.jobId !== jobId) { destroyPdfDoc(doc); return null; }  // 跨任务串页防护
    evictPageDocIfNeeded();
    state.pageDocs.set(p, doc);
    return doc;
  } catch (err) {
    if (err instanceof PageTaskError) throw err;
    console.warn(`第 ${p} 页取页/解析失败，按未译好处理：`, err);
    return null;
  }
}

/* 取第 p 页（1-based）的已译单页文档；未译好返回 null；任务已出错抛 PageTaskError。
 * 同页并发请求经 state.pageFetches 去重，共享同一 Promise，settle 后从 Map 删除。 */
function getTranslatedPageDoc(p) {
  if (state.pageDocs.has(p)) return Promise.resolve(state.pageDocs.get(p));
  let promise = state.pageFetches.get(p);
  if (!promise) {
    promise = fetchTranslatedPageDoc(p).finally(() => {
      if (state.pageFetches.get(p) === promise) state.pageFetches.delete(p);
    });
    state.pageFetches.set(p, promise);
  }
  return promise;
}

function applyView() {
  els.viewer.classList.toggle("compare", state.view === "compare");
  els.viewer.classList.toggle("single", state.view !== "compare");
  els.paneRTag.textContent = "译文";
  els.syncScrollToggle.hidden = state.view !== "compare";   // 单栏视图不涉及双栏同步，隐藏开关
  state.page = Math.min(state.page, totalPages());
  syncPager();
}

function syncPager() {
  // 用户正在输入页号时不覆盖输入框：未译页的轮询重试会周期性触发 renderCurrent →
  // syncPager，若无条件重置 value，用户输入到一半就被清掉，「输入页号跳转」形同虚设。
  if (document.activeElement !== els.pageInput) els.pageInput.value = state.page;
  els.pageInput.max = totalPages();
  els.pageTotal.textContent = totalPages();
  els.prevPage.disabled = state.page <= 1;
  els.nextPage.disabled = state.page >= totalPages();
}

/* per-slot 串行渲染：cancel 旧任务后必须等其 promise 真正 settle（吞 RenderingCancelledException
 * 与任何异常）才能开始新渲染，否则会撞上 pdf.js 的
 * "Cannot use the same canvas during multiple render() operations"。
 * 用 state.renderChain[slot] 把同一 slot 的历次调用串成链，保证严格顺序执行。 */
function renderPageTo(doc, pageNum, canvas, slot, seq) {
  const activeTask = state.renderTasks[slot];
  if (activeTask) { try { activeTask.cancel(); } catch (_) {} }
  const prevChain = state.renderChain[slot] || Promise.resolve();
  const chain = prevChain
    .catch(() => {})   // 吞掉前一次调用遗留的任何异常，不阻塞后续渲染
    .then(() => renderPageToOnce(doc, pageNum, canvas, slot, seq));
  state.renderChain[slot] = chain;
  return chain;
}

async function renderPageToOnce(doc, pageNum, canvas, slot, seq) {
  const page = await doc.getPage(pageNum);
  if (seq !== state.renderSeq) return;
  const wrap = canvas.parentElement;
  const base = page.getViewport({ scale: 1 });
  const ratio = base.height / base.width;   // 等比：CSS 高度由宽度推导，不依赖 CSS height:auto
  let cssWidth, scale;
  if (state.zoom == null) {
    // 适应宽度：随容器宽度自适应（现行为）
    cssWidth = Math.max(wrap.clientWidth - 2, 240);
    scale = cssWidth / base.width;
  } else {
    // 固定比例：scale 直接取 state.zoom（pdf.js 1.0 = 72dpi 原始大小），不受容器宽度影响
    scale = state.zoom;
    cssWidth = base.width * scale;
  }
  const cssHeight = cssWidth * ratio;
  const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
  const viewport = page.getViewport({ scale: scale * dpr });

  canvas.width = Math.floor(viewport.width);
  canvas.height = Math.floor(viewport.height);
  canvas.style.width = cssWidth + "px";
  canvas.style.height = cssHeight + "px";   // 显式等比高度：防止 flex 交叉轴拉伸压扁/拉长画布

  const ctx = canvas.getContext("2d");
  const task = page.render({ canvas, canvasContext: ctx, viewport });
  state.renderTasks[slot] = task;
  try {
    await task.promise;
  } catch (e) {
    if (e && e.name === "RenderingCancelledException") return;
    throw e;
  } finally {
    if (state.renderTasks[slot] === task) state.renderTasks[slot] = null;
  }
}

/* 右栏：渲染第 p 页译文；未译好时显示占位并按指数退避重试；任务确证出错（HTTP 409）则终止并提示 */
async function renderTranslatedPane(p, seq) {
  let doc;
  try {
    doc = await getTranslatedPageDoc(p);
  } catch (err) {
    // 仅 PageTaskError（HTTP 409）会走到这里；其它瞬态异常已在 fetchTranslatedPageDoc 内部
    // console.warn 并按「未译好」处理，不会导致连续翻页被误判为任务出错弹回首页。
    clearTimeout(state.pageRetryTimer);
    state.pageFetches.clear();
    showError("翻译任务出错：" + (err.message || err));
    return;
  }
  if (seq !== state.renderSeq) return;
  if (doc) {
    state.pageRetryDelay = PAGE_RETRY_MIN_MS;   // 取页成功，重置退避
    els.paneRLoading.hidden = true;
    await renderPageTo(doc, 1, els.canvasR, "R", seq);
    return;
  }
  // 未译好：清空画布 + 占位 + 指数退避轮询重试（700ms 起，×1.5，上限 5000ms）
  els.canvasR.width = els.canvasL.width || 600;
  els.canvasR.height = els.canvasL.height || 800;
  els.canvasR.style.width = els.canvasL.style.width || "";
  els.canvasR.getContext("2d").clearRect(0, 0, els.canvasR.width, els.canvasR.height);
  els.paneRLoading.hidden = false;
  clearTimeout(state.pageRetryTimer);
  const delay = state.pageRetryDelay;
  state.pageRetryDelay = Math.min(delay * PAGE_RETRY_FACTOR, PAGE_RETRY_MAX_MS);
  state.pageRetryTimer = setTimeout(() => {
    if (state.page === p && !els.previewSection.hidden) renderCurrent();
  }, delay);
}

async function renderCurrent() {
  if (!state.originalDoc) return;
  const seq = ++state.renderSeq;
  const p = state.page;
  try {
    if (state.view === "compare") {
      await Promise.all([
        renderPageTo(state.originalDoc, p, els.canvasL, "L", seq),
        renderTranslatedPane(p, seq),
      ]);
    } else {
      await renderTranslatedPane(p, seq);
    }
  } catch (err) {
    console.error("渲染失败", err);
  }
  syncPager();
  syncRetransButtons();
  // 渲染完成后保持双栏对齐（翻页/缩放/重试完成会替换 canvas，滚动范围随之变化，
  // 若不重新按比例对齐，两栏位置会渐渐漂移）。syncScrollTo 内部自带开关与视图门控。
  if (seq === state.renderSeq) {
    syncScrollTo(els.canvasL.parentElement, els.canvasR.parentElement);
  }
}

function gotoPage(p) {
  const t = totalPages();
  const next = Math.min(Math.max(1, p), t);
  if (next !== state.page) state.pageRetryDelay = PAGE_RETRY_MIN_MS;   // 页切换重置退避
  state.page = next;
  postFocus(state.page - 1);
  syncPager();
  renderCurrent();
}

els.prevPage.addEventListener("click", () => gotoPage(state.page - 1));
els.nextPage.addEventListener("click", () => gotoPage(state.page + 1));
els.pageInput.addEventListener("change", () => gotoPage(parseInt(els.pageInput.value || "1", 10)));
// 回车立即跳转并失焦：失焦既给出「已提交」的明确反馈，也让 syncPager 恢复对输入框的同步。
els.pageInput.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  gotoPage(parseInt(els.pageInput.value || "1", 10));
  els.pageInput.blur();
});
document.addEventListener("keydown", (e) => {
  if (els.previewSection.hidden || e.target === els.pageInput) return;
  if (e.key === "ArrowLeft") gotoPage(state.page - 1);
  if (e.key === "ArrowRight") gotoPage(state.page + 1);
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  if (els.previewSection.hidden) return;
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderCurrent, 250);
});

/* ---------- 缩放 ---------- *
 * state.zoom：null = 适应宽度（工具栏「适宽」按钮/Ctrl+0）；0.5~3.0 = 绝对比例，步进 0.25。
 * 百分比按钮显示当前比例（适应宽度时显示「适宽」），点击重置为 100%（与「适宽」是两个
 * 独立控件：前者固定回 1.0 倍原始大小，后者回到随容器自适应）。 */

function syncZoomLabel() {
  els.zoomLabel.textContent = state.zoom == null ? "适宽" : Math.round(state.zoom * 100) + "%";
  els.zoomOut.disabled = state.zoom !== null && state.zoom <= ZOOM_MIN;
  els.zoomIn.disabled = state.zoom !== null && state.zoom >= ZOOM_MAX;
  els.zoomFitBtn.classList.toggle("on", state.zoom === null);
  els.viewer.classList.toggle("zoomed", state.zoom !== null);
}

function setZoom(z) {
  const next = z === null ? null : Math.min(Math.max(z, ZOOM_MIN), ZOOM_MAX);
  if (next === state.zoom) return;
  state.zoom = next;
  syncZoomLabel();
  renderCurrent();
}

/* +/− 相对当前比例步进；若当前是「适应宽度」，以 1.0（100%）为步进基准 */
function zoomBy(delta) {
  const base = state.zoom == null ? 1 : state.zoom;
  setZoom(Math.min(Math.max(base + delta, ZOOM_MIN), ZOOM_MAX));
}

els.zoomIn.addEventListener("click", () => zoomBy(ZOOM_STEP));
els.zoomOut.addEventListener("click", () => zoomBy(-ZOOM_STEP));
els.zoomLabel.addEventListener("click", () => setZoom(1));
els.zoomFitBtn.addEventListener("click", () => setZoom(null));

document.addEventListener("keydown", (e) => {
  if (els.previewSection.hidden) return;
  if (!(e.ctrlKey || e.metaKey)) return;   // 必须 Ctrl/⌘ 修饰，避免拦截普通输入的 +/-/0
  if (e.key === "+" || e.key === "=") { e.preventDefault(); zoomBy(ZOOM_STEP); }
  else if (e.key === "-") { e.preventDefault(); zoomBy(-ZOOM_STEP); }
  else if (e.key === "0") { e.preventDefault(); setZoom(null); }
});

/* ---------- 缩放后拖拽平移（pan） ---------- *
 * 仅当画布因缩放溢出 .canvas-wrap 时才激活：mousedown（绑在 wrap 上，只在按下画布区域时
 * 启动，不影响工具栏/目录点击）记起点 + 起始 scroll；拖动期间把 mousemove/mouseup 绑到
 * **window**，使光标移出 wrap 边界（缩放后每栏仅半个视口，朝边缘拖很容易越界）仍能连续
 * 平移，松开才结束、并解绑 window 监听避免泄漏。未监听 wheel/touch，滚轮与触控滚动保留。 */
function setupPan(wrap) {
  let dragging = false, x0 = 0, y0 = 0, startLeft = 0, startTop = 0;
  const onMove = (e) => {
    if (!dragging) return;
    wrap.scrollLeft = startLeft - (e.clientX - x0);
    wrap.scrollTop = startTop - (e.clientY - y0);
  };
  const stopDrag = () => {
    if (!dragging) return;
    dragging = false;
    wrap.classList.remove("grabbing");
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", stopDrag);
  };
  wrap.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;               // 仅左键
    if (state.zoom == null) return;            // 适宽时无溢出，不激活
    const canPanX = wrap.scrollWidth > wrap.clientWidth;
    const canPanY = wrap.scrollHeight > wrap.clientHeight;
    if (!canPanX && !canPanY) return;
    dragging = true;
    x0 = e.clientX; y0 = e.clientY;
    startLeft = wrap.scrollLeft; startTop = wrap.scrollTop;
    wrap.classList.add("grabbing");
    // 拖动跟随绑到 window：光标移出 .canvas-wrap 边界也能连续平移，松开才结束。
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", stopDrag);
    e.preventDefault();   // 避免拖动时触发文本选择/原生拖拽残影
  });
}

setupPan(els.canvasL.parentElement);
setupPan(els.canvasR.parentElement);

/* ---------- 对照双栏滚动同步 ---------- *
 * 开关默认开、仅 compare 视图显示（applyView 控制隐藏）；滚动/拖动一栏时另一栏同步到
 * 相同归一化位置（按 scrollLeft/scrollWidth、scrollTop/scrollHeight 比例映射，两栏尺寸
 * 或缩放不同也不会错位）。state.syncingScroll 标志包住对 other 的赋值，避免其 scroll
 * 事件反向再次触发同步造成递归抖动；用 requestAnimationFrame 延后清除标志，覆盖浏览器
 * 异步派发 scroll 事件的那一帧。 */
function syncScrollTo(from, to) {
  if (!state.syncScroll || state.view !== "compare") return;
  if (state.syncingScroll) return;
  state.syncingScroll = true;
  const fromXRange = from.scrollWidth - from.clientWidth || 1;
  const fromYRange = from.scrollHeight - from.clientHeight || 1;
  const toXRange = to.scrollWidth - to.clientWidth || 1;
  const toYRange = to.scrollHeight - to.clientHeight || 1;
  to.scrollLeft = (from.scrollLeft / fromXRange) * toXRange;
  to.scrollTop = (from.scrollTop / fromYRange) * toYRange;
  requestAnimationFrame(() => { state.syncingScroll = false; });
}

function setupScrollSync(wrapA, wrapB) {
  wrapA.addEventListener("scroll", () => syncScrollTo(wrapA, wrapB));
  wrapB.addEventListener("scroll", () => syncScrollTo(wrapB, wrapA));
}

setupScrollSync(els.canvasL.parentElement, els.canvasR.parentElement);

els.syncScroll.addEventListener("change", () => {
  state.syncScroll = els.syncScroll.checked;
  // 勾选瞬间立即把译文栏对齐到原文栏当前位置（而非等到下一次滚动才开始同步）。
  if (state.syncScroll) syncScrollTo(els.canvasL.parentElement, els.canvasR.parentElement);
});

/* ---------- 下载：finalize 全量翻译 + 按钮内进度 ---------- */

function syncDownloadBtn() {
  syncRetransButtons();   // job.status 变化的统一落点之一（也覆盖 openPreview 间接调用）
  if (state.job && state.job.status === "done") {
    els.downloadBtn.textContent = "⬇ 下载 PDF";
    els.downloadBtn.classList.remove("busy");
    els.downloadBtn.disabled = false;
  }
}

els.downloadBtn.addEventListener("click", async () => {
  if (state.finalizing) return;
  // 已全量完成：直接下载
  if (state.job && state.job.status === "done") {
    window.location.href = `/api/jobs/${state.jobId}/download`;
    return;
  }
  // 触发全量翻译并跟踪进度。捕获 jobId：轮询期间用户点「新任务」后
  // state.jobId 会被清空/更换，旧任务的轮询必须立即失效，不能打到 /api/jobs/null。
  const jobId = state.jobId;
  const resetBtn = () => {
    els.downloadBtn.textContent = "⬇ 下载 PDF";
    els.downloadBtn.classList.remove("busy");
  };
  state.finalizing = true;
  els.downloadBtn.classList.add("busy");
  els.downloadBtn.textContent = "正在生成完整译本…";
  try {
    const res = await fetch(`/api/jobs/${jobId}/finalize`, { method: "POST" });
    if (!res.ok) throw new Error(`finalize 失败 (HTTP ${res.status})`);
    // finalize 成功即代表后端已进入 finalizing：本地先行乐观置位，避免首次轮询
    // （900ms 后）到来前的短暂窗口内重译按钮仍显示可用。
    if (state.job) { state.job.status = "finalizing"; syncRetransButtons(); }
    while (true) {
      await new Promise((r) => setTimeout(r, 900));
      if (state.jobId !== jobId) { resetBtn(); return; }  // 用户已切换任务，静默退出
      const jr = await fetch(`/api/jobs/${jobId}`);
      const job = await jr.json();
      if (state.jobId !== jobId) { resetBtn(); return; }
      state.job = job;
      syncRetransButtons();
      if (job.status === "error") throw new Error(job.error || "翻译失败");
      if (job.status === "done") break;
      if (job.status === "rendering") {
        els.downloadBtn.textContent = "正在合成 PDF…";
      } else {
        const pct = job.page_count ? Math.round((job.pages_done / job.page_count) * 100) : 0;
        els.downloadBtn.textContent = `生成完整译本 ${pct}%（${job.pages_done}/${job.page_count} 页）`;
      }
    }
    resetBtn();
    window.location.href = `/api/jobs/${jobId}/download`;
  } catch (err) {
    resetBtn();
    if (state.jobId === jobId) {
      // 轮询中途异常（瞬态网络 / 非法 JSON）时，state.job.status 可能停在第 905 行乐观
      // 置位的 "finalizing"，若不校正，下面 finally 的 syncRetransButtons 会让重译按钮
      // 永久卡在禁用态。这里再拉一次真实状态回填；若连这次也失败，则把乐观置位的
      // finalizing/rendering 降级为 serving，保证按钮可再用（用户可重新点下载续跑）。
      try {
        const jr = await fetch(`/api/jobs/${jobId}`);
        const job = await jr.json();
        if (state.jobId === jobId) state.job = job;
      } catch (_) {
        if (state.jobId === jobId && state.job
            && (state.job.status === "finalizing" || state.job.status === "rendering")) {
          state.job.status = "serving";
        }
      }
      alert("生成完整译本失败：" + (err.message || err));
    }
  } finally {
    state.finalizing = false;
    if (state.jobId === jobId) syncRetransButtons();   // 兜底：确保出错/异常路径也刷新可用态
  }
});

/* ---------- 新任务 ---------- */

els.newTaskBtn.addEventListener("click", () => {
  history.replaceState(null, "", location.pathname);
  state.jobId = null; state.job = null;
  state.finalizing = false;   // 中止进行中的 finalize 轮询（其循环会因 jobId 变化自行退出）
  clearTimeout(state.pageRetryTimer);
  clearTimeout(state.focusThrottleTimer); state.focusThrottleTimer = null;
  state.pageRetryDelay = PAGE_RETRY_MIN_MS;
  state.pageFetches.clear();   // 未 settle 的旧任务取页 Promise 不再需要跨任务复用
  if (state.originalDoc) { destroyPdfDoc(state.originalDoc); state.originalDoc = null; }
  for (const doc of state.pageDocs.values()) destroyPdfDoc(doc);
  state.pageDocs.clear();
  els.outlineTree.innerHTML = "";
  state.outlineLoaded = false;
  state.outlineDestCache = new WeakMap();
  state.outlineActiveEl = null;
  state.outlineClickSeq = 0;
  setOutlineCollapsed(true);
  els.cacheChip.hidden = true;
  els.jobModelChip.hidden = true;
  state.zoom = null;
  syncZoomLabel();
  els.clearFile.click();
  show(els.setupCard);
});

/* ---------- 健康检查 ---------- */

(async () => {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    els.modelName.textContent = data.model || "已连接";
    els.modelChip.classList.add("online");
    defaultModelName = data.model || "";
  } catch (_) {
    els.modelName.textContent = "后端未连接";
  }
})();

/* ---------- 模型设置面板（任意 OpenAI 兼容接口） ---------- */

function cfgShowMsg(text, ok) {
  els.cfgMsg.textContent = text;
  els.cfgMsg.className = "cfg-msg " + (ok ? "ok" : "err");
  els.cfgMsg.hidden = false;
}

async function openSettings() {
  els.cfgMsg.hidden = true;
  els.cfgApiKey.value = "";
  try {
    const res = await fetch("/api/config");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const cfg = await res.json();
    els.cfgBaseUrl.value = cfg.base_url || "";
    els.cfgModel.value = cfg.model || "";
    els.cfgApiKey.placeholder = cfg.api_key_masked ? `${cfg.api_key_masked}（留空则不修改）` : "sk-…";
    els.cfgConcurrency.value = cfg.concurrency ?? 6;
    els.cfgThinking.checked = !!cfg.thinking_enabled;
    els.settingsModal.hidden = false;
  } catch (err) {
    alert("读取配置失败：" + (err.message || err));
  }
}

els.modelChip.addEventListener("click", openSettings);
els.cfgCancel.addEventListener("click", () => { els.settingsModal.hidden = true; });
els.settingsModal.addEventListener("click", (e) => {
  if (e.target === els.settingsModal) els.settingsModal.hidden = true;
});

els.cfgSave.addEventListener("click", async () => {
  const body = {
    base_url: els.cfgBaseUrl.value.trim(),
    model: els.cfgModel.value.trim(),
    thinking_enabled: els.cfgThinking.checked,
    concurrency: parseInt(els.cfgConcurrency.value, 10) || 6,
  };
  const key = els.cfgApiKey.value.trim();
  if (key) body.api_key = key;
  els.cfgSave.disabled = true;
  try {
    const res = await fetch("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    els.modelName.textContent = data.model || body.model;
    els.cfgApiKey.value = "";
    els.cfgApiKey.placeholder = data.api_key_masked ? `${data.api_key_masked}（留空则不修改）` : els.cfgApiKey.placeholder;
    cfgShowMsg("已保存，之后创建的任务将使用新配置", true);
    setTimeout(() => { els.settingsModal.hidden = true; }, 900);
  } catch (err) {
    cfgShowMsg("保存失败：" + (err.message || err), false);
  } finally {
    els.cfgSave.disabled = false;
  }
});

/* ---------- 本机翻译接口覆盖面板（v3 模型覆盖：仅 localStorage，随上传一次性提交） ---------- *
 * 与上面的「模型设置」弹层（PUT /api/config，改服务端 .env 默认值）是两套独立机制：
 * 这里的设置只影响本浏览器发起的下一次上传，不写服务端配置、不重启也不影响其他用户。 */

function localCfgShowMsg(text, ok) {
  els.localCfgMsg.textContent = text;
  els.localCfgMsg.className = "cfg-msg " + (ok ? "ok" : "err");
  els.localCfgMsg.hidden = false;
}

function openLocalSettings() {
  els.localCfgMsg.hidden = true;
  const s = loadLocalSettings();
  els.localCfgBaseUrl.value = s.base_url;
  els.localCfgApiKey.value = s.api_key;
  els.localCfgModel.value = s.model;
  const fallback = defaultModelName ? `留空使用服务端默认（当前：${defaultModelName}）` : "留空使用服务端默认";
  els.localCfgModel.placeholder = fallback;
  els.localSettingsModal.hidden = false;
}

els.localSettingsBtn.addEventListener("click", openLocalSettings);
els.localCfgCancel.addEventListener("click", () => { els.localSettingsModal.hidden = true; });
els.localSettingsModal.addEventListener("click", (e) => {
  if (e.target === els.localSettingsModal) els.localSettingsModal.hidden = true;
});

els.localCfgClear.addEventListener("click", () => {
  clearLocalSettings();
  els.localCfgBaseUrl.value = "";
  els.localCfgApiKey.value = "";
  els.localCfgModel.value = "";
  localCfgShowMsg("已清除，下次上传将使用服务端默认", true);
});

els.localCfgSave.addEventListener("click", () => {
  saveLocalSettings({
    base_url: els.localCfgBaseUrl.value.trim(),
    api_key: els.localCfgApiKey.value.trim(),
    model: els.localCfgModel.value.trim(),
  });
  localCfgShowMsg("已保存到本机浏览器，下次上传生效", true);
  setTimeout(() => { els.localSettingsModal.hidden = true; }, 700);
});

/* ---------- 初始化：支持 ?job=<id> 恢复查看任务（刷新不丢、可分享） ---------- */

(() => {
  const jobId = new URLSearchParams(location.search).get("job");
  if (!jobId) return;
  state.jobId = jobId;
  show(els.progressCard);
  startPolling();
})();
