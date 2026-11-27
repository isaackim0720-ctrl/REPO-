// Liver HU lab website - browser side (no external libraries).
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, nd = 1) => (v === null || v === undefined || Number.isNaN(v)) ? "–" : Number(v).toFixed(nd);
const mins = (s) => s < 90 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`;

async function getJSON(url, opts) {
  const r = await fetch(url, Object.assign({ credentials: "same-origin" }, opts || {}));
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

// ---------- collecting files (picker or drag-and-drop, incl. folders) ----------
let picked = [];   // [{file, path}]

function setPicked(list) {
  picked = list.filter((x) => !x.file.name.startsWith("."));
  const bytes = picked.reduce((a, x) => a + x.file.size, 0);
  $("picked").textContent = picked.length ? `${picked.length} file(s) selected, ${(bytes / 1e6).toFixed(0)} MB` : "";
  $("go").disabled = picked.length === 0;
}

function readEntry(entry, prefix) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((f) => resolve([{ file: f, path: prefix + f.name }]), () => resolve([]));
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const all = [];
      const readBatch = () => reader.readEntries(async (entries) => {
        if (!entries.length) {
          const nested = await Promise.all(all.map((e) => readEntry(e, prefix + entry.name + "/")));
          resolve(nested.flat());
        } else { all.push(...entries); readBatch(); }
      }, () => resolve([]));
      readBatch();
    } else resolve([]);
  });
}

// ---------- upload in batches ----------
function sendBatch(jobId, batch, onProgress) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    for (const x of batch) fd.append("files", x.file, x.path);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/jobs/${jobId}/files`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded);
    xhr.onload = () => (xhr.status === 200 ? resolve() : reject(new Error(xhr.status + " " + xhr.responseText)));
    xhr.onerror = () => reject(new Error("network error during upload"));
    xhr.send(fd);
  });
}

async function startUpload() {
  $("go").disabled = true;
  $("upbox").hidden = false;
  const total = picked.reduce((a, x) => a + x.file.size, 0) || 1;
  try {
    const fd = new FormData();
    fd.append("label", $("label").value);
    fd.append("roi_diameter_mm", $("diam").value);
    fd.append("edge_margin_mm", $("edge").value);
    fd.append("ligament_margin_mm", $("lig").value);
    fd.append("preferred_slice_mm", $("slice").value);
    if ($("ident").checked) fd.append("include_identifiers", "on");
    if ($("saveseg").checked) fd.append("save_segmentation", "on");
    fd.append("keep_edit_data", $("keepedit").checked ? "on" : "off");
    const { job_id } = await getJSON("/api/jobs", { method: "POST", body: fd });
    let sent = 0, batch = [], batchBytes = 0;
    const flush = async () => {
      if (!batch.length) return;
      const base = sent;
      await sendBatch(job_id, batch, (loaded) => {
        const pct = Math.min(100, (100 * (base + loaded)) / total);
        $("bar").style.width = pct + "%";
        $("upmsg").textContent = `Uploading… ${pct.toFixed(0)}%`;
      });
      sent += batchBytes; batch = []; batchBytes = 0;
    };
    for (const x of picked) {
      batch.push(x); batchBytes += x.file.size;
      if (batch.length >= 300 || batchBytes >= 150e6) await flush();
    }
    await flush();
    await getJSON(`/api/jobs/${job_id}/start`, { method: "POST" });
    $("upmsg").textContent = "Upload complete - starting measurement…";
    location.href = `/jobs/${job_id}`;
  } catch (e) {
    $("upmsg").innerHTML = `<span class="error">Upload failed: ${esc(e.message)}</span>`;
    $("go").disabled = false;
  }
}

// ---------- index page ----------
function initIndex() {
  const drop = $("drop");
  $("pickFolder").onchange = (e) => setPicked([...e.target.files].map((f) => ({ file: f, path: f.webkitRelativePath || f.name })));
  $("pickFiles").onchange = (e) => setPicked([...e.target.files].map((f) => ({ file: f, path: f.name })));
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = async (e) => {
    e.preventDefault(); drop.classList.remove("over");
    const items = [...(e.dataTransfer.items || [])].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    if (items.length) {
      $("picked").textContent = "Reading folder…";
      setPicked((await Promise.all(items.map((it) => readEntry(it, "")))).flat());
    } else setPicked([...e.dataTransfer.files].map((f) => ({ file: f, path: f.name })));
  };
  $("go").onclick = startUpload;
  refreshJobs();
  setInterval(refreshJobs, 4000);
}

