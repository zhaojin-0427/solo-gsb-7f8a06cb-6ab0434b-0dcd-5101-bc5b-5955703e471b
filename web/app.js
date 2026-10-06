// 多机位同步剪辑 — 原生 JS 前端
const $ = (id) => document.getElementById(id);
const ROLES = ["A", "B", "master"];
const ROLE_LABEL = { A: "机位 A", B: "机位 B", master: "主音轨" };

let state = null;
let currentFrame = 0;

function msg(text, kind = "") {
  const el = $("messages");
  el.innerHTML = `<span class="${kind}">${text}</span>`;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 200 ? res.json() : null;
}

// ----------------------------------------------------------- anchor editors

function anchorEditorsHtml() {
  return ROLES.map((role) => {
    const ans = (state?.anchors[role]) || [];
    const rows = ans.map(([m, o], i) => `
      <div class="anchor-row" data-role="${role}">
        <input data-k="m" type="text" value="${m}" placeholder="素材时刻">
        <input data-k="o" type="text" value="${o}" placeholder="成片时刻">
        <button class="danger" data-del="${role}" data-i="${i}">×</button>
      </div>`).join("");
    return `<div style="margin-top:8px">
      <span class="tag ${role}">${ROLE_LABEL[role]}</span>
      <div data-anchorrows="${role}">${rows}</div>
      <button class="secondary" data-addanchor="${role}" style="margin-top:4px">+ 锚点</button>
      <div class="hint">至少两个，素材时刻与成片时刻均严格递增；段内线性，范围外无素材。</div>
    </div>`;
  }).join("");
}

function cutsHtml() {
  const cuts = state?.cuts || [];
  return cuts.map(([s, e, cam], i) => `
    <div class="anchor-row cut-row">
      <input type="text" data-cut="s" data-i="${i}" value="${s}">
      <input type="text" data-cut="e" data-i="${i}" value="${e}">
      <select data-cut="c" data-i="${i}" style="background:#17181d;color:#fff;border:1px solid #34373f;border-radius:6px;padding:6px">
        <option value="A" ${cam === "A" ? "selected" : ""}>A</option>
        <option value="B" ${cam === "B" ? "selected" : ""}>B</option>
      </select>
      <button class="danger" data-delcut="${i}">×</button>
    </div>`).join("");
}

function renderEditor() {
  $("duration").value = state.duration;
  $("anchorEditors").innerHTML = anchorEditorsHtml();
  $("cutList").innerHTML = cutsHtml();

  const meta = (a, extra) => a
    ? `${a.fps ? a.fps + " fps · " : ""}${a.frame_count != null ? a.frame_count + " 帧" : ""}`
      + `${a.sample_rate ? a.sample_rate + " Hz · " + a.channels + "ch · " + a.samples + " 采样" : ""}${extra || ""}`
    : "未上传";
  $("metaA").textContent = meta(state.assets.A);
  $("metaB").textContent = meta(state.assets.B);
  $("metaM").textContent = meta(state.assets.master);
  $("canvasSpec").textContent = state.canvas[0] ? `画布：${state.canvas[0]}×${state.canvas[1]}` : "画布：未确定";
  $("exportReady").textContent = state.ready ? "可以导出" : (state.problems.join("；") || "");

  // Half-open cells: frame n covers [n/30, (n+1)/30); count = ceil(d*30 - 1/2).
  const d = fracToFloat(state.duration);
  const n = Math.max(0, Math.ceil(d * 30 - 0.5));
  const slider = $("frameSlider");
  slider.max = Math.max(0, n - 1);
  if (currentFrame > n - 1) currentFrame = Math.max(0, n - 1);
  slider.value = currentFrame;
  if (state.errors.length) msg(state.errors.join("；"), "warn");
}

// "n/d" or decimal -> display string numeric
function fracToFloat(s) {
  if (typeof s === "number") return s;
  if (s.includes("/")) {
    const [a, b] = s.split("/");
    return Number(a) / Number(b);
  }
  return Number(s);
}

function collectEdits() {
  const anchors = {};
  for (const role of ROLES) {
    anchors[role] = [];
    document.querySelectorAll(`[data-anchorrows="${role}"] .anchor-row`).forEach((row) => {
      const m = row.querySelector('[data-k="m"]').value.trim();
      const o = row.querySelector('[data-k="o"]').value.trim();
      if (m || o) anchors[role].push([m, o]);
    });
  }
  const cuts = [];
  document.querySelectorAll("#cutList .cut-row").forEach((row) => {
    const s = row.querySelector('[data-cut="s"]').value.trim();
    const e = row.querySelector('[data-cut="e"]').value.trim();
    const c = row.querySelector('[data-cut="c"]').value;
    if (s && e) cuts.push([s, e, c]);
  });
  return { duration: $("duration").value.trim() || "0", anchors, cuts };
}

// -------------------------------------------------------------- uploads

async function upload(role, file) {
  if (!file) return;
  msg(`上传 ${ROLE_LABEL[role]} …`);
  const fd = new FormData();
  fd.append("role", role);
  fd.append("file", file);
  await api("/api/upload", { method: "POST", body: fd });
  await refresh();
  msg(`${ROLE_LABEL[role]} 上传并解析完成`, "ok");
}

$("fileA").addEventListener("change", (e) => upload("A", e.target.files[0]));
$("fileB").addEventListener("change", (e) => upload("B", e.target.files[0]));
$("fileM").addEventListener("change", (e) => upload("master", e.target.files[0]));

