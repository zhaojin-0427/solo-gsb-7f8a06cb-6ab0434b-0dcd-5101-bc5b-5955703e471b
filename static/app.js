// 本地多机位同步剪辑 - 原生 JS 单页
"use strict";

const FPS = 30;
const state = {
  assets: [],
  edit: null,
  totalFrames: 0,
  currentFrame: 0,
  playing: false,
  playTimer: null,
  anchorDraft: {}, // assetId -> [{material, timeline}]
  cuts: [],        // [{start, end, asset_id}]
  currentJob: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
  });
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(typeof data === "string" ? data : JSON.stringify(data.detail || data));
  return data;
}

function showMsg(el, text, isErr = false) {
  el.textContent = text;
  el.className = "msg" + (isErr ? " err" : "");
}

async function refresh() {
  const s = await api("/api/state");
  state.assets = s.assets;
  state.edit = s.edit;
  state.totalFrames = s.output ? s.output.frames : 0;
  renderAssets();
  renderAnchorEditor();
  renderCutsEditor();
  if (s.edit) {
    $("duration").value = s.edit.duration;
    state.anchorDraft = {};
    for (const [id, list] of Object.entries(s.edit.anchors)) {
      state.anchorDraft[id] = list.map(a => ({ ...a }));
    }
    state.cuts = (s.edit.cuts || []).map(c => ({ ...c }));
    renderAnchorEditor();
    renderCutsEditor();
  }
  renderExports(s.exports);
  if (s.output) {
    $("frameInfo").textContent =
      `共 ${s.output.frames} 帧 / ${s.output.samples} 采样，${s.output.width}x${s.output.height}`;
  }
}

// ---------------------------------------------------------------- assets --

function renderAssets() {
  const box = $("assetList");
  box.innerHTML = "";
  for (const a of state.assets) {
    const card = document.createElement("div");
    card.className = "asset-card";
    const kindLabel = a.kind === "video"
      ? `视频 ${a.width}x${a.height} @${a.fps}fps ${a.nframes}帧`
      : `音频 ${a.channels}ch ${a.in_sample_rate}Hz ${a.nsamples}采样`;
    card.innerHTML =
      `<span class="name">#${a.id} ${a.kind === "video" ? "🎥" : "🎧"} ${escapeHtml(a.filename)}</span>
       <span>${kindLabel}</span>
       <span>显示起点 material0
         <input value="${a.material0}" data-k="material0" data-id="${a.id}">
       </span>
       <span>钟速 clock
         <input value="${a.clock}" data-k="clock" data-id="${a.id}">
       </span>
       <button class="mini" data-clock="${a.id}">保存时钟参数</button>`;
    box.appendChild(card);
  }
  box.querySelectorAll("button[data-clock]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = Number(btn.dataset.clock);
      const m0 = cardInput(id, "material0").value;
      const clock = cardInput(id, "clock").value;
      try {
        await api(`/api/assets/${id}/clock`, {
          method: "PUT", body: JSON.stringify({ material0: m0, clock }),
        });
        await refresh();
      } catch (e) { alert(e.message); }
    });
  });
}

function cardInput(id, k) {
  return document.querySelector(`input[data-id="${id}"][data-k="${k}"]`);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function upload(files, kind) {
  for (const file of files) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("kind", kind);
    const res = await fetch("/api/assets?kind=" + kind, { method: "POST", body: fd });
    if (!res.ok) {
      const t = await res.text();
      alert(`上传失败: ${t}`); return;
    }
  }
  await refresh();
}

// --------------------------------------------------------------- anchors --

function renderAnchorEditor() {
  const box = $("anchorEditor");
  box.innerHTML = "";
  if (!state.assets.length) {
    box.innerHTML = '<p class="hint">先上传素材。</p>';
    return;
  }
  for (const a of state.assets) {
    const block = document.createElement("div");
    block.className = "anchor-block";
    const title = document.createElement("div");
    title.textContent =
      `#${a.id} ${a.kind === "video" ? "🎥" : "🎧"} ${a.filename}  ` +
      `（显示时钟范围 [${a.material0}, ${a.material_span}]，钟速 ${a.clock}）`;
    block.appendChild(title);
    const list = state.anchorDraft[a.id] || [];
    list.forEach((pt, i) => {
      block.appendChild(anchorLine(a.id, i, pt));
    });
    const add = document.createElement("button");
    add.className = "mini"; add.textContent = "+ 锚点";
    add.addEventListener("click", () => {
      list.push({ material: "0", timeline: "0" });
      renderAnchorEditor();
    });
    block.appendChild(add);
    box.appendChild(block);
  }
}