async function refreshJobs() {
  try {
    const d = await getJSON("/api/jobs");
    const rows = d.jobs.map((j) => {
      const pct = j.n_cases ? Math.round((100 * j.n_done) / j.n_cases) : 0;
      return `<tr>
        <td>${j.id}</td><td><a href="/jobs/${j.id}">${esc(j.label || "(no name)")}</a></td>
        <td>${esc(j.submitted_by)}</td><td class="small">${esc((j.created_at || "").replace("T", " "))}</td>
        <td><span class="badge ${esc(j.status)}">${esc(j.status)}</span></td>
        <td class="small">${j.n_cases ? `${j.n_done}/${j.n_cases} scans (${pct}%)` : esc(j.status === "uploading" ? `${j.n_files} files received` : j.message || "")}</td>
        <td><a href="/jobs/${j.id}">Open</a> · <a href="#" onclick="delJob(${j.id});return false" class="small">Delete</a></td></tr>`;
    });
    $("jobs").innerHTML = rows.join("") || `<tr><td colspan="7" class="muted">No uploads yet.</td></tr>`;
    $("queue").textContent = d.cases_waiting ? `${d.cases_waiting} scan(s) waiting` + (d.avg_seconds_per_case ? ` · about ${mins(d.cases_waiting * d.avg_seconds_per_case)} left` : "") : "Idle";
  } catch (e) { /* keep last view */ }
}

async function delJob(id) {
  if (!confirm(`Delete upload #${id} and all its results? This cannot be undone.`)) return;
  await getJSON(`/api/jobs/${id}/delete`, { method: "POST" });
  if (location.pathname.startsWith("/jobs/")) location.href = "/"; else refreshJobs();
}

// ---------- job page ----------
const ROIS = [["LL", "Left lobe", "roi-ll"], ["RL1", "Right lobe 1 (front)", "roi-rl1"], ["RL2", "Right lobe 2 (back)", "roi-rl2"], ["SPL", "Spleen", "roi-spl"]];