// -------------------------------------------------------------- edit events

$("anchorEditors").addEventListener("click", (e) => {
  const add = e.target.getAttribute?.("data-addanchor");
  const del = e.target.getAttribute?.("data-del");
  if (add) {
    state.anchors[add] = state.anchors[add] || [];
    state.anchors[add].push(["", ""]);
    renderEditor();
  }
  if (del) {
    state.anchors[del].splice(Number(e.target.dataset.i), 1);
    renderEditor();
  }
});

$("addCut").addEventListener("click", () => {
  state.cuts.push(["0", state.duration, "A"]);
  renderEditor();
});

$("cutList").addEventListener("click", (e) => {
  const i = e.target.getAttribute?.("data-delcut");
  if (i != null && e.target.hasAttribute("data-delcut")) {
    state.cuts.splice(Number(i), 1);
    renderEditor();
  }
});

$("save").addEventListener("click", async () => {
  try {
    const body = collectEdits();
    const newState = await api("/api/state", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state = newState;
    renderEditor();
    await showFrame(currentFrame);
    msg(`剪辑已保存（版本 ${state.edit_version}）`, "ok");
  } catch (err) {
    msg("保存失败：" + err.message, "err");
  }
});

// -------------------------------------------------------------- preview

async function showFrame(n) {
  const total = Number($("frameSlider").max) + 1;
  if (!total) {
    $("frameInfo").textContent = "时长为 0，无帧";
    return;
  }
  n = Math.max(0, Math.min(total - 1, n));
  currentFrame = n;
  $("frameSlider").value = n;
  const v = state.edit_version || 0;
  $("frameImg").src = `/preview/frame/${n}.png?v=${v}&t=${Date.now()}`;
  try {
    const info = await api(`/api/frame-info?n=${n}`);
    const outSec = fracToFloat(info.output_time).toFixed(6);
    const matSec = info.material_time != null ? fracToFloat(info.material_time).toFixed(6) : "—";
    $("frameInfo").textContent =
      `输出帧      #${info.frame} / ${total - 1}\n` +
      `成片时刻    ${info.output_time} s  (${outSec})\n` +
      `机位        ${info.camera || "（补黑）"}\n` +
      `素材时刻    ${info.material_time ?? "—"} s  (${matSec})\n` +
      `来源显示帧  ${info.source_frame ?? "—"}   源帧率 ${info.source_fps ?? "—"}\n` +
      (info.fill ? `填充原因    ${info.fill === "no-camera" ? "该时刻未选机位 → 黑帧" : "超出锚点范围 → 黑帧"}`
                 : "映射        有效");
  } catch (err) {
    $("frameInfo").textContent = err.message;
  }
}

$("prevFrame").addEventListener("click", () => showFrame(currentFrame - 1));
$("nextFrame").addEventListener("click", () => showFrame(currentFrame + 1));
$("frameSlider").addEventListener("input", (e) => showFrame(Number(e.target.value)));
$("jumpBtn").addEventListener("click", () => {
  const t = fracToFloat($("jumpTime").value);
  if (Number.isFinite(t)) showFrame(Math.floor(t * 30));
});
window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.key === "ArrowLeft") showFrame(currentFrame - 1);
  if (e.key === "ArrowRight") showFrame(currentFrame + 1);
});

$("reloadAudio").addEventListener("click", () => {
  $("audioPlayer").src = `/preview/audio.wav?v=${state.edit_version}&t=${Date.now()}`;
});

// -------------------------------------------------------------- export

$("exportBtn").addEventListener("click", async () => {
  try {
    const job = await api("/api/export", { method: "POST" });
    msg(`导出任务 ${job.id} 已启动（绑定当前快照）`, "ok");
    pollJob(job.id);
  } catch (err) {
    msg("无法导出：" + err.message, "err");
  }
});

async function pollJob(id) {
  const timer = setInterval(async () => {
    const j = await api(`/api/jobs/${id}`);
    renderJobs();
    if (j.status !== "running") {
      clearInterval(timer);
      msg(`任务 ${id}：${j.status === "done" ? "完成" : j.status === "cancelled" ? "已取消" : "失败：" + j.message}`,
          j.status === "done" ? "ok" : j.status === "cancelled" ? "warn" : "err");
    }
  }, 700);
}

async function renderJobs() {
  const jobs = await api("/api/jobs");
  $("jobList").innerHTML = jobs.slice(0, 8).map((j) => `
    <div class="job">
      <div>${j.id} · ${j.status}${j.message ? " · " + j.message : ""}</div>
      <div class="bar"><div style="width:${Math.round(j.progress * 100)}%"></div></div>
      ${j.status === "running" ? `<button class="danger" onclick="cancelJob_('${j.id}')">取消</button>` : ""}
      ${j.mp4_url ? `<a href="${j.mp4_url}">下载 MP4</a> ·
        <a href="${j.manifest_url}">帧/采样→来源映射</a> ·
        <a href="${j.snapshot_url}">绑定快照</a>` : ""}
    </div>`).join("");
}

window.cancelJob_ = async (id) => {
  await api(`/api/jobs/${id}/cancel`, { method: "POST" });
  renderJobs();
};

// -------------------------------------------------------------- bootstrap

async function refresh() {
  state = await api("/api/state");
  state.anchors = state.anchors || {};
  renderEditor();
  await showFrame(currentFrame);
  renderJobs();
}

refresh().catch((e) => msg(e.message, "err"));
