const $ = (selector) => document.querySelector(selector);
const state = { file: null, videoId: sessionStorage.getItem("videoId") || "", timer: null };
const els = {
  file: $("#videoFile"), dropzone: $("#dropzone"), fileSummary: $("#fileSummary"), upload: $("#uploadButton"),
  status: $("#statusLabel"), detail: $("#statusDetail"), progress: $("#progressBar"), player: $("#player"),
  emptyPlayer: $("#emptyPlayer"), form: $("#questionForm"), question: $("#question"), ask: $("#askButton"),
  answerEmpty: $("#answerEmpty"), answerCard: $("#answerCard"), answer: $("#answerText"), citations: $("#citations"),
  dialog: $("#settingsDialog"), apiKey: $("#apiKey"), tenant: $("#tenantId"), toast: $("#toast")
};

function credentials() {
  return { "X-API-Key": sessionStorage.getItem("apiKey") || "", "X-Tenant-Id": sessionStorage.getItem("tenantId") || "demo" };
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...credentials(), ...(options.headers || {}) } });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try { message = (await response.json()).detail || message; } catch (_) { /* response is not JSON */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}
function notify(message) {
  els.toast.textContent = message; els.toast.classList.add("show");
  window.setTimeout(() => els.toast.classList.remove("show"), 2800);
}
function setProgress(percent, label, detail) {
  els.progress.style.width = `${Math.max(0, Math.min(100, percent))}%`;
  els.status.textContent = label; els.detail.textContent = detail;
}
function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB"]; let index = 0, value = bytes;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}
function formatTime(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
  return h ? `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}` : `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}
async function sha256(file) {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
}
function chooseFile(file) {
  if (!file) return;
  if (!file.type.startsWith("video/")) return notify("请选择视频文件。");
  state.file = file;
  els.fileSummary.hidden = false;
  els.fileSummary.textContent = `${file.name} · ${formatBytes(file.size)}`;
  els.upload.disabled = false;
  setProgress(0, "准备就绪", "点击“上传并解析”开始。 ");
}
async function upload() {
  if (!state.file) return;
  if (!sessionStorage.getItem("apiKey")) { els.dialog.showModal(); return; }
  els.upload.disabled = true;
  try {
    setProgress(3, "正在准备", "创建分片上传任务…");
    const hashPromise = sha256(state.file);
    const started = await api("/uploads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename: state.file.name, size_bytes: state.file.size }) });
    const count = Math.ceil(state.file.size / started.part_size), parts = [];
    for (let number = 1; number <= count; number++) {
      const signed = await api(`/uploads/${started.upload_id}/parts/${number}`);
      const start = (number - 1) * started.part_size;
      const response = await fetch(signed.url, { method: "PUT", body: state.file.slice(start, Math.min(start + started.part_size, state.file.size)) });
      if (!response.ok) throw new Error(`第 ${number} 个分片上传失败`);
      const etag = response.headers.get("ETag");
      if (!etag) throw new Error("对象存储未返回 ETag，请检查 CORS 配置");
      parts.push({ PartNumber: number, ETag: etag });
      setProgress(5 + 60 * number / count, "正在上传", `已上传 ${number} / ${count} 个分片`);
    }
    await api(`/uploads/${started.upload_id}/complete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ parts }) });
    const created = await api("/videos", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ upload_id: started.upload_id, sha256: await hashPromise }) });
    state.videoId = created.job_id; sessionStorage.setItem("videoId", state.videoId);
    setProgress(70, "等待解析", "任务已提交，正在读取语音与幻灯片…");
    pollJob();
  } catch (error) {
    setProgress(0, "执行失败", error.message); els.upload.disabled = false; notify(error.message);
  }
}
async function pollJob() {
  window.clearTimeout(state.timer);
  if (!state.videoId) return;
  try {
    const job = await api(`/jobs/${state.videoId}`);
    const stages = { queued: 72, running: 84, complete: 100, failed: 0 };
    setProgress(stages[job.status] ?? 78, job.status === "complete" ? "解析完成" : job.status === "failed" ? "解析失败" : "正在解析", job.error || `当前阶段：${job.stage || job.status}`);
    if (job.status === "complete") return activateQuestion();
    if (job.status === "failed") { els.upload.disabled = false; return; }
    state.timer = window.setTimeout(pollJob, 2000);
  } catch (error) {
    setProgress(0, "无法恢复任务", error.message); sessionStorage.removeItem("videoId");
  }
}
function activateQuestion() {
  els.question.disabled = false; els.ask.disabled = false; els.upload.disabled = false;
  els.answerEmpty.textContent = "输入问题，系统只会依据当前视频回答。";
}
async function askQuestion(event) {
  event.preventDefault(); const question = els.question.value.trim(); if (question.length < 2) return notify("问题至少需要两个字符。");
  els.ask.disabled = true; els.ask.textContent = "检索中…";
  try {
    const result = await api(`/videos/${state.videoId}/questions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) });
    els.answerEmpty.hidden = true; els.answerCard.hidden = false; els.answer.textContent = result.answer;
    els.citations.replaceChildren();
    if (result.abstained) { els.answerCard.classList.add("abstained"); notify("视频中没有足够证据。"); }
    else {
      els.answerCard.classList.remove("abstained"); setPlayer(result.playback_url);
      result.citations.forEach(item => els.citations.append(citationButton(item)));
    }
  } catch (error) { notify(error.message); }
  finally { els.ask.disabled = false; els.ask.textContent = "查找证据"; }
}
function setPlayer(url) {
  if (els.player.src !== url) { els.player.src = url; els.player.style.display = "block"; els.emptyPlayer.hidden = true; }
}
function citationButton(item) {
  const button = document.createElement("button"); button.type = "button"; button.className = "citation";
  const scores = [["K", item.keyword_score], ["V", item.vector_score], ["F", item.fused_score]].filter(([, value]) => value !== undefined).map(([name, value]) => `${name} ${Number(value).toFixed(2)}`).join(" · ");
  button.innerHTML = `<span class="citation-time">${formatTime(item.start_ms)}–${formatTime(item.end_ms)}</span><span class="citation-text"></span><span class="citation-meta"></span>`;
  button.querySelector(".citation-text").textContent = item.text;
  button.querySelector(".citation-meta").textContent = `${item.modality === "slide" ? "幻灯片" : "语音"}${scores ? ` · ${scores}` : ""}`;
  button.addEventListener("click", () => { document.querySelectorAll(".citation").forEach(node => node.classList.remove("active")); button.classList.add("active"); els.player.currentTime = item.start_ms / 1000; els.player.play().catch(() => {}); });
  return button;
}

els.file.addEventListener("change", () => chooseFile(els.file.files[0]));
for (const eventName of ["dragenter", "dragover"]) els.dropzone.addEventListener(eventName, event => { event.preventDefault(); els.dropzone.classList.add("dragging"); });
for (const eventName of ["dragleave", "drop"]) els.dropzone.addEventListener(eventName, event => { event.preventDefault(); els.dropzone.classList.remove("dragging"); });
els.dropzone.addEventListener("drop", event => chooseFile(event.dataTransfer.files[0]));
els.upload.addEventListener("click", upload); els.form.addEventListener("submit", askQuestion);
$("#settingsButton").addEventListener("click", () => { els.apiKey.value = sessionStorage.getItem("apiKey") || ""; els.tenant.value = sessionStorage.getItem("tenantId") || "demo"; els.dialog.showModal(); });
$("#settingsForm").addEventListener("submit", () => { sessionStorage.setItem("apiKey", els.apiKey.value); sessionStorage.setItem("tenantId", els.tenant.value || "demo"); notify("连接设置已保存。"); if (state.videoId) pollJob(); });
if (state.videoId && sessionStorage.getItem("apiKey")) pollJob();
