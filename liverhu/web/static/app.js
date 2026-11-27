// HUnter lab website - browser side (no external libraries).
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

const val = (id) => ($(id) ? $(id).value : "");
const ticked = (id, dflt = false) => ($(id) ? $(id).checked : dflt);

async function startUpload(library = false) {
  $("go").disabled = true;
  $("upbox").hidden = false;
  const total = picked.reduce((a, x) => a + x.file.size, 0) || 1;
  try {
    const fd = new FormData();
    fd.append("label", val("label"));
    fd.append("roi_diameter_mm", val("diam"));
    fd.append("edge_margin_mm", val("edge"));
    fd.append("ligament_margin_mm", val("lig"));
    fd.append("preferred_slice_mm", val("slice"));
    if (ticked("ident")) fd.append("include_identifiers", "on");
    if (ticked("saveseg")) fd.append("save_segmentation", "on");
    fd.append("keep_edit_data", ticked("keepedit", true) ? "on" : "off");
    if (library) fd.append("library", "on");
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
    if (library) {
      $("upmsg").textContent = "Uploaded. The studies appear in the list below as soon as their files are read.";
      setPicked([]);
      $("label").value = "";
      refreshLibrary();
      setTimeout(() => { $("upbox").hidden = true; $("bar").style.width = "0"; }, 6000);
      return;
    }
    $("upmsg").textContent = "Upload complete - starting measurement…";
    location.href = `/jobs/${job_id}`;
  } catch (e) {
    const m = String(e.message || e);
    $("upmsg").innerHTML = `<span class="error">Upload failed: ${esc(m.startsWith("507") ? "this computer is almost out of disk space. Delete studies from the library (or other files) and try again." : m)}</span>`;
    $("go").disabled = picked.length === 0;
  }
}

// ---------- index page ----------
function initDrop() {
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
}

function initIndex() {
  initDrop();
  $("go").onclick = () => startUpload(false);
  refreshJobs();
  setInterval(refreshJobs, 4000);
}

