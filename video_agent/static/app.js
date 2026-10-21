const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const state = { local: new Map(), remote: [], active: 0, selected: new Set(), current: null,
  transcript: [], summary: null, polling: null, streamController: null, pauseAll: false };
const MAX_UPLOADS = 3;
const els = { file: $("#videoFile"), dropzone: $("#dropzone"), list: $("#taskList"), count: $("#queueCount"),
  stats: $("#queueStats"), workspace: $("#workspace"), empty: $("#deskEmpty"), player: $("#player"),
  playerEmpty: $("#playerEmpty"), toast: $("#toast"), dialog: $("#settingsDialog"), apiKey: $("#apiKey"),
  tenant: $("#tenantId"), batch: $("#batchExport") };

function credentials() { return { "X-API-Key": sessionStorage.getItem("apiKey") || "", "X-Tenant-Id": sessionStorage.getItem("tenantId") || "demo" }; }
function conversationKey(videoId) { return `video-conversation:${sessionStorage.getItem("tenantId") || "demo"}:${videoId}`; }
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...credentials(), ...(options.headers || {}) } });
  if (!response.ok) { let message = `请求失败（${response.status}）`; try { const body = await response.json(); message = body.detail || body.error?.message || message; } catch (_) {} const error = new Error(typeof message === "string" ? message : JSON.stringify(message)); error.status = response.status; throw error; }
  if (response.status === 204) return null;
  return (response.headers.get("content-type") || "").includes("json") ? response.json() : response;
}
function notify(message) { els.toast.textContent = message; els.toast.classList.add("show"); setTimeout(() => els.toast.classList.remove("show"), 3000); }
function fmtBytes(bytes) { const units = ["B","KB","MB","GB"]; let i=0,v=bytes||0; while(v>=1024&&i<3){v/=1024;i++;} return `${v.toFixed(i?1:0)} ${units[i]}`; }
function fmtTime(ms) { const value=Math.max(0,Math.floor(ms/1000)),h=Math.floor(value/3600),m=Math.floor(value%3600/60),s=value%60; return h?`${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`:`${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`; }
function uploadKey(file) { return `video-upload:${sessionStorage.getItem("tenantId")||"demo"}:${file.name}:${file.size}:${file.lastModified}`; }
function loadUpload(file) { try { return JSON.parse(localStorage.getItem(uploadKey(file)) || "null"); } catch (_) { return null; } }
function saveUpload(file,value){ localStorage.setItem(uploadKey(file),JSON.stringify(value)); }
function clearUpload(file){ localStorage.removeItem(uploadKey(file)); }
async function sha256(file){ const digest=await crypto.subtle.digest("SHA-256",await file.arrayBuffer()); return [...new Uint8Array(digest)].map(x=>x.toString(16).padStart(2,"0")).join(""); }

