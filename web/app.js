/* VaultMCP client */
const $ = (s) => document.querySelector(s);
const grid = $("#grid"), emptyEl = $("#empty");
let debounceTimer = null, currentData = [];

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail?.error || j.detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

function imgSrc(url, title) {
  return url ? `/api/image?url=${encodeURIComponent(url)}` : null;
}

function card(item) {
  const el = document.createElement("div");
  el.className = "card";
  const src = imgSrc(item.image_url);
  el.innerHTML = `
    ${src ? `<img class="thumb" loading="lazy" src="${src}">`
          : `<div class="thumb placeholder">📦</div>`}
    <div class="card-body">
      <div class="card-title">${esc(item.title)}</div>
      <div class="card-meta">
        <span>${esc(item.publisher || "")}</span>
        <span class="badge ${item.source}">${item.source === "unity" ? "Unity" : "Fab"}</span>
      </div>
      <div><span class="badge cat">${esc(item.category)}</span>
        ${item.size_str ? ` <span style="color:var(--muted);font-size:11px">${item.size_str}</span>` : ""}</div>
      <div class="tags">${(item.tags || []).slice(0, 6).map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div>
    </div>`;
  el.onclick = () => openModal(item);
  return el;
}

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }

async function load() {
  const q = $("#search").value.trim();
  const engine = $("#engine").value, pipeline = $("#pipeline").value, category = $("#category").value;
  const params = new URLSearchParams({ q: "", query: q, category, pipeline, source: engine, limit: "120" });
  const data = await api(`/api/assets?${params}`);
  currentData = data.items;
  grid.innerHTML = "";
  currentData.forEach(i => grid.appendChild(card(i)));
  emptyEl.classList.toggle("hidden", currentData.length > 0);
  const st = data.stats;
  const parts = Object.entries(st.sources || {}).map(([k, v]) => `${k}: ${v}`);
  $("#stats").textContent = `${st.total} assets · ${parts.join(" · ")}`;
}

async function initCategories() {
  const { categories } = await api("/api/categories");
  const sel = $("#category");
  categories.forEach(c => {
    const o = document.createElement("option"); o.value = c; o.textContent = c; sel.appendChild(o);
  });
}

async function openModal(item) {
  const full = await api(`/api/asset/${encodeURIComponent(item.id)}`);
  const src = imgSrc(full.image_url);
  const gallery = (full.gallery_images || []).slice(0, 8)
    .map(g => `<img src="${imgSrc(g)}">`).join("");
  const videos = (full.video_links || []).length
    ? `<div class="section"><h4>Videos</h4><ul>${full.video_links
        .map(v => `<li><a href="${v}" target="_blank" style="color:var(--accent)">${v}</a></li>`).join("")}</ul></div>`
    : "";
  $("#modal-body").innerHTML = `
    ${src ? `<img class="hero" src="${src}">` : ""}
    <h2>${esc(full.title)}</h2>
    <div class="sub">${esc(full.publisher)} · <span class="badge ${full.source}">${full.source === "unity" ? "Unity Asset Store" : "Fab / Unreal"}</span>
      ${full.version ? ` · v${esc(full.version)}` : ""}${full.size_str ? ` · ${esc(full.size_str)}` : ""}</div>
    <dl class="kv">
      <dt>Category</dt><dd>${esc(full.category)}</dd>
      ${full.render_pipelines?.length ? `<dt>Pipelines</dt><dd>${esc(full.render_pipelines.join(", "))}</dd>` : ""}
      ${full.formats?.length ? `<dt>Formats</dt><dd>${esc(full.formats.join(", "))}</dd>` : ""}
      ${full.license ? `<dt>License</dt><dd>${esc(full.license)}</dd>` : ""}
      ${full.claimed_date ? `<dt>Acquired</dt><dd>${esc(full.claimed_date)}</dd>` : ""}
      ${full.store_url ? `<dt>Store</dt><dd><a href="${full.store_url}" target="_blank" style="color:var(--accent)">Open listing ↗</a></dd>` : ""}
    </dl>
    ${full.summary ? `<div class="section"><h4>About</h4><p>${esc(full.summary)}</p></div>` : ""}
    ${full.usage_notes ? `<div class="section"><h4>Usage notes</h4><p>${esc(full.usage_notes)}</p></div>` : ""}
    ${(full.tags || []).length ? `<div class="tags" style="margin-top:12px">${full.tags.map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}
    ${gallery ? `<div class="section"><h4>Gallery</h4><div class="gallery">${gallery}</div></div>` : ""}
    ${videos}
    <button class="copy-btn" id="copy-ctx">📋 Copy context for AI</button>`;
  $("#copy-ctx").onclick = () => copyContext(full);
  $("#modal").classList.remove("hidden");
}

function copyContext(a) {
  const ctx = [
    `Asset: ${a.title}`, `Publisher: ${a.publisher}`,
    `Engine: ${a.source === "unity" ? "Unity Asset Store" : "Fab (Unreal)"}`,
    a.version && `Version: ${a.version}`,
    `Category: ${a.category}`,
    a.render_pipelines?.length && `Pipelines: ${a.render_pipelines.join(", ")}`,
    a.formats?.length && `Formats: ${a.formats.join(", ")}`,
    a.tags?.length && `Tags: ${a.tags.join(", ")}`,
    a.summary && `About: ${a.summary}`,
    a.usage_notes && `Usage notes: ${a.usage_notes}`,
    a.store_url && `Store URL: ${a.store_url}`,
  ].filter(Boolean).join("\n");
  navigator.clipboard.writeText(ctx);
  const b = $("#copy-ctx"); b.textContent = "✅ Copied!";
  setTimeout(() => b.textContent = "📋 Copy context for AI", 1500);
}

/* ---- events ---- */
$("#search").addEventListener("input", () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(load, 120);
});
["#engine", "#pipeline", "#category"].forEach(s => $(s).addEventListener("change", load));
$("#modal-close").onclick = () => $("#modal").classList.add("hidden");
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); });
document.addEventListener("keydown", e => { if (e.key === "Escape") $("#modal").classList.add("hidden"); });

$("#btn-sync").onclick = () => $("#sync-panel").classList.toggle("hidden");

document.querySelectorAll(".btn-login").forEach(b => b.onclick = async () => {
  $("#sync-status").textContent = `Opening browser for ${b.dataset.provider} login…`;
  try {
    const r = await api(`/api/login/${b.dataset.provider}`, { method: "POST" });
    $("#sync-status").textContent = r.message;
  } catch (e) { $("#sync-status").textContent = "⚠ " + e.message; }
});
document.querySelectorAll(".btn-fetch").forEach(b => b.onclick = async () => {
  if (b.id === "btn-enrich") {
    $("#sync-status").textContent = "Enriching batch…";
    try { const r = await api("/api/enrich", { method: "POST" }); $("#sync-status").textContent = `Enriched ${r.enriched} assets.`; load(); }
    catch (e) { $("#sync-status").textContent = "⚠ " + e.message; }
    return;
  }
  $("#sync-status").textContent = `Fetching ${b.dataset.provider} library…`;
  try {
    const r = await api(`/api/fetch/${b.dataset.provider}`, { method: "POST" });
    $("#sync-status").textContent = `✅ Seen ${r.assets_seen} assets from ${r.provider}.`; load();
  } catch (e) { $("#sync-status").textContent = "⚠ " + e.message; }
});

initCategories().then(load);