function caseCard(c) {
  const done = ["ok", "partial", "failed"].includes(c.status);
  const rows = ROIS.map(([k, name, cls]) => {
    const r = c[k] || {};
    if (!done) return "";
    if (r.placed === undefined || r.placed === null) return "";   // e.g. spleen not measured in older results
    if (!r.placed) return `<tr><td class="${cls}">${k}</td><td colspan="7" class="error">not placed</td></tr>`;
    const where = k === "SPL" ? "spleen" : `seg ${esc(r.segment_at_center)} · ${esc(r.side_of_ligament)}${r.side_ok ? "" : " ⚠"}`;
    return `<tr><td class="${cls}" title="${esc(name)}">${k}</td>
      <td class="num">${fmt(r.mean_hu)}${r.hu_check ? ` <span class="huwarn" title="${r.hu_check === "low" ? "Too low" : "Too high"}${r.hu_range ? `: expected ${fmt(r.hu_range[0], 0)}–${fmt(r.hu_range[1], 0)} HU` : ""}">⚠ ${r.hu_check === "low" ? "low" : "high"}</span>` : ""}</td><td class="num">${fmt(r.sd_hu)}</td>
      <td class="num">${fmt(r.min_hu, 0)}</td><td class="num">${fmt(r.max_hu, 0)}</td>
      <td class="num">${fmt(r.area_cm2, 2)}</td><td class="num small">${fmt(r.z_mm)}${r.dicom_instance ? ` (#${r.dicom_instance})` : ""}</td>
      <td class="small">${where}${r.manual ? ` · <b title="moved by hand">✎ ${esc(r.edited_by || "")}</b>` : ""}</td></tr>`;
  }).join("");
  const flags = (c.flags || []).length ? `<ul class="flags">${c.flags.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : "";
  const qBadge = { good: ["ok", "✓ Good quality"], check: ["partial", "⚠ Check"], poor: ["failed", "✕ Poor quality"] }[c.quality] || null;
  const notes = [...(c.completeness_notes || []), ...(c.quality_notes || [])];
  const checks = done && c.quality ? `<p class="small">${qBadge ? `<span class="badge ${qBadge[0]}">${qBadge[1]}</span>` : ""}
      ${c.scan_complete === false ? ` <span class="badge failed">Incomplete scan</span>` : ""}
      <span class="muted"> · ${esc(c.contrast_phase || "")}${c.contrast_reason ? ` (${esc(c.contrast_reason)})` : ""}</span></p>
      ${notes.length ? `<ul class="small qnotes">${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}` : "";
  const img = c.has_qa ? `<img src="/cases/${c.id}/qa.png?r=${c.revision || 0}" alt="QA image for ${esc(c.case_id)}" onclick="openImg(this.src)">`
    : `<div class="muted small">${done ? "No image" : "Image appears when the scan is finished."}</div>`;
  const elapsed = c.status === "running" && c.started_at ? Math.max(0, (Date.now() - Date.parse(c.started_at)) / 1000) : null;
  const status = c.status === "running" ? `${esc(c.step || "starting")}${elapsed !== null ? ` · ${mins(elapsed)} so far` : ""}` : c.status;
  return `<section class="card"><div class="row" style="justify-content:space-between;align-items:center">
      <h2>${esc(c.case_id)}</h2>
      <span><span class="badge ${esc(c.status)}">${esc(status)}</span>
      ${c.seconds ? `<span class="small muted">${mins(c.seconds)}</span>` : ""}</span></div>
    <div class="case"><div>${img}</div><div>
      ${done && c.status !== "failed" ? `<p class="small muted">Liver mean of 3 circles: <b>${fmt(c.liver_mean)} HU</b>${c.spleen_mean !== null && c.spleen_mean !== undefined ? ` · spleen <b>${fmt(c.spleen_mean)} HU</b> · liver − spleen <b>${fmt(c.l_minus_s)} HU</b> · liver/spleen <b>${fmt(c.l_s_ratio, 2)}</b>` : ""} · series: ${esc(c.series || "?")} · slice ${fmt(c.slice_mm, 2)} mm</p>` : ""}
      ${checks}
      ${c.error ? `<p class="error small">${esc(c.error)}</p>` : ""}
      ${rows ? `<div class="table-wrap"><table><thead><tr><th>ROI</th><th class="num">Mean HU</th><th class="num">SD</th><th class="num">Min</th><th class="num">Max</th><th class="num">Area cm²</th><th class="num">z mm (image)</th><th>Where</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      ${flags}
      ${done ? `<p class="small">${c.editable ? `<a class="btn" href="/cases/${c.id}/edit">Adjust circles</a> &nbsp;` : ""}<a href="/cases/${c.id}/result.json" target="_blank">all values (JSON)</a> · <a href="/cases/${c.id}/log.txt" target="_blank">log</a></p>` : ""}
    </div></div></section>`;
}

function openImg(src) { const lb = $("lightbox"); lb.querySelector("img").src = src; lb.style.display = "flex"; }

let lastSig = "";
async function refreshJob(id) {
  const d = await getJSON(`/api/jobs/${id}`);
  const j = d.job;
  $("status").className = "badge " + j.status;
  $("status").textContent = j.status;
  const pct = j.n_cases ? (100 * j.n_done) / j.n_cases : 0;
  $("bar").style.width = pct + "%";
  let p = j.n_cases ? `${j.n_done} of ${j.n_cases} scan(s) measured` : (j.status === "queued" ? "waiting for the computer…" : "");
  const left = j.n_cases - j.n_done;
  if (left > 0 && d.avg_seconds_per_case) p += ` · about ${mins((left + d.cases_ahead) * d.avg_seconds_per_case)} left`;
  if (d.cases_ahead) p += ` · ${d.cases_ahead} scan(s) from earlier uploads first`;
  $("progress").textContent = p;
  $("message").textContent = j.message || "";
  const sig = JSON.stringify(d.cases.map((c) => [c.id, c.status, c.step, c.has_qa, c.revision, c.status === "running" ? Date.now() : 0]));
  if (sig !== lastSig) { $("cases").innerHTML = d.cases.map(caseCard).join(""); lastSig = sig; }
  return ["done", "failed", "cancelled"].includes(j.status);
}

function initJob(id) {
  const tick = async () => {
    let finished = false;
    try { finished = await refreshJob(id); } catch (e) { /* retry */ }
    if (!finished) setTimeout(tick, 4000);
  };
  tick();
}

// ---------- circle editor ----------
const ROI_COLORS = { LL: "#00e5ff", RL1: "#ff4dd2", RL2: "#ffb000", SPL: "#4ade80" };

async function initEditor(caseId) {
  const info = await getJSON(`/api/cases/${caseId}/edit-info`);
  if (!info.available) {
    $("editor").innerHTML = `<section class="card"><p>This scan can't be edited: it was measured before the editor existed
      (or with editing turned off), so its image data wasn't kept. Upload the scan again to adjust its circles.</p></section>`;
    return;
  }
  const [X, Y, Z] = info.shape, [sx, sy, sz] = info.spacing;
  const W = X * sx, H = Y * sy;                       // image size in mm (display)
  const st = {};                                      // per-circle working state
  for (const r of info.rois) st[r.key] = { ...r, orig: { x: r.x, y: r.y, z: r.z, d: r.diameter_mm }, d: r.diameter_mm };
  let key = info.rois[0].key, timer = null, dragging = false, lastZ = -1;
  const ov = $("ov"), img = $("slice");
  ov.setAttribute("viewBox", `0 0 ${W} ${H}`);
  $("zslider").max = Z - 1;

  const tabs = () => {
    $("tabs").innerHTML = info.rois.map((r) => `<button type="button" data-k="${r.key}" class="${r.key === key ? "active" : ""}">
      <span style="color:${r.key === key ? "inherit" : ROI_COLORS[r.key]}">●</span> ${esc(r.name)}${st[r.key].manual ? '<span class="dot">✎</span>' : ""}${st[r.key].placed ? "" : '<span class="dot">(not placed)</span>'}</button>`).join("");
    $("tabs").querySelectorAll("button").forEach((b) => (b.onclick = () => { key = b.dataset.k; draw(true); }));
  };
  const toMm = (x, y) => [(X - 1 - x + 0.5) * sx, (Y - 1 - y + 0.5) * sy];
  const fromEvent = (e) => {
    const rect = ov.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / rect.width) * W, my = ((e.clientY - rect.top) / rect.height) * H;
    return [X - 1 - Math.floor(mx / sx), Y - 1 - Math.floor(my / sy)];
  };
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function draw(full) {
    const s = st[key];
    if (s.z !== lastZ) { img.src = `/cases/${caseId}/slice/${s.z}.png`; lastZ = s.z; }
    const [cx, cy] = toMm(s.x, s.y);
    ov.innerHTML = `<circle cx="${cx}" cy="${cy}" r="${s.d / 2}" fill="none" stroke="${ROI_COLORS[key]}" stroke-width="${Math.max(W, H) / 250}"/>
      <text x="${cx}" y="${cy - s.d / 2 - 2}" fill="${ROI_COLORS[key]}" font-size="${Math.max(W, H) / 35}" text-anchor="middle" font-weight="700">${key}</text>`;
    $("zslider").value = s.z;
    $("diam").value = s.d;
    if (full) {
      tabs();
      $("roiname").textContent = s.name;
      $("reset").hidden = !s.manual;
      const a = s.auto;
      for (const [id, f, nd] of [["mean", "mean_hu", 1], ["sd", "sd_hu", 1], ["min", "min_hu", 0], ["max", "max_hu", 0], ["area", "area_cm2", 2]])
        $("a_" + id).textContent = a && a[f] !== null && a[f] !== undefined ? `automatic: ${fmt(a[f], nd)}` : "";
      $("autonote").textContent = s.manual ? `This circle was moved by hand${s.edited_by ? " by " + s.edited_by : ""}.` : (s.placed ? "Automatic circle." : `Not placed automatically${s.reason ? " (" + s.reason + ")" : ""}; place it by hand if appropriate.`);
      $("status").textContent = "";
    }
    clearTimeout(timer);
    timer = setTimeout(measure, 120);
  }

  async function measure() {
    const s = st[key];
    try {
      const m = await getJSON(`/api/cases/${caseId}/measure?key=${key}&x=${s.x}&y=${s.y}&z=${s.z}&d=${s.d}`);
      s.x = m.x; s.y = m.y; s.z = m.z;          // server keeps the circle inside the image
      const rng = (info.hu_ranges || {})[key];
      const hw = rng && m.mean_hu < rng[0] ? "low" : (rng && m.mean_hu > rng[1] ? "high" : "");
      $("r_mean").innerHTML = esc(fmt(m.mean_hu) + " HU") + (hw ? ` <span class="huwarn" title="expected ${fmt(rng[0], 0)}–${fmt(rng[1], 0)} HU (${esc(info.contrast_phase || "")})">⚠ ${hw}</span>` : "");
      $("r_sd").textContent = fmt(m.sd_hu);
      $("r_min").textContent = fmt(m.min_hu, 0); $("r_max").textContent = fmt(m.max_hu, 0);
      $("r_area").textContent = fmt(m.area_cm2, 2) + " cm²";
      $("r_where").textContent = (key === "SPL" ? "spleen" : `segment ${m.segment_at_center} · ${m.side_of_ligament} of ligament`) +
        ` · z ${fmt(m.z_mm)} mm` + (m.dicom_instance ? ` (image ${m.dicom_instance})` : "");
      $("zinfo").textContent = `Slice ${s.z + 1} of ${Z}`;
      const w = m.warnings || [];
      $("warn").hidden = !w.length;
      $("warn").innerHTML = w.map((t) => `<li>${esc(t)}</li>`).join("");
    } catch (e) { $("status").innerHTML = `<span class="error">${esc(e.message)}</span>`; }
  }

  const moveTo = (e) => { const [x, y] = fromEvent(e); const s = st[key]; s.x = clamp(x, 0, X - 1); s.y = clamp(y, 0, Y - 1); draw(false); };
  ov.addEventListener("pointerdown", (e) => { dragging = true; ov.setPointerCapture(e.pointerId); moveTo(e); });
  ov.addEventListener("pointermove", (e) => { if (dragging) moveTo(e); });
  ov.addEventListener("pointerup", () => { dragging = false; });
  const setZ = (z) => { st[key].z = clamp(z, 0, Z - 1); draw(false); };
  $("up").onclick = () => setZ(st[key].z + 1);
  $("down").onclick = () => setZ(st[key].z - 1);
  $("zslider").oninput = (e) => setZ(parseInt(e.target.value, 10));
  const setD = (d) => { st[key].d = clamp(Math.round(d * 2) / 2, 3, 60); draw(false); };
  $("diam").onchange = (e) => setD(parseFloat(e.target.value) || st[key].d);
  $("dminus").onclick = () => setD(st[key].d - 1);
  $("dplus").onclick = () => setD(st[key].d + 1);
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    const s = st[key];
    const moves = { ArrowLeft: [1, 0], ArrowRight: [-1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] };   // screen -> patient axes
    if (moves[e.key]) { s.x = clamp(s.x + moves[e.key][0], 0, X - 1); s.y = clamp(s.y + moves[e.key][1], 0, Y - 1); draw(false); e.preventDefault(); }
    else if (e.key === "PageUp") { setZ(s.z + 1); e.preventDefault(); }
    else if (e.key === "PageDown") { setZ(s.z - 1); e.preventDefault(); }
  });
  $("undo").onclick = () => { const s = st[key]; Object.assign(s, { x: s.orig.x, y: s.orig.y, z: s.orig.z, d: s.orig.d }); draw(true); };
  $("save").onclick = async () => {
    const s = st[key];
    $("save").disabled = true; $("status").textContent = "Saving…";
    try {
      const r = await getJSON(`/api/cases/${caseId}/rois/${key}`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x: s.x, y: s.y, z: s.z, d: s.d }) });
      const fresh = await getJSON(`/api/cases/${caseId}/edit-info`);
      for (const f of fresh.rois) st[f.key] = { ...f, orig: { x: f.x, y: f.y, z: f.z, d: f.diameter_mm }, d: f.diameter_mm };
      draw(true);
      $("status").innerHTML = `<b>Saved.</b> Liver mean (3 circles) is now ${fmt(r.liver_mean)} HU` + (r.spleen_mean !== null ? `, spleen ${fmt(r.spleen_mean)} HU.` : ".");
    } catch (e) { $("status").innerHTML = `<span class="error">Could not save: ${esc(e.message)}</span>`; }
    $("save").disabled = false;
  };
  $("reset").onclick = async () => {
    if (!confirm("Put this circle back where the automatic method placed it?")) return;
    await getJSON(`/api/cases/${caseId}/rois/${key}/reset`, { method: "POST" });
    const fresh = await getJSON(`/api/cases/${caseId}/edit-info`);
    for (const f of fresh.rois) st[f.key] = { ...f, orig: { x: f.x, y: f.y, z: f.z, d: f.diameter_mm }, d: f.diameter_mm };
    draw(true);
    $("status").innerHTML = "<b>Reset to the automatic circle.</b>";
  };
  draw(true);
}