function addFiles(files) {
  [...files].filter(file => file.type.startsWith("video/")).forEach(file => {
    const id = crypto.randomUUID(); state.local.set(id,{ id,file,title:file.name,source_type:"local",status:"queued",stage:"等待上传",progress:0,controller:null });
  });
  renderQueue(); schedule();
}
async function openUpload(file) {
  const saved=loadUpload(file);
  if(saved){ try { const remote=await api(`/uploads/${saved.upload_id}`); if(["open","completing","complete"].includes(remote.status)&&remote.declared_bytes===file.size)return {...saved,...remote}; } catch(error){ if(![404,409,410].includes(error.status))throw error; } clearUpload(file); }
  const started=await api("/uploads",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({filename:file.name,size_bytes:file.size})});
  const session={...started,parts:[],idempotency_key:crypto.randomUUID()}; saveUpload(file,session); return session;
}
async function uploadTask(task) {
  state.active++; task.status="uploading"; renderQueue();
  try {
    const hashPromise=sha256(task.file), started=await openUpload(task.file); task.uploadId=started.upload_id;
    const count=Math.ceil(task.file.size/started.part_size), parts=new Map((started.parts||[]).map(part=>[part.PartNumber,part]));
    for(let number=1;started.status==="open"&&number<=count;number++){
      if(task.status==="paused") throw Object.assign(new Error("paused"),{paused:true});
      if(parts.has(number)){task.progress=Math.round(65*number/count);renderQueue();continue;}
      const signed=await api(`/uploads/${started.upload_id}/parts/${number}`), start=(number-1)*started.part_size;
      task.controller=new AbortController(); const response=await fetch(signed.url,{method:"PUT",signal:task.controller.signal,body:task.file.slice(start,Math.min(start+started.part_size,task.file.size))});
      if(!response.ok)throw new Error(`第 ${number} 个分片上传失败`); const etag=response.headers.get("ETag"); if(!etag)throw new Error("对象存储未返回 ETag，请检查 CORS");
      parts.set(number,{PartNumber:number,ETag:etag}); task.progress=Math.round(65*number/count); task.stage=`上传 ${number}/${count}`; renderQueue();
    }
    if(started.status!=="complete")await api(`/uploads/${started.upload_id}/complete`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({parts:[...parts.values()].sort((a,b)=>a.PartNumber-b.PartNumber)})});
    const created=await api("/videos",{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":started.idempotency_key},body:JSON.stringify({upload_id:started.upload_id,sha256:await hashPromise})});
    clearUpload(task.file); state.local.delete(task.id); await refreshVideos(); if(created.job_id)openVideo(created.job_id);
  } catch(error) {
    if(task.status!=="paused"&&!error.paused){task.status="failed";task.stage=error.message;notify(`${task.title}：${error.message}`);}
  } finally { task.controller=null; state.active--; renderQueue(); schedule(); }
}
function schedule(){ if(state.pauseAll)return; for(const task of state.local.values()){if(state.active>=MAX_UPLOADS)break;if(task.status==="queued")uploadTask(task);} }
function pauseTask(task){task.status="paused";task.stage="已暂停，可继续";task.controller?.abort();renderQueue();}
function resumeTask(task){task.status="queued";task.stage="等待上传";schedule();renderQueue();}
async function cancelTask(task){task.controller?.abort();if(task.uploadId)try{await api(`/uploads/${task.uploadId}`,{method: "DELETE"});}catch(_){}clearUpload(task.file);state.local.delete(task.id);renderQueue();schedule();}

function taskNode(item,isLocal=false){
  const row=document.createElement("article");row.className=`task ${state.current===item.id?"selected":""}`;row.dataset.id=item.id;
  const check=document.createElement("input");check.type="checkbox";check.className="task-check";check.checked=state.selected.has(item.id);check.disabled=isLocal||!item.id;check.addEventListener("click",event=>{event.stopPropagation();check.checked?state.selected.add(item.id):state.selected.delete(item.id);els.batch.disabled=!state.selected.size;});
  const main=document.createElement("div");main.className="task-main";const title=document.createElement("strong");title.className="task-title";title.textContent=item.title||item.filename||"未命名视频";
  const meta=document.createElement("div");meta.className="task-meta";meta.textContent=`${item.source_type==="url"?"网址导入":"本地视频"}${isLocal?` · ${fmtBytes(item.file.size)}`:""}`;
  const progress=document.createElement("div");progress.className="mini-progress";progress.innerHTML=`<i style="width:${item.progress||0}%"></i>`;const detail=document.createElement("div");detail.className="task-detail";detail.textContent=item.error||item.stage||item.status;
  main.append(title,meta,progress,detail);const menu=document.createElement("div");menu.className="task-menu";
  if(isLocal){const action=document.createElement("button");action.textContent=item.status==="paused"?"▶":"Ⅱ";action.title=item.status==="paused"?"继续":"暂停";action.onclick=e=>{e.stopPropagation();item.status==="paused"?resumeTask(item):pauseTask(item);};const remove=document.createElement("button");remove.textContent="×";remove.title="取消并移除";remove.onclick=e=>{e.stopPropagation();cancelTask(item);};menu.append(action,remove);} else if(["failed","cancelled"].includes(item.status)){const retry=document.createElement("button");retry.textContent="↻";retry.title="重试";retry.onclick=async e=>{e.stopPropagation();await api(`/jobs/${item.id}/retry`,{method:"POST"});refreshVideos();};menu.append(retry);} else if(!["ready","complete"].includes(item.status)){const cancel=document.createElement("button");cancel.textContent="■";cancel.title="取消任务";cancel.onclick=async e=>{e.stopPropagation();await api(`/jobs/${item.id}/cancel`,{method:"POST"});refreshVideos();};menu.append(cancel);}
  row.append(check,main,menu);if(!isLocal)row.addEventListener("click",()=>openVideo(item.id));return row;
}
function renderQueue(){
  els.list.replaceChildren();const items=[...state.local.values(),...state.remote];if(!items.length){els.list.innerHTML='<div class="queue-empty">添加视频后，任务会在这里按顺序推进。</div>';}
  items.forEach(item=>els.list.append(taskNode(item,state.local.has(item.id))));els.count.textContent=items.length;els.stats.textContent=`${state.active} 个上传中 · ${items.length} 个任务`;
}
async function refreshVideos(){if(!sessionStorage.getItem("apiKey"))return;try{const query=new URLSearchParams({page_size:"100",q:$("#globalSearch").value,status:$("#statusFilter").value});const result=await api(`/videos?${query}`);state.remote=result.items;renderQueue();if(state.current){const current=state.remote.find(x=>x.id===state.current);if(current)updateWorkspaceStatus(current);}}catch(error){notify(error.message);}}

async function openVideo(id){state.current=id;renderQueue();try{const video=await api(`/videos/${id}`);els.empty.hidden=true;els.workspace.hidden=false;$("#videoTitle").textContent=video.title;$("#videoMeta").textContent=`${video.source_type==="url"?"网址导入":"本地视频"} · ${fmtTime(video.duration_ms)} · ${new Date(video.created_at).toLocaleDateString("zh-CN")}`;$("#favoriteButton").textContent=video.favorite?"★":"☆";$("#favoriteButton").dataset.favorite=video.favorite;renderTags(video.tags||[]);updateWorkspaceStatus(video);if(video.playback_url){els.player.src=video.playback_url;els.player.style.display="block";els.playerEmpty.hidden=true;}else{els.player.removeAttribute("src");els.player.style.display="none";els.playerEmpty.hidden=false;}await Promise.all([loadTranscript(),loadSummary(),loadAudio()]);watchJob(id);}catch(error){notify(error.message);}}
function renderTags(tags){const container=$("#videoTags");container.replaceChildren(...tags.map(tag=>{const node=document.createElement("span");node.className="tag";node.textContent=tag;return node;}));const add=document.createElement("button");add.className="tag";add.textContent="＋ 标签";add.onclick=async()=>{const value=prompt("输入标签名称");if(!value?.trim())return;const next=[...new Set([...tags,value.trim()])];await api(`/videos/${state.current}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({tags:next})});renderTags(next);refreshVideos();};container.append(add);}
async function watchJob(id){state.streamController?.abort();const controller=new AbortController();state.streamController=controller;try{const response=await fetch(`/jobs/${id}/events`,{headers:credentials(),signal:controller.signal});if(!response.ok)return;const reader=response.body.getReader(),decoder=new TextDecoder();let buffer="";while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});const events=buffer.split("\n\n");buffer=events.pop();for(const event of events){const line=event.split("\n").find(x=>x.startsWith("data: "));if(!line)continue;const job=JSON.parse(line.slice(6));updateWorkspaceStatus(job);const cached=state.remote.find(x=>x.id===id);if(cached)Object.assign(cached,job);renderQueue();}}}catch(error){if(error.name!=="AbortError")setTimeout(()=>state.current===id&&refreshVideos(),2000);}}
function updateWorkspaceStatus(video){const progress=video.progress||0;$("#workStatus").textContent=video.error||video.stage||video.status;$("#workProgress").style.width=`${progress}%`;$("#workPercent").textContent=`${progress}%`;}
async function loadTranscript(){if(!state.current)return;try{const data=await api(`/videos/${state.current}/transcript`);state.transcript=data.items;renderTranscript();}catch(_){state.transcript=[];renderTranscript();}}
function renderTranscript(){const term=$("#transcriptSearch").value.trim().toLowerCase(),rows=state.transcript.filter(x=>!term||x.text.toLowerCase().includes(term)),list=$("#transcriptList");list.replaceChildren();if(!rows.length){list.innerHTML='<div class="queue-empty">暂无文字稿，视频解析完成后会显示在这里。</div>';return;}rows.forEach(item=>{const row=document.createElement("div");row.className="transcript-row";const time=document.createElement("button");time.className="time-button";time.textContent=fmtTime(item.start_ms);time.onclick=()=>seek(item.start_ms);const text=document.createElement("p");text.textContent=item.text;row.append(time,text);list.append(row);});}
function seek(ms){els.player.currentTime=ms/1000;els.player.play().catch(()=>{});}
async function loadAudio(){if(!state.current)return;const data=await api(`/videos/${state.current}/audio`);const button=$("#audioButton");if(data.status==="ready"){button.textContent="下载 MP3";button.dataset.url=data.download_url;$("#audioStatus").textContent="音频已经准备好，可随时下载。";}else{button.textContent=data.status==="processing"?"生成中…":"生成 MP3";button.dataset.url="";$("#audioStatus").textContent=data.status==="failed"?"生成失败，可再次尝试。":"将当前视频提取为通用 MP3 文件。";}}
async function loadSummary(){if(!state.current)return;const data=await api(`/videos/${state.current}/summary`);state.summary=data.summary;renderSummary();}
function renderSummary(){const box=$("#summaryContent");box.replaceChildren();if(!state.summary){box.innerHTML='<div class="queue-empty">生成后，这里会显示带时间码的章节、核心要点和结论。</div>';return;}(state.summary.chapters||[]).forEach(chapter=>{const node=document.createElement("article");node.className="chapter";const time=document.createElement("button");time.textContent=fmtTime(chapter.start_ms);time.onclick=()=>seek(chapter.start_ms);const title=document.createElement("h3");title.textContent=chapter.title;const text=document.createElement("p");text.textContent=chapter.summary;node.append(time,title,text);box.append(node);});const points=document.createElement("div");points.className="summary-block";points.textContent=`核心要点\n${(state.summary.key_points||[]).map(x=>`• ${x}`).join("\n")}\n\n结论\n${state.summary.conclusion||""}`;points.style.whiteSpace="pre-wrap";box.append(points);}

async function download(path,name){const response=await fetch(path,{headers:credentials()});if(!response.ok)return notify("下载失败");const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download=name;link.click();URL.revokeObjectURL(url);}
function citationButton(item){const button=document.createElement("button");button.className="citation";button.type="button";const time=document.createElement("span");time.className="citation-time";time.textContent=`${fmtTime(item.start_ms)}–${fmtTime(item.end_ms)}`;const text=document.createElement("span");text.textContent=item.text;button.append(time,text);button.onclick=()=>seek(item.start_ms);return button;}

els.file.addEventListener("change",()=>addFiles(els.file.files));["dragenter","dragover"].forEach(name=>els.dropzone.addEventListener(name,e=>{e.preventDefault();els.dropzone.classList.add("dragging");}));["dragleave","drop"].forEach(name=>els.dropzone.addEventListener(name,e=>{e.preventDefault();els.dropzone.classList.remove("dragging");}));els.dropzone.addEventListener("drop",e=>addFiles(e.dataTransfer.files));
$$('.mode-tab').forEach(button=>button.onclick=()=>{$$('.mode-tab').forEach(x=>x.classList.toggle("active",x===button));$("#localPane").hidden=button.dataset.mode!=="local";$("#urlForm").hidden=button.dataset.mode!=="url";});
$("#urlForm").onsubmit=async event=>{event.preventDefault();try{await api("/imports",{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":crypto.randomUUID()},body:JSON.stringify({url:$("#videoUrl").value})});$("#videoUrl").value="";notify("网址已加入导入队列");refreshVideos();}catch(error){notify(error.message);}};
$("#pauseAll").onclick=()=>{state.pauseAll=!state.pauseAll;$("#pauseAll").textContent=state.pauseAll?"继续全部":"全部暂停";state.local.forEach(task=>state.pauseAll&&task.status==="uploading"?pauseTask(task):(!state.pauseAll&&task.status==="paused"&&resumeTask(task)));if(!state.pauseAll)schedule();};
$("#retryFailed").onclick=()=>{state.local.forEach(task=>{if(task.status==="failed")resumeTask(task);});state.remote.filter(x=>["failed","cancelled"].includes(x.status)).forEach(x=>api(`/jobs/${x.id}/retry`,{method:"POST"}).catch(error=>notify(error.message)));};
let searchTimer;$("#globalSearch").oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(refreshVideos,300);};$("#statusFilter").onchange=refreshVideos;$("#transcriptSearch").oninput=renderTranscript;
$$('.work-tab').forEach(button=>button.onclick=()=>{$$('.work-tab').forEach(x=>x.classList.toggle("active",x===button));$$('.tool-panel').forEach(panel=>panel.hidden=panel.id!==`${button.dataset.tab}Panel`);});
$$('[data-download]').forEach(button=>button.onclick=()=>state.current&&download(`/videos/${state.current}/transcript?format=${button.dataset.download}`,`transcript.${button.dataset.download}`));
$("#copyTranscript").onclick=()=>navigator.clipboard.writeText(state.transcript.map(x=>x.text).join("\n")).then(()=>notify("文字稿已复制"));
$("#audioButton").onclick=async()=>{if(!state.current)return;if($("#audioButton").dataset.url){location.href=$("#audioButton").dataset.url;return;}try{await api(`/videos/${state.current}/audio`,{method:"POST"});notify("正在生成 MP3");setTimeout(loadAudio,1500);}catch(error){notify(error.message);}};
$("#summaryButton").onclick=async()=>{if(!state.current)return;const button=$("#summaryButton");button.disabled=true;button.textContent="生成中…";try{state.summary=await api(`/videos/${state.current}/summary?regenerate=${Boolean(state.summary)}`,{method:"POST"});renderSummary();}catch(error){notify(error.message);}finally{button.disabled=false;button.textContent=state.summary?"重新生成":"生成总结";}};
$("#copySummary").onclick=()=>state.summary&&navigator.clipboard.writeText($("#summaryContent").innerText).then(()=>notify("总结已复制"));
$("#questionForm").onsubmit=async event=>{event.preventDefault();const question=$("#question").value.trim();if(question.length<2)return notify("问题至少需要两个字符");const button=$("#askButton");button.disabled=true;try{const conversation_id=sessionStorage.getItem(conversationKey(state.current));const result=await api(`/videos/${state.current}/questions`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question,conversation_id})});sessionStorage.setItem(conversationKey(state.current),result.conversation_id);$("#answerCard").hidden=false;$("#answerText").textContent=result.answer;$("#citations").replaceChildren(...result.citations.map(citationButton));if(result.playback_url&&!els.player.src){els.player.src=result.playback_url;els.player.style.display="block";}}catch(error){notify(error.message);}finally{button.disabled=false;}};
$("#favoriteButton").onclick=async()=>{const next=$("#favoriteButton").dataset.favorite!=="true";await api(`/videos/${state.current}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({favorite:next})});$("#favoriteButton").dataset.favorite=next;$("#favoriteButton").textContent=next?"★":"☆";refreshVideos();};
$("#deleteVideo").onclick=async()=>{if(!state.current||!confirm("删除该视频及其文字、音频和总结？此操作无法撤销。"))return;await api(`/videos/${state.current}`,{method:"DELETE"});state.current=null;els.workspace.hidden=true;els.empty.hidden=false;refreshVideos();};
els.batch.onclick=async()=>{const ids=[...state.selected];const response=await fetch("/videos/batch-export",{method:"POST",headers:{...credentials(),"Content-Type":"application/json"},body:JSON.stringify(ids)});if(!response.ok)return notify("批量导出失败");const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download="video-evidence-export.zip";a.click();URL.revokeObjectURL(url);};
$("#settingsButton").onclick=()=>{els.apiKey.value=sessionStorage.getItem("apiKey")||"";els.tenant.value=sessionStorage.getItem("tenantId")||"demo";els.dialog.showModal();};$("#settingsForm").onsubmit=()=>{sessionStorage.setItem("apiKey",els.apiKey.value);sessionStorage.setItem("tenantId",els.tenant.value||"demo");notify("连接设置已保存");refreshVideos();};
if(sessionStorage.getItem("apiKey")){refreshVideos();state.polling=setInterval(refreshVideos,3000);}else setTimeout(()=>els.dialog.showModal(),300);
