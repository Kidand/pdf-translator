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
  page: 1,
  renderTasks: { L: null, R: null },
  renderSeq: 0,
  pageRetryTimer: null,     // 当前页未译好时的轮询定时器
  finalizing: false,
};

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
  if (job.page_count) meta += `　共 ${job.page_count} 页`;
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
    state.pageDocs.clear();
    show(els.previewSection);
    applyView();
    postFocus(0);
    await renderCurrent();
    syncDownloadBtn();
  } catch (err) {
    showError("预览加载失败：" + (err.message || err));
  }
}

function totalPages() {
  return state.originalDoc ? state.originalDoc.numPages : 1;
}

function postFocus(pageIndex0) {
  // 火后不理：告诉后端当前浏览位置，用于优先翻译附近页
  fetch(`/api/jobs/${state.jobId}/focus`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page: pageIndex0 }),
  }).catch(() => {});
}

/* 页缓存上限：超出后按插入序淘汰最旧（简易 LRU），防止大文档长会话内存无限增长 */
const MAX_PAGE_DOCS = 40;

/* 取第 p 页（1-based）的已译单页文档；未译好返回 null；任务已出错抛异常 */
async function getTranslatedPageDoc(p) {
  if (state.pageDocs.has(p)) return state.pageDocs.get(p);
  const jobId = state.jobId;   // 捕获：请求返回时若已切换任务，结果作废
  const res = await api(`/page/${p - 1}`);
  if (state.jobId !== jobId) return null;
  if (res.status === 409) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || "任务已出错");
  }
  if (res.status !== 200) return null;   // 202 = 翻译中
  const data = await res.arrayBuffer();
  const doc = await pdfjsLib.getDocument({ data }).promise;
  if (state.jobId !== jobId) { doc.destroy(); return null; }  // 跨任务串页防护
  if (state.pageDocs.size >= MAX_PAGE_DOCS) {
    const oldest = state.pageDocs.keys().next().value;
    state.pageDocs.get(oldest).destroy();
    state.pageDocs.delete(oldest);
  }
  state.pageDocs.set(p, doc);
  return doc;
}

function applyView() {
  els.viewer.classList.toggle("compare", state.view === "compare");
  els.viewer.classList.toggle("single", state.view !== "compare");
  els.paneRTag.textContent = "译文";
  state.page = Math.min(state.page, totalPages());
  syncPager();
}

function syncPager() {
  els.pageInput.value = state.page;
  els.pageInput.max = totalPages();
  els.pageTotal.textContent = totalPages();
  els.prevPage.disabled = state.page <= 1;
  els.nextPage.disabled = state.page >= totalPages();
}

async function renderPageTo(doc, pageNum, canvas, slot, seq) {
  const page = await doc.getPage(pageNum);
  if (seq !== state.renderSeq) return;
  const wrap = canvas.parentElement;
  const cssWidth = Math.max(wrap.clientWidth - 2, 320);
  const base = page.getViewport({ scale: 1 });
  const scale = cssWidth / base.width;
  const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
  const viewport = page.getViewport({ scale: scale * dpr });

  if (state.renderTasks[slot]) { try { state.renderTasks[slot].cancel(); } catch (_) {} }

  canvas.width = Math.floor(viewport.width);
  canvas.height = Math.floor(viewport.height);
  canvas.style.width = cssWidth + "px";

  const ctx = canvas.getContext("2d");
  const task = page.render({ canvas, canvasContext: ctx, viewport });
  state.renderTasks[slot] = task;
  try { await task.promise; } catch (e) {
    if (e && e.name === "RenderingCancelledException") return;
    throw e;
  }
}

/* 右栏：渲染第 p 页译文；未译好时显示占位并安排重试；任务出错则终止并提示 */
async function renderTranslatedPane(p, seq) {
  let doc;
  try {
    doc = await getTranslatedPageDoc(p);
  } catch (err) {
    clearTimeout(state.pageRetryTimer);
    showError("翻译任务出错：" + (err.message || err));
    return;
  }
  if (seq !== state.renderSeq) return;
  if (doc) {
    els.paneRLoading.hidden = true;
    await renderPageTo(doc, 1, els.canvasR, "R", seq);
    return;
  }
  // 未译好：清空画布 + 占位 + 轮询重试
  els.canvasR.width = els.canvasL.width || 600;
  els.canvasR.height = els.canvasL.height || 800;
  els.canvasR.style.width = els.canvasL.style.width || "";
  els.canvasR.getContext("2d").clearRect(0, 0, els.canvasR.width, els.canvasR.height);
  els.paneRLoading.hidden = false;
  clearTimeout(state.pageRetryTimer);
  state.pageRetryTimer = setTimeout(() => {
    if (state.page === p && !els.previewSection.hidden) renderCurrent();
  }, 700);
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
}

function gotoPage(p) {
  const t = totalPages();
  state.page = Math.min(Math.max(1, p), t);
  postFocus(state.page - 1);
  syncPager();
  renderCurrent();
}

els.prevPage.addEventListener("click", () => gotoPage(state.page - 1));
els.nextPage.addEventListener("click", () => gotoPage(state.page + 1));
els.pageInput.addEventListener("change", () => gotoPage(parseInt(els.pageInput.value || "1", 10)));
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

/* ---------- 下载：finalize 全量翻译 + 按钮内进度 ---------- */

function syncDownloadBtn() {
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
    while (true) {
      await new Promise((r) => setTimeout(r, 900));
      if (state.jobId !== jobId) { resetBtn(); return; }  // 用户已切换任务，静默退出
      const jr = await fetch(`/api/jobs/${jobId}`);
      const job = await jr.json();
      if (state.jobId !== jobId) { resetBtn(); return; }
      state.job = job;
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
    if (state.jobId === jobId) alert("生成完整译本失败：" + (err.message || err));
  } finally {
    state.finalizing = false;
  }
});

/* ---------- 新任务 ---------- */

els.newTaskBtn.addEventListener("click", () => {
  history.replaceState(null, "", location.pathname);
  state.jobId = null; state.job = null;
  state.finalizing = false;   // 中止进行中的 finalize 轮询（其循环会因 jobId 变化自行退出）
  clearTimeout(state.pageRetryTimer);
  if (state.originalDoc) { state.originalDoc.destroy(); state.originalDoc = null; }
  for (const doc of state.pageDocs.values()) doc.destroy();
  state.pageDocs.clear();
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
  } catch (_) {
    els.modelName.textContent = "后端未连接";
  }
})();

/* ---------- 初始化：支持 ?job=<id> 恢复查看任务（刷新不丢、可分享） ---------- */

(() => {
  const jobId = new URLSearchParams(location.search).get("job");
  if (!jobId) return;
  state.jobId = jobId;
  show(els.progressCard);
  startPolling();
})();
