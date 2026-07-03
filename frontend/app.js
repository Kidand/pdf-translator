/* PDF 双语翻译器 — 前端逻辑 */
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
  paneRTag: $("paneRTag"), modelChip: $("modelChip"), modelName: $("modelName"), modeHint: $("modeHint"),
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
  resultDoc: null,
  page: 1,
  renderTasks: { L: null, R: null },
  renderSeq: 0,
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

const STAGE_LABEL = {
  queued: "正在排队…",
  extracting: "正在解析 PDF 版式…",
  translating: "正在翻译…",
  rendering: "正在按原版式重排译文…",
};

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
        ? "每页原文后紧跟一页译文，适合双页对照阅读"
        : "输出仅含译文页，版式与原文一致";
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

/* ---------- 进度轮询 ---------- */

function updateProgressUI(job) {
  const pct = Math.round((job.progress || 0) * 100);
  els.barFill.style.width = pct + "%";

  const stageOrder = ["extracting", "translating", "rendering"];
  const cur = job.status;
  document.querySelectorAll(".step").forEach((st) => {
    const s = st.dataset.s;
    st.classList.remove("active", "done");
    if (s === cur) st.classList.add("active");
    else if (stageOrder.indexOf(s) < stageOrder.indexOf(cur) || cur === "done") st.classList.add("done");
  });

  let meta = STAGE_LABEL[job.status] || "";
  const parts = [];
  if (job.page_count) parts.push(`共 ${job.page_count} 页`);
  if (job.status === "translating" && job.total_blocks) {
    parts.push(`已翻译 ${job.done_blocks} / ${job.total_blocks} 段`);
  }
  parts.push(pct + "%");
  els.progressMeta.textContent = meta + "　" + parts.join(" · ");
}

async function startPolling() {
  state.polling = true;
  let failures = 0;
  while (state.polling) {
    try {
      const res = await fetch(`/api/jobs/${state.jobId}`);
      if (res.status === 404) throw new Error("任务不存在");
      const job = await res.json();
      state.job = job;
      failures = 0;
      updateProgressUI(job);
      if (job.status === "done") { state.polling = false; await openPreview(); return; }
      if (job.status === "error") { state.polling = false; showError(job.error || "未知错误"); return; }
    } catch (err) {
      if (++failures > 8) { state.polling = false; showError("与服务器失去连接：" + err.message); return; }
    }
    await new Promise((r) => setTimeout(r, 800));
  }
}

els.cancelWatch.addEventListener("click", () => { state.polling = false; show(els.setupCard); });

function showError(msg) {
  state.polling = false;
  els.errorMsg.textContent = msg;
  show(els.errorCard);
}
els.retryBtn.addEventListener("click", () => show(els.setupCard));

/* ---------- 预览 ---------- */

async function openPreview() {
  els.progressMeta.textContent = "翻译完成，正在加载预览…";
  const base = `/api/jobs/${state.jobId}`;
  try {
    const [orig, result] = await Promise.all([
      pdfjsLib.getDocument({ url: `${base}/file/original` }).promise,
      pdfjsLib.getDocument({ url: `${base}/file/result` }).promise,
    ]);
    state.originalDoc = orig;
    state.resultDoc = result;
    state.page = 1;
    els.downloadBtn.href = `${base}/download`;
    show(els.previewSection);
    applyView();
    await renderCurrent();
  } catch (err) {
    showError("预览加载失败：" + (err.message || err));
  }
}

function totalPages() {
  if (!state.resultDoc) return 1;
  if (state.view === "compare") return state.originalDoc.numPages;
  return state.resultDoc.numPages;
}

/* 对照视图下，原文第 p 页（1-based）对应结果文档中的译文页码 */
function translatedPageFor(p) {
  return state.job.mode === "interleaved" ? Math.min(2 * p, state.resultDoc.numPages) : p;
}

function applyView() {
  els.viewer.classList.toggle("compare", state.view === "compare");
  els.viewer.classList.toggle("single", state.view !== "compare");
  els.paneRTag.textContent = state.view === "compare" ? "译文" : "翻译结果";
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

async function renderCurrent() {
  if (!state.resultDoc) return;
  const seq = ++state.renderSeq;
  const p = state.page;
  try {
    if (state.view === "compare") {
      await Promise.all([
        renderPageTo(state.originalDoc, p, els.canvasL, "L", seq),
        renderPageTo(state.resultDoc, translatedPageFor(p), els.canvasR, "R", seq),
      ]);
    } else {
      await renderPageTo(state.resultDoc, p, els.canvasR, "R", seq);
    }
  } catch (err) {
    console.error("渲染失败", err);
  }
  syncPager();
}

function gotoPage(p) {
  const t = totalPages();
  state.page = Math.min(Math.max(1, p), t);
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

/* ---------- 新任务 ---------- */

els.newTaskBtn.addEventListener("click", () => {
  history.replaceState(null, "", location.pathname);
  state.jobId = null; state.job = null;
  if (state.originalDoc) { state.originalDoc.destroy(); state.originalDoc = null; }
  if (state.resultDoc) { state.resultDoc.destroy(); state.resultDoc = null; }
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
