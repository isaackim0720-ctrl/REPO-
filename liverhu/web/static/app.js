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
const ROIS = [["LL", "Left lobe", "roi-ll"], ["RL1", "Right lobe 1 (upper)", "roi-rl1"], ["RL2", "Right lobe 2 (lower)", "roi-rl2"], ["SPL", "Spleen", "roi-spl"]];

function caseCard(c) {
  const done = ["ok", "partial", "failed"].includes(c.status);
  const rows = ROIS.map(([k, name, cls]) => {
    const r = c[k] || {};
    if (!done) return "";
    if (r.placed === undefined || r.placed === null) return "";   // e.g. spleen not measured in older results
    if (!r.placed) return `<tr><td class="${cls}">${k}</td><td colspan="7" class="error">not placed</td></tr>`;
    const where = k === "SPL" ? "spleen" : `seg ${esc(r.segment_at_center)} · ${esc(r.side_of_ligament)}${r.side_ok ? "" : " ⚠"}`;
    return `<tr><td class="${cls}" title="${esc(name)}">${k}</td>
      <td class="num">${fmt(r.mean_hu)}</td><td class="num">${fmt(r.sd_hu)}</td>
      <td class="num">${fmt(r.min_hu, 0)}</td><td class="num">${fmt(r.max_hu, 0)}</td>
      <td class="num">${fmt(r.area_cm2, 2)}</td><td class="num small">${fmt(r.z_mm)}${r.dicom_instance ? ` (#${r.dicom_instance})` : ""}</td>
      <td class="small">${where}</td></tr>`;
  }).join("");
  const flags = (c.flags || []).length ? `<ul class="flags">${c.flags.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : "";
  const img = c.has_qa ? `<img src="/cases/${c.id}/qa.png" alt="QA image for ${esc(c.case_id)}" onclick="openImg(this.src)">`
    : `<div class="muted small">${done ? "No image" : "Image appears when the scan is finished."}</div>`;
  const status = c.status === "running" ? `running: ${esc(c.step)}` : c.status;
  return `<section class="card"><div class="row" style="justify-content:space-between;align-items:center">
      <h2>${esc(c.case_id)}</h2>
      <span><span class="badge ${esc(c.status)}">${esc(status)}</span>
      ${c.seconds ? `<span class="small muted">${mins(c.seconds)}</span>` : ""}</span></div>
    <div class="case"><div>${img}</div><div>
      ${done && c.status !== "failed" ? `<p class="small muted">Liver mean of 3 circles: <b>${fmt(c.liver_mean)} HU</b>${c.spleen_mean !== null && c.spleen_mean !== undefined ? ` · spleen <b>${fmt(c.spleen_mean)} HU</b> · liver − spleen <b>${fmt(c.l_minus_s)} HU</b> · liver/spleen <b>${fmt(c.l_s_ratio, 2)}</b>` : ""} · series: ${esc(c.series || "?")} · slice ${fmt(c.slice_mm, 2)} mm</p>` : ""}
      ${c.error ? `<p class="error small">${esc(c.error)}</p>` : ""}
      ${rows ? `<div class="table-wrap"><table><thead><tr><th>ROI</th><th class="num">Mean HU</th><th class="num">SD</th><th class="num">Min</th><th class="num">Max</th><th class="num">Area cm²</th><th class="num">z mm (image)</th><th>Where</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      ${flags}
      ${done ? `<p class="small"><a href="/cases/${c.id}/result.json" target="_blank">all values (JSON)</a> · <a href="/cases/${c.id}/log.txt" target="_blank">log</a></p>` : ""}
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
  const sig = JSON.stringify(d.cases.map((c) => [c.id, c.status, c.step, c.has_qa]));
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