// ---------- library page ----------
let lib = { studies: [] }, libSel = new Set(), libSig = "", lastClick = null;
const gb = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round((b || 0) / 1e6))} MB`);
const nth = (n) => { const s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); };

function initLibrary() {
  initDrop();
  $("go").onclick = () => startUpload(true);
  $("filter").oninput = () => { libSig = ""; renderLibrary(); };
  $("selall").onchange = (e) => {
    for (const s of shownStudies()) e.target.checked ? libSel.add(s.id) : libSel.delete(s.id);
    libSig = ""; renderLibrary();
  };
  $("lib").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr || e.target.closest("a")) return;
    const id = Number(tr.dataset.id);
    const box = tr.querySelector("input[type=checkbox]");
    const on = e.target === box ? box.checked : !libSel.has(id);
    const shown = shownStudies().map((s) => s.id);
    if (e.shiftKey && lastClick !== null && shown.includes(lastClick)) {   // shift-click selects a range
      const [a, b] = [shown.indexOf(lastClick), shown.indexOf(id)].sort((x, y) => x - y);
      for (const i of shown.slice(a, b + 1)) on ? libSel.add(i) : libSel.delete(i);
    } else on ? libSel.add(id) : libSel.delete(id);
    lastClick = id;
    libSig = ""; renderLibrary();
  });
  $("measure").onclick = measureSelected;
  $("unqueue").onclick = () => libAction("unqueue", [...libSel], (d) => `${d.unqueued.length} taken out of the queue.`);
  $("delete").onclick = deleteSelected;
  refreshLibrary();
  setInterval(refreshLibrary, 4000);
}

function shownStudies() {
  const f = val("filter").trim().toLowerCase();
  if (!f) return lib.studies;
  return lib.studies.filter((s) => [s.name, s.batch, s.info.series, s.info.patient_id, s.info.accession_number, s.added_by]
    .some((t) => String(t || "").toLowerCase().includes(f)));
}

async function refreshLibrary() {
  try { lib = await getJSON("/api/library"); } catch (e) { return; }
  const known = new Set(lib.studies.map((s) => s.id));
  for (const id of [...libSel]) if (!known.has(id)) libSel.delete(id);
  renderLibrary();
}

function libStatus(s) {
  if (s.status === "stored") return `<span class="badge">stored</span>`;
  if (s.status === "queued") return `<span class="badge queued">waiting, ${nth(s.place || 1)} in line</span>`;
  if (s.status === "running") {
    const t = s.started_at ? `, ${mins(Math.max(0, (Date.now() - Date.parse(s.started_at)) / 1000))}` : "";
    return `<span class="badge running">measuring</span> <span class="small muted">${esc(s.step || "starting")}${t}</span>`;
  }
  const link = `/jobs/${s.job_id}?case=${s.id}`;
  if (!s.measured) return `<span class="badge failed">failed</span> <a class="small" href="${link}">Why</a>`;
  const warn = s.hu_warning || s.quality === "poor" || s.quality === "check" || s.scan_complete === false;
  return `<a class="result-link" href="${link}">${warn ? `<span class="huwarn" title="Has warnings: open the result">${WARN_ICON}</span>` : ""}`
    + `liver <b>${fmt(s.liver_mean)}</b>, spleen <b>${fmt(s.spleen_mean)}</b> HU</a>${s.manual ? ` <span class="small muted">circles moved by hand</span>` : ""}`;
}

function renderLibrary() {
  const shown = shownStudies();
  const sig = JSON.stringify([shown.map((s) => [s.id, s.status, s.place, s.step, s.measured, s.liver_mean, s.status === "running" ? Date.now() : 0]), [...libSel], lib.batches]);
  if (sig !== libSig) {
    libSig = sig;
    $("lib").innerHTML = shown.map((s) => {
      const i = s.info || {};
      const detail = [i.series, i.n_images ? `${i.n_images} images` : "", i.slice_mm ? `${fmt(i.slice_mm, 1)} mm` : ""].filter(Boolean).join(", ");
      const ids = [i.patient_id ? `ID ${i.patient_id}` : "", i.study_date ? `date ${i.study_date}` : ""].filter(Boolean).join(", ");
      const dup = s.same_study_as.length ? `<div class="small warn-text">Same study as ${s.same_study_as.map((x) => esc(nameOf(x))).join(", ")}</div>` : "";
      return `<tr data-id="${s.id}" class="${libSel.has(s.id) ? "picked" : ""}">
        <td class="sel"><input type="checkbox" ${libSel.has(s.id) ? "checked" : ""} aria-label="Select ${esc(s.name)}"></td>
        <td><b>${esc(s.name)}</b><div class="small muted">${esc(detail)}${ids ? `<br>${esc(ids)}` : ""}</div>${dup}</td>
        <td class="small">${esc(s.batch || "–")}</td>
        <td class="small">${esc((s.added_at || "").replace("T", " ").slice(0, 16))}<div class="muted">${esc(s.added_by)}</div></td>
        <td class="num small">${gb(i.size_bytes)}</td>
        <td>${libStatus(s)}</td></tr>`;
    }).join("") || `<tr><td colspan="6" class="muted">${lib.studies.length ? "No study matches." : "The library is empty. Drop studies above to store them."}</td></tr>`;
    $("notices").innerHTML = (lib.batches || []).map((b) => b.status === "failed"
      ? `<p class="notice bad">${esc(b.label || "Upload " + b.id)}: ${esc(b.message || "no CT scans found")}. <a href="#" onclick="dismissBatch(${b.id});return false">Dismiss</a></p>`
      : b.status === "uploading"
        ? `<p class="notice">${esc(b.label || "Upload " + b.id)}: uploading, ${b.n_files} files so far. <a href="#" onclick="dismissBatch(${b.id}, true);return false">Remove</a> <span class="muted">(only if this upload was interrupted)</span></p>`
        : `<p class="notice">${esc(b.label || "Upload " + b.id)}: reading the files…</p>`).join("");
  }
  const all = shown.length > 0 && shown.every((s) => libSel.has(s.id));
  $("selall").checked = all;
  $("selall").indeterminate = !all && shown.some((s) => libSel.has(s.id));
  const sel = lib.studies.filter((s) => libSel.has(s.id));
  const canMeasure = sel.filter((s) => !["queued", "running"].includes(s.status)).length;
  const waiting = sel.filter((s) => s.status === "queued").length;
  $("selcount").textContent = sel.length ? `${sel.length} selected` : "None selected";
  $("measure").disabled = !canMeasure;
  $("measure").textContent = canMeasure > 1 ? `Measure ${canMeasure} selected` : "Measure selected";
  $("unqueue").disabled = !waiting;
  $("delete").disabled = !sel.length;
  const free = lib.free_bytes;
  const wait = lib.waiting ? ` ${lib.waiting} in the measuring queue${lib.avg_seconds_per_case ? `, about ${mins(lib.waiting * lib.avg_seconds_per_case)}` : ""}.` : "";
  $("libsum").innerHTML = `${lib.studies.length} ${lib.studies.length === 1 ? "study" : "studies"}, ${gb(lib.stored_bytes)}.${wait} `
    + `<span class="${free < 10e9 ? "warn-text" : ""}">${gb(free)} free on this computer.</span>`;
}

function nameOf(id) { const s = lib.studies.find((x) => x.id === id); return s ? s.name : `#${id}`; }