function anchorLine(assetId, i, pt) {
  const line = document.createElement("div");
  line.className = "anchor-line";
  line.innerHTML =
    `<span>#${i + 1}</span>
     素材时刻 <input value="${pt.material}" data-f="material">
     ↔ 成片时刻 <input value="${pt.timeline}" data-f="timeline">
     <button class="mini">删除</button>`;
  line.querySelectorAll("input").forEach(inp => {
    inp.addEventListener("input", () => {
      state.anchorDraft[assetId][i][inp.dataset.f] = inp.value;
    });
  });
  line.querySelector("button").addEventListener("click", () => {
    state.anchorDraft[assetId].splice(i, 1);
    renderAnchorEditor();
  });
  return line;
}

// ------------------------------------------------------------------ cuts --

function renderCutsEditor() {
  const box = $("cutEditor");
  box.innerHTML = "";
  const videos = state.assets.filter(a => a.kind === "video");
  if (!videos.length) {
    box.innerHTML = '<p class="hint">先上传机位视频。</p>';
    return;
  }
  state.cuts.forEach((c, i) => {
    const line = document.createElement("div");
    line.className = "cut-line";
    const opts = videos.map(v =>
      `<option value="${v.id}" ${v.id === c.asset_id ? "selected" : ""}>机位 #${v.id} ${escapeHtml(v.filename)}</option>`
    ).join("");
    line.innerHTML =
      `[<input value="${c.start}" data-f="start">,
       <input value="${c.end}" data-f="end">) →
       <select data-f="asset_id">${opts}</select>
       <button class="mini">删除</button>`;
    line.querySelectorAll("input").forEach(inp => {
      inp.addEventListener("input", () => {
        state.cuts[i][inp.dataset.f] = inp.value;
      });
    });
    line.querySelector("select").addEventListener("change", (e) => {
      state.cuts[i].asset_id = Number(e.target.value);
    });
    line.querySelector("button").addEventListener("click", () => {
      state.cuts.splice(i, 1);
      renderCutsEditor();
    });
    box.appendChild(line);
  });
}

function collectEditPayload() {
  const anchors = {};
  for (const a of state.assets) {
    const list = state.anchorDraft[a.id] || [];
    anchors[a.id] = list.map(p => ({ material: p.material, timeline: p.timeline }));
  }
  return {
    duration: $("duration").value.trim(),
    anchors,
    cuts: state.cuts.map(c => ({
      start: c.start, end: c.end, asset_id: Number(c.asset_id),
    })),
  };
}

async function saveEdit() {
  try {
    const payload = collectEditPayload();
    const r = await api("/api/edit", { method: "PUT", body: JSON.stringify(payload) });
    showMsg($("editMsg"), `已保存：${r.frames} 帧 / ${r.samples} 采样，${r.width}x${r.height}`);
    await refresh();
    showFrame(Math.min(state.currentFrame, Math.max(0, state.totalFrames - 1)));
  } catch (e) {
    showMsg($("editMsg"), e.message, true);
  }
}

// -------------------------------------------------------------- prefill --

function prefill() {
  const videos = state.assets.filter(a => a.kind === "video");
  const audio = state.assets.find(a => a.kind === "audio");
  if (videos.length < 2 || !audio) {
    alert("样例参数需要至少 2 个机位视频和 1 个主音轨（可先运行 sample_generator.py 生成）。");
    return;
  }
  const endSec = (a) => {
    // displayed time of the last instant = material0 + clock*span
    const [n, d] = String(a.clock).includes("/")
      ? a.clock.split("/").map(Number) : [Number(a.clock), 1];
    const span = Number(a.material_span);
    return Number(a.material0) + (n / d) * span;
  };
  $("duration").value = "4";
  for (const a of state.assets) {
    state.anchorDraft[a.id] = [
      { material: a.material0, timeline: "0" },
      { material: String(+endSec(a).toFixed(6)), timeline: "4" },
    ];
  }
  // demo camera choices with black gaps:
  // [0,1) cam0  (1,4/3) black  [4/3, 7/3) cam1  (7/3, 12/5) black
  // [12/5, 3) cam0  (3,4) black
  state.cuts = [
    { start: "0", end: "1", asset_id: videos[0].id },
    { start: "4/3", end: "7/3", asset_id: videos[1].id },
    { start: "12/5", end: "3", asset_id: videos[0].id },
  ];
  renderAnchorEditor();
  renderCutsEditor();
}

// --------------------------------------------------------------- preview --

async function showFrame(i) {
  if (!state.totalFrames) return;
  i = Math.max(0, Math.min(state.totalFrames - 1, i));
  state.currentFrame = i;
  const bust = Date.now();
  $("previewImg").src = `/api/preview/${i}?t=${bust}`;
  try {
    const rec = await api(`/api/preview/${i}?rec=1&t=${bust}`);
    $("frameInfo").textContent =
      `帧 ${i}/${state.totalFrames - 1}  T=${rec.target_time}s`;
    $("frameRecord").textContent = JSON.stringify(rec, null, 2);
  } catch (e) { /* ignore */ }
}