async function libAction(what, ids, msg, extra = {}) {
  try {
    const d = await getJSON(`/api/library/${what}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids, ...extra }) });
    $("libmsg").textContent = msg(d);
    $("libmsg").className = "small";
  } catch (e) {
    $("libmsg").textContent = `That did not work: ${e.message}`;
    $("libmsg").className = "small error";
  }
  libSel.clear(); libSig = "";
  await refreshLibrary();
}

function measureSelected() {
  // in the order they were picked
  const ids = [...libSel].filter((id) => { const s = lib.studies.find((x) => x.id === id); return s && !["queued", "running"].includes(s.status); });
  const again = ids.filter((id) => lib.studies.find((x) => x.id === id).measured).length;
  if (again && !confirm(`${again} of these ${again === 1 ? "was" : "were"} measured before. Measuring again replaces ${again === 1 ? "its" : "their"} results, including circles moved by hand. Continue?`)) return;
  libAction("measure", ids, (d) => `${d.queued.length} ${d.queued.length === 1 ? "study" : "studies"} added to the queue. They are measured one after another; you can close this page.`, {
    roi_diameter_mm: val("diam"), edge_margin_mm: val("edge"), ligament_margin_mm: val("lig"),
    keep_edit_data: ticked("keepedit", true), save_segmentation: ticked("saveseg"),
  });
}

function deleteSelected() {
  const n = libSel.size;
  if (!confirm(`Delete ${n} ${n === 1 ? "study" : "studies"} from the library? Their scans and results are removed from this computer. This cannot be undone.`)) return;
  libAction("delete", [...libSel], (d) => `${d.deleted.length} deleted.`);
}

async function dismissBatch(id, ask = false) {
  if (ask && !confirm("Remove this unfinished upload? Files received so far are deleted.")) return;
  await getJSON(`/api/jobs/${id}/delete`, { method: "POST" });
  libSig = ""; refreshLibrary();
}

async function refreshJobs() {
  try {
    const d = await getJSON("/api/jobs");
    const rows = d.jobs.map((j) => {
      const pct = j.n_cases ? Math.round((100 * j.n_done) / j.n_cases) : 0;
      const label = { done: "measured", running: "measuring", queued: "reading files", uploading: "uploading", failed: "failed", cancelled: "cancelled" }[j.status] || j.status;
      return `<tr>
        <td class="muted">${j.id}</td><td><a href="/jobs/${j.id}">${esc(j.label || "Upload " + j.id)}</a></td>
        <td>${esc(j.submitted_by)}</td><td class="small muted">${esc((j.created_at || "").replace("T", " ").slice(0, 16))}</td>
        <td><span class="badge ${esc(j.status)}">${esc(label)}</span></td>
        <td class="small">${j.n_cases ? `${j.n_done} of ${j.n_cases} studies` : esc(j.status === "uploading" ? `${j.n_files} files received` : j.message || "")}</td>
        <td class="small"><a href="/jobs/${j.id}">Open</a>&nbsp;&nbsp;<a href="#" onclick="delJob(${j.id});return false">Delete</a></td></tr>`;
    });
    $("jobs").innerHTML = rows.join("") || `<tr><td colspan="7" class="muted">Nothing measured yet. Drop a study above to start.</td></tr>`;
    $("queue").textContent = d.cases_waiting ? `${d.cases_waiting} studies waiting` + (d.avg_seconds_per_case ? `, about ${mins(d.cases_waiting * d.avg_seconds_per_case)} left` : "") : "Ready";
  } catch (e) { /* keep last view */ }
}

async function delJob(id) {
  if (!confirm(`Delete upload #${id} and all its results? This cannot be undone.`)) return;
  await getJSON(`/api/jobs/${id}/delete`, { method: "POST" });
  if (location.pathname.startsWith("/jobs/")) location.href = "/"; else refreshJobs();
}

// ---------- job page ----------
const ROIS = [["LL", "Left lobe", "roi-ll"], ["RL1", "Right lobe 1 (front)", "roi-rl1"], ["RL2", "Right lobe 2 (back)", "roi-rl2"], ["SPL", "Spleen", "roi-spl"]];

const WARN_ICON = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5 15 14H1z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M8 6v4" stroke="currentColor" stroke-width="1.6"/><circle cx="8" cy="12" r=".9" fill="currentColor"/></svg>`;
const ROI_HEX = { LL: "#2fd3e8", RL1: "#f25cc8", RL2: "#f2a93b", SPL: "#5dd68a" };
const WIN = [-160, 240];   // the same grayscale window as the check images

function huWarn(r) {
  if (!r.hu_check) return "";
  const t = `${r.hu_check === "low" ? "Too low" : "Too high"}${r.hu_range ? `: expected ${fmt(r.hu_range[0], 0)}–${fmt(r.hu_range[1], 0)} HU` : ""}`;
  return `<span class="huwarn" title="${esc(t)}">${WARN_ICON}${r.hu_check}</span>`;
}

// HU ruler: grayscale bar in the image window, expected ranges as brackets, circles as markers
function huRuler(c) {
  const pct = (hu) => ((Math.max(WIN[0], Math.min(WIN[1], hu)) - WIN[0]) / (WIN[1] - WIN[0])) * 100;
  const band = (rng, cls, label) => (rng ? `<div class="band ${cls}" style="left:${pct(rng[0])}%;width:${pct(rng[1]) - pct(rng[0])}%"><span>${label} ${fmt(rng[0], 0)}–${fmt(rng[1], 0)} HU</span></div>` : "");
  let marks = "", desc = [];
  const placedAt = [];                       // nudge dots that would sit on top of each other
  for (const k of ["LL", "RL1", "RL2", "SPL"]) {
    const r = c[k] || {};
    if (!r.placed || r.mean_hu === null || r.mean_hu === undefined) continue;
    const t = `${k} ${fmt(r.mean_hu)} HU${r.hu_check ? ", " + r.hu_check : ""}`;
    desc.push(t);
    const p = pct(r.mean_hu);
    const n = k === "SPL" ? 0 : placedAt.filter((q) => Math.abs(q - p) < 2.2).length;
    if (k !== "SPL") placedAt.push(p);
    const dx = n === 0 ? 0 : (n % 2 ? 1 : -1) * Math.ceil(n / 2) * 11;
    marks += `<div class="mk${k === "SPL" ? " below" : ""}${r.hu_check ? " out" : ""}" style="left:${p}%;--c:${ROI_HEX[k]};--dx:${dx}px" title="${esc(t)}"></div>`;
  }
  const ticks = [-100, 0, 100, 200].map((t) => `<span class="tick" style="left:${pct(t)}%">${t}</span>`).join("");
  return `<div class="ruler" role="img" aria-label="${esc("Mean HU: " + desc.join("; "))}"><div class="bar"></div>
    ${band((c.LL || {}).hu_range, "liver", "expected liver")}${band((c.SPL || {}).hu_range, "spleen", "expected spleen")}${marks}${ticks}</div>`;
}

function caseCard(c) {
  const done = ["ok", "partial", "failed"].includes(c.status);
  const rows = ROIS.map(([k, name, cls]) => {
    const r = c[k] || {};
    if (!done || r.placed === undefined || r.placed === null) return "";
    if (!r.placed) return `<tr><td class="${cls}">${k}</td><td colspan="6" class="error">not placed</td></tr>`;
    const where = k === "SPL" ? "spleen" : `segment ${esc(r.segment_at_center)}, ${esc(r.side_of_ligament)}${r.side_ok ? "" : " (check side)"}`;
    return `<tr><td class="${cls}" title="${esc(name)}">${k}</td>
      <td class="num">${fmt(r.mean_hu)}${huWarn(r)}</td><td class="num">${fmt(r.sd_hu)}</td>
      <td class="num">${fmt(r.min_hu, 0)}</td><td class="num">${fmt(r.max_hu, 0)}</td>
      <td class="num">${fmt(r.area_cm2, 2)}</td>
      <td class="small">${where}${r.manual ? `, <span title="moved by hand">moved by ${esc(r.edited_by || "hand")}</span>` : ""}</td></tr>`;
  }).join("");
  const z = c.LL && c.LL.placed ? `${fmt(c.LL.z_mm)} mm${c.LL.dicom_instance ? `, image ${c.LL.dicom_instance}` : ""}` : "";
  const qBadge = { good: ["ok", "Good quality"], check: ["partial", "Check"], poor: ["failed", "Poor quality"] }[c.quality] || null;
  const notes = [...(c.completeness_notes || []), ...(c.quality_notes || [])];
  const flags = (c.flags || []).filter((f) => !/ mean HU is /.test(f));
  const img = c.has_qa ? `<img src="/cases/${c.id}/qa.png?r=${c.revision || 0}" alt="Check image for ${esc(c.case_id)}" onclick="openImg(this.src)">`
    : `<div class="wait">${done ? "No image" : c.status === "stored" ? "Stored in the library, not measured yet. Tick it in the Library and press Measure." : "The check image appears when the study is measured."}</div>`;
  const label = { running: "measuring", queued: "waiting", stored: "in library" }[c.status] || c.status;
  const elapsed = c.status === "running" && c.started_at ? Math.max(0, (Date.now() - Date.parse(c.started_at)) / 1000) : null;
  const running = c.status === "running" ? `${esc(c.step || "starting")}${elapsed !== null ? `, ${mins(elapsed)} so far` : ""}` : "";
  const measured = done && c.status !== "failed";
  return `<section class="card" id="case-${c.id}">
    <div class="study-head"><h2>${esc(c.case_id)}</h2>
      <div class="status-line">
        ${measured && qBadge ? `<span class="badge ${qBadge[0]}">${qBadge[0] === "ok" ? "" : WARN_ICON.replace("<svg", '<svg width="12" height="12"')}${qBadge[1]}</span>` : ""}
        ${measured && c.scan_complete === false ? `<span class="badge failed">Incomplete scan</span>` : ""}
        ${!measured ? `<span class="badge ${esc(c.status)}">${esc(label)}</span>` : ""}
        ${c.seconds ? `<span class="small muted">${mins(c.seconds)}</span>` : ""}
      </div></div>
    ${running ? `<p class="small">${running}</p>` : ""}
    <div class="study"><div class="viewer">${img}</div><div>
      ${c.error ? `<p class="error small">${esc(c.error)}</p>` : ""}
      ${rows ? `<div class="table-wrap"><table class="measures"><thead><tr><th>Circle</th><th class="num">Mean HU</th><th class="num">SD</th><th class="num">Min</th><th class="num">Max</th><th class="num">cm²</th><th>Where</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      ${measured ? huRuler(c) : ""}
      ${measured ? `<dl class="stats">
          <div><dt>Liver mean</dt><dd>${fmt(c.liver_mean)} <small>HU</small></dd></div>
          <div><dt>Spleen</dt><dd>${fmt(c.spleen_mean)} <small>HU</small></dd></div>
          <div><dt>Liver − spleen</dt><dd>${fmt(c.l_minus_s)} <small>HU</small></dd></div>
          <div><dt>Liver / spleen</dt><dd>${fmt(c.l_s_ratio, 2)}</dd></div></dl>
        <p class="small muted">Liver circles on the slice at ${esc(z)}. ${esc(c.contrast_phase ? c.contrast_phase.charAt(0).toUpperCase() + c.contrast_phase.slice(1) : "")}${c.contrast_reason ? ` (${esc(c.contrast_reason)})` : ""}. Series ${esc(c.series || "?")}, ${fmt(c.slice_mm, 2)} mm slices.</p>` : ""}
      ${notes.length ? `<ul class="qnotes">${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
      ${flags.length ? `<ul class="flags">${flags.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
      ${done ? `<div class="study-actions">${c.editable ? `<a class="btn" href="/cases/${c.id}/edit">Adjust circles</a>` : ""}<a href="/cases/${c.id}/result.json" target="_blank">All values (JSON)</a><a href="/cases/${c.id}/log.txt" target="_blank">Log</a></div>` : ""}
    </div></div></section>`;
}

function openImg(src) { const lb = $("lightbox"); lb.querySelector("img").src = src; lb.style.display = "flex"; }

let lastSig = "";
async function refreshJob(id, only) {
  const d = await getJSON(`/api/jobs/${id}`);
  const j = d.job;
  if (only) d.cases = d.cases.filter((c) => c.id === only);
  $("status").className = "badge " + j.status;
  $("status").textContent = { done: "measured", running: "measuring", queued: "reading files", uploading: "uploading", stored: "in library" }[j.status] || j.status;
  const pct = j.n_cases ? (100 * j.n_done) / j.n_cases : 0;
  $("bar").style.width = pct + "%";
  let p = j.n_cases ? `${j.n_done} of ${j.n_cases} scan(s) measured` : (j.status === "queued" ? "reading the files…" : "");
  const left = j.n_cases - j.n_done;
  if (left > 0 && d.avg_seconds_per_case) p += ` · about ${mins((left + d.cases_ahead) * d.avg_seconds_per_case)} left`;
  if (d.cases_ahead) p += ` · ${d.cases_ahead} scan(s) from earlier uploads first`;
  $("progress").textContent = p;
  $("message").textContent = j.message || "";
  const sig = JSON.stringify(d.cases.map((c) => [c.id, c.status, c.step, c.has_qa, c.revision, c.status === "running" ? Date.now() : 0]));
  if (sig !== lastSig) { $("cases").innerHTML = d.cases.map(caseCard).join("") || `<p class="muted">This study is no longer stored.</p>`; lastSig = sig; }
  if (j.status === "stored") return !d.cases.some((c) => ["queued", "running"].includes(c.status));
  return ["done", "failed", "cancelled"].includes(j.status);
}

function initJob(id, only = null) {
  const tick = async () => {
    let finished = false;
    try { finished = await refreshJob(id, only); } catch (e) { /* retry */ }
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
      $("r_mean").innerHTML = esc(fmt(m.mean_hu) + " HU") + (hw ? ` <span class="huwarn" title="expected ${fmt(rng[0], 0)}–${fmt(rng[1], 0)} HU (${esc(info.contrast_phase || "")})">${WARN_ICON}${hw}</span>` : "");
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