function togglePlay() {
  if (state.playing) { stopPlay(); return; }
  state.playing = true;
  $("btnPlay").textContent = "暂停";
  let last = performance.now();
  let acc = 0;
  const tick = (now) => {
    if (!state.playing) return;
    const dt = now - last; last = now;
    acc += dt;
    const frameMs = 1000 / FPS;
    while (acc >= frameMs) {
      acc -= frameMs;
      if (state.currentFrame >= state.totalFrames - 1) { stopPlay(); return; }
      state.currentFrame++;
    }
    showFrame(state.currentFrame);
    state.playTimer = requestAnimationFrame(tick);
  };
  state.playTimer = requestAnimationFrame(tick);
}
function stopPlay() {
  state.playing = false;
  if (state.playTimer) cancelAnimationFrame(state.playTimer);
  $("btnPlay").textContent = "播放 / 暂停";
}

// ---------------------------------------------------------------- export --

async function startExport() {
  await saveEditQuiet();
  showMsg($("exportMsg"), "");
  try {
    const r = await api("/api/export/start", { method: "POST" });
    state.currentJob = r.job_id;
    pollExport();
  } catch (e) { showMsg($("exportMsg"), e.message, true); }
}

async function saveEditQuiet() {
  const payload = collectEditPayload();
  await api("/api/edit", { method: "PUT", body: JSON.stringify(payload) });
  await refresh();
}

async function pollExport() {
  if (!state.currentJob) return;
  const j = await api(`/api/export/${state.currentJob}`);
  showMsg($("exportMsg"),
    j.status === "running" ? "导出中……（导出期间仍可继续编辑当前项目）"
    : j.status === "done" ? "导出完成。"
    : j.status === "canceled" ? "已取消（未发布任何文件）。"
    : `导出失败：${j.error || ""}`,
    j.status === "failed");
  const s = await api("/api/state");
  renderExports(s.exports);
  if (j.status === "running") setTimeout(pollExport, 700);
}

async function cancelExport() {
  if (!state.currentJob) return;
  await api(`/api/export/${state.currentJob}/cancel`, { method: "POST" });
}

function renderExports(exports) {
  const tb = $("exportTable").querySelector("tbody");
  tb.innerHTML = "";
  for (const j of exports) {
    const tr = document.createElement("tr");
    const dl = j.status === "done"
      ? `<a href="${j.status === "done" ? `/api/export/${j.id}/mp4` : "#"}">MP4</a>`
      : "—";
    const mp = j.status === "done"
      ? `<a href="/api/export/${j.id}/mapping">mapping.json</a>` : "—";
    tr.innerHTML =
      `<td>${j.id.slice(0, 8)}</td>
       <td class="status-${j.status}">${j.status}</td>
       <td>${new Date(j.created_at * 1000).toLocaleTimeString()}</td>
       <td>${j.finished_at ? new Date(j.finished_at * 1000).toLocaleTimeString() : "—"}</td>
       <td>${dl}</td><td>${mp}</td>
       <td>${escapeHtml(j.error || "")}</td>`;
    tb.appendChild(tr);
  }
}

// ----------------------------------------------------------------- init --

$("upVideo").addEventListener("change", async (e) => {
  await upload([...e.target.files], "video"); e.target.value = "";
});
$("upAudio").addEventListener("change", async (e) => {
  await upload([...e.target.files], "audio"); e.target.value = "";
});
$("btnPrefill").addEventListener("click", prefill);
$("btnValidate").addEventListener("click", saveEdit);
$("btnSave").addEventListener("click", saveEdit);
$("btnAddCut").addEventListener("click", () => {
  const v = state.assets.find(a => a.kind === "video");
  if (!v) { alert("先上传机位视频"); return; }
  state.cuts.push({ start: "0", end: "1", asset_id: v.id });
  renderCutsEditor();
});
$("btnPrev").addEventListener("click", () => { stopPlay(); showFrame(state.currentFrame - 1); });
$("btnNext").addEventListener("click", () => { stopPlay(); showFrame(state.currentFrame + 1); });
$("btnPlay").addEventListener("click", togglePlay);
$("btnJump").addEventListener("click", () => {
  stopPlay(); showFrame(Number($("jumpFrame").value || 0));
});
$("btnExport").addEventListener("click", startExport);
$("btnCancelExport").addEventListener("click", cancelExport);

refresh().then(() => showFrame(0)).catch(() => {});
