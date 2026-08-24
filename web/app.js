/* VaultMCP Modern Frontend Logic */
const state = {
  assets: [],
  categories: [],
  recipes: [],
  selectedCategory: 'all',
  selectedEngine: 'all',
  selectedPipeline: 'all',
  selectedFilter: 'all', // 'all', 'local', 'cloud', 'recipes'
  sortBy: 'relevance',
  viewMode: 'grid',
  searchQuery: '',
  stats: {},
  activeModalAsset: null
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// API Helper
function getAuthToken() {
  const match = document.cookie.match(/(?:^|; )vault_token=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : (localStorage.getItem("vault_token") || "");
}

async function api(path, opts = {}) {
  opts.headers = opts.headers || {};
  const token = getAuthToken();
  if (token && !opts.headers["X-VaultMCP-Token"]) {
    opts.headers["X-VaultMCP-Token"] = token;
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = j.detail?.error || j.detail || msg;
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function esc(str) {
  const d = document.createElement('div');
  d.textContent = str ?? '';
  return d.innerHTML;
}

function imgSrc(url) {
  return url ? `/api/image?url=${encodeURIComponent(url)}` : null;
}

// Render Asset Card
function createAssetCard(item) {
  const card = document.createElement('div');
  card.className = 'asset-card';
  card.dataset.id = item.id;

  const isLocal = Boolean(item.local_path);
  const src = imgSrc(item.image_url);

  card.innerHTML = `
    <div class="card-media">
      ${src ? `<img src="${src}" alt="${esc(item.title)}" loading="lazy">`
            : `<div class="card-media-placeholder">📦</div>`}
      <div class="media-top-badges">
        <span class="source-tag ${item.source}">${item.source === 'unity' ? 'Unity' : 'Fab'}</span>
        <span class="status-tag ${isLocal ? 'local' : 'cloud'}">
          ${isLocal ? '⚡ Local' : '☁ Cloud'}
        </span>
      </div>
      ${item.match ? `<span class="match-provenance-tag">${esc(item.match)}</span>` : ''}
    </div>
    <div class="card-content">
      <div class="card-title" title="${esc(item.title)}">${esc(item.title)}</div>
      <div class="card-publisher-row">
        <span class="card-publisher" title="${esc(item.publisher)}">${esc(item.publisher || 'Unknown Publisher')}</span>
        ${item.size_str ? `<span class="card-size">${esc(item.size_str)}</span>` : ''}
      </div>
      <div class="card-pills-row">
        <span class="category-badge">${esc(item.category)}</span>
        ${(item.render_pipelines || []).slice(0, 2).map(p => `<span class="pipeline-badge">${esc(p)}</span>`).join('')}
      </div>
      <div class="card-tags">
        ${(item.tags || []).slice(0, 4).map(t => `<span class="tag-chip">${esc(t)}</span>`).join('')}
      </div>
    </div>
  `;

  card.onclick = () => openDetailModal(item.id);
  return card;
}

// Fetch & Render Main Library
async function loadAssets() {
  const localFilter = (state.selectedFilter === 'local' || state.selectedFilter === 'cloud')
    ? state.selectedFilter : 'all';
  const params = new URLSearchParams({
    query: state.searchQuery,
    category: state.selectedCategory,
    pipeline: state.selectedPipeline,
    source: state.selectedEngine,
    local: localFilter,
    limit: '2000'
  });

  try {
    const data = await api(`/api/assets?${params}`);
    state.assets = data.items || [];
    state.stats = data.stats || {};

    let filtered = [...state.assets];

    // Sort
    if (state.sortBy === 'title_asc') {
      filtered.sort((a, b) => a.title.localeCompare(b.title));
    } else if (state.sortBy === 'size_desc') {
      filtered.sort((a, b) => (b.size_mb || 0) - (a.size_mb || 0));
    } else if (state.sortBy === 'claimed_desc') {
      filtered.sort((a, b) => (b.claimed_date || '').localeCompare(a.claimed_date || ''));
    }

    renderGrid(filtered);
    updateMetrics(filtered.length);
  } catch (err) {
    console.error('Failed to load assets:', err);
  }
}

function renderGrid(items) {
  const grid = $('#asset-grid');
  const empty = $('#empty-state');
  const recipesView = $('#recipes-view');

  if (state.selectedFilter === 'recipes') {
    grid.classList.add('hidden');
    empty.classList.add('hidden');
    recipesView.classList.remove('hidden');
    renderRecipes();
    return;
  }

  recipesView.classList.add('hidden');
  grid.classList.remove('hidden');
  grid.innerHTML = '';

  if (items.length === 0) {
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    const fragment = document.createDocumentFragment();
    items.forEach(item => fragment.appendChild(createAssetCard(item)));
    grid.appendChild(fragment);
  }
}

async function renderRecipes() {
  const container = $('#recipes-grid');
  container.innerHTML = '<div class="metrics-count">Loading stack recipes...</div>';

  try {
    const recipesData = await api('/api/recipes').catch(() => ({ recipes: [] }));
    const recipes = recipesData.recipes || [];

    if (recipes.length === 0) {
      container.innerHTML = '<p class="helper-text">No custom recipes defined yet in data/recipes.json.</p>';
      return;
    }

    container.innerHTML = '';
    recipes.forEach(r => {
      const card = document.createElement('div');
      card.className = 'recipe-card';
      card.innerHTML = `
        <div class="recipe-title-bar">
          <h3>${esc(r.name)}</h3>
          <span class="category-badge">${esc(r.pipeline || r.engine || 'Multi-Platform')}</span>
        </div>
        <p class="recipe-desc">${esc(r.notes || 'Production-grade stack composition.')}</p>
        <div class="recipe-items-list">
          ${(r.owned_matches || []).map(m => `
            <div class="recipe-item-row">
              <span class="recipe-item-name">${m.local ? '⚡ ' : '☁ '}${esc(m.title)}</span>
              <span class="recipe-item-slot">${esc(m.category)}</span>
            </div>
          `).join('')}
        </div>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    container.innerHTML = `<p class="helper-text">Error loading recipes: ${esc(err.message)}</p>`;
  }
}

function updateMetrics(count) {
  $('#results-count').textContent = `Showing ${count} of ${state.stats.total || 0} assets`;
  $('#count-all').textContent = state.stats.total || 0;

  const localCount = state.stats.downloaded_locally ?? 0;
  $('#count-local').textContent = localCount;
  $('#count-cloud').textContent = Math.max(0, (state.stats.total || 0) - localCount);

  // Search mode badge — the web API runs FTS5 keyword search;
  // hybrid semantic fusion currently lives in the MCP tools.
  const modeBadge = $('#search-mode-badge');
  if (state.searchQuery) {
    modeBadge.textContent = 'FTS5 Keyword Search';
    modeBadge.classList.remove('hidden');
  } else {
    modeBadge.classList.add('hidden');
  }
}

// Initialize Categories in Sidebar
async function initCategories() {
  const { categories } = await api('/api/categories');
  state.categories = categories || [];
  const container = $('#category-list');
  container.innerHTML = '';

  const allBtn = document.createElement('button');
  allBtn.className = 'cat-item active';
  allBtn.dataset.cat = 'all';
  allBtn.innerHTML = `<span>All Categories</span><span class="cat-count" id="cat-count-all">0</span>`;
  allBtn.onclick = () => selectCategory('all', allBtn);
  container.appendChild(allBtn);

  state.categories.forEach(cat => {
    const btn = document.createElement('button');
    btn.className = 'cat-item';
    btn.dataset.cat = cat;
    btn.innerHTML = `<span>${esc(cat)}</span><span class="cat-count">${state.stats.categories?.[cat] || ''}</span>`;
    btn.onclick = () => selectCategory(cat, btn);
    container.appendChild(btn);
  });
}

function selectCategory(cat, btnEl) {
  state.selectedCategory = cat;
  $$('.cat-item').forEach(b => b.classList.remove('active'));
  btnEl.classList.add('active');
  loadAssets();
}

// Inspector Modal
async function openDetailModal(assetId) {
  const full = await api(`/api/asset/${encodeURIComponent(assetId)}`);
  state.activeModalAsset = full;

  const isLocal = Boolean(full.local_path);
  const src = imgSrc(full.image_url);

  $('#modal-title').textContent = full.title;
  $('#modal-hero-img').src = src || '';
  $('#modal-hero-badge').textContent = isLocal ? '⚡ Ready on Disk' : '☁ Cloud Library';
  $('#modal-hero-badge').className = `modal-status-badge ${isLocal ? 'local' : 'cloud'}`;

  // Badges
  const badgesBox = $('#modal-badges');
  badgesBox.innerHTML = `
    <span class="source-tag ${full.source}">${full.source === 'unity' ? 'Unity Asset Store' : 'Fab / Unreal'}</span>
    <span class="category-badge">${esc(full.category)}</span>
    ${(full.render_pipelines || []).map(p => `<span class="pipeline-badge">${esc(p)}</span>`).join('')}
  `;

  // Meta Bar
  $('#modal-meta-bar').innerHTML = `
    <span>${esc(full.publisher || 'Unknown')}</span>
    ${full.version ? `<span>v${esc(full.version)}</span>` : ''}
    ${full.size_str ? `<span>${esc(full.size_str)}</span>` : ''}
    ${full.claimed_date ? `<span>Acquired ${esc(full.claimed_date)}</span>` : ''}
  `;

  // Content
  $('#modal-summary').textContent = full.summary || 'No description provided.';
  $('#modal-usage').textContent = full.usage_notes || 'Standard integration practices apply.';
  $('#modal-tags').innerHTML = (full.tags || []).map(t => `<span class="tag-chip">${esc(t)}</span>`).join('');

  // Gallery
  const galleryBox = $('#modal-gallery');
  galleryBox.innerHTML = '';
  if (full.gallery_images && full.gallery_images.length > 0) {
    full.gallery_images.forEach(gUrl => {
      const img = document.createElement('img');
      img.src = imgSrc(gUrl);
      img.onclick = () => { $('#modal-hero-img').src = img.src; };
      galleryBox.appendChild(img);
    });
  }

  // Videos
  const videoBox = $('#modal-videos');
  const videoLinksBox = $('#modal-video-links');
  videoLinksBox.innerHTML = '';
  if (full.video_links && full.video_links.length > 0) {
    videoBox.classList.remove('hidden');
    full.video_links.forEach(vUrl => {
      const a = document.createElement('a');
      a.className = 'video-chip';
      a.href = vUrl;
      a.target = '_blank';
      a.rel = 'noopener';
      a.innerHTML = `▶ Watch Demo / Trailer ↗`;
      videoLinksBox.appendChild(a);
    });
  } else {
    videoBox.classList.add('hidden');
  }

  // Stack & Gotchas tab
  const stackWarning = $('#modal-stack-warnings');
  const warnings = [];
  if (full.source === 'fab') {
    warnings.push('⚡ Unreal Engine Format: Requires channel re-authoring for Unity (ORM Green channel roughness must be inverted to smoothness for HDRP Mask Maps).');
  }
  if ((full.render_pipelines || []).includes('URP') && !(full.render_pipelines || []).includes('HDRP')) {
    warnings.push('⚠ URP Shader: Will render pink/unsupported in Unity HDRP projects.');
  }
  if ((full.render_pipelines || []).includes('HDRP') && !(full.render_pipelines || []).includes('URP')) {
    warnings.push('⚠ HDRP Shader: Requires High Definition Render Pipeline.');
  }
  stackWarning.innerHTML = warnings.length > 0 ? warnings.join('<br><br>') : '✓ No known pipeline conflicts for this asset.';

  // Specs
  $('#modal-specs-grid').innerHTML = `
    <dt>Asset ID</dt><dd>${esc(full.id)}</dd>
    <dt>Package ID</dt><dd>${esc(full.package_id || 'N/A')}</dd>
    <dt>Engine Formats</dt><dd>${esc((full.formats || []).join(', ') || (full.source === 'unity' ? 'UnityPackage' : 'Unreal Engine'))}</dd>
    <dt>Local Path</dt><dd>${esc(full.local_path || 'Not downloaded to local cache')}</dd>
  `;

  // AI Markdown Context
  const aiCtx = [
    `### Asset: ${full.title}`,
    `- **Publisher:** ${full.publisher || 'Unknown'}`,
    `- **Source:** ${full.source === 'unity' ? 'Unity Asset Store' : 'Fab (Unreal Engine)'}`,
    `- **Category:** ${full.category}`,
    full.render_pipelines?.length && `- **Render Pipelines:** ${full.render_pipelines.join(', ')}`,
    full.local_path ? `- **Local Status:** Downloaded on Disk (${full.local_path})` : `- **Local Status:** Cloud Library`,
    full.summary && `- **Summary:** ${full.summary}`,
    full.usage_notes && `- **Usage Notes:** ${full.usage_notes}`,
    full.store_url && `- **Store URL:** ${full.store_url}`,
  ].filter(Boolean).join('\n');
  $('#modal-ai-preview').textContent = aiCtx;

  // Unpack Box Setup
  const unpackBox = $('#unpack-box');
  if (isLocal && full.source === 'unity') {
    unpackBox.classList.remove('hidden');
    const savedPath = localStorage.getItem('vaultmcp_last_project') || '';
    $('#project-path-input').value = savedPath;
    $('#unpack-feedback').textContent = '';
  } else {
    unpackBox.classList.add('hidden');
  }

  // Links & Buttons
  $('#modal-store-link').href = full.store_url || '#';
  $('#modal-store-link').style.display = full.store_url ? 'inline-flex' : 'none';

  $('#detail-modal').classList.remove('hidden');
}

// Direct Unpack Action
async function executeUnpack() {
  const projectDir = $('#project-path-input').value.trim();
  const stripDemos = $('#strip-demos-check').checked;
  const feedback = $('#unpack-feedback');

  if (!projectDir) {
    feedback.textContent = '❌ Please specify a target Unity project directory.';
    feedback.style.color = 'var(--accent-red)';
    return;
  }

  localStorage.setItem('vaultmcp_last_project', projectDir);
  feedback.textContent = '⏳ Extracting .unitypackage directly to Assets/...';
  feedback.style.color = 'var(--text-secondary)';

  try {
    const formData = new URLSearchParams();
    formData.append('asset_id', state.activeModalAsset.id);
    formData.append('project_dir', projectDir);
    formData.append('strip_demos', stripDemos ? 'true' : 'false');

    const res = await api('/api/import', {
      method: 'POST',
      body: formData,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
    });

    let msg = `✅ Extracted ${res.written} files into ${res.project}`;
    if (res.stripped > 0) {
      msg += ` (Stripped ${res.stripped} demo files, saving ${res.stripped_mb} MB)`;
    }
    feedback.textContent = msg;
    feedback.style.color = 'var(--accent-green)';
  } catch (err) {
    feedback.textContent = `❌ Import error: ${err.message}`;
    feedback.style.color = 'var(--accent-red)';
  }
}

// Event Listeners Setup
function setupEvents() {
  // Search Input
  let debounce = null;
  const searchInput = $('#search-input');
  searchInput.addEventListener('input', (e) => {
    state.searchQuery = e.target.value.trim();
    $('#btn-clear-search').classList.toggle('hidden', !state.searchQuery);
    clearTimeout(debounce);
    debounce = setTimeout(loadAssets, 150);
  });

  $('#btn-clear-search').onclick = () => {
    searchInput.value = '';
    state.searchQuery = '';
    $('#btn-clear-search').classList.add('hidden');
    loadAssets();
  };

  // Keyboard shortcut '/'
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && document.activeElement !== searchInput) {
      e.preventDefault();
      searchInput.focus();
    }
    if (e.key === 'Escape') {
      $('#detail-modal').classList.add('hidden');
      $('#sync-modal').classList.add('hidden');
    }
  });

  // Engine Selector
  $$('.seg-btn').forEach(btn => {
    btn.onclick = () => {
      $$('.seg-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.selectedEngine = btn.dataset.engine;
      loadAssets();
    };
  });

  // Pipeline Selector
  $$('.pipe-chip').forEach(chip => {
    chip.onclick = () => {
      $$('.pipe-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      state.selectedPipeline = chip.dataset.pipeline;
      loadAssets();
    };
  });

  // Quick View Filter
  $$('.nav-item').forEach(item => {
    item.onclick = () => {
      $$('.nav-item').forEach(i => i.classList.remove('active'));
      item.classList.add('active');
      state.selectedFilter = item.dataset.filter;
      loadAssets();
    };
  });

  // Sort Selector
  $('#sort-select').onchange = (e) => {
    state.sortBy = e.target.value;
    loadAssets();
  };

  // View Toggles
  $('#view-grid').onclick = () => {
    $('#view-grid').classList.add('active');
    $('#view-compact').classList.remove('active');
    $('#asset-grid').classList.remove('compact');
  };
  $('#view-compact').onclick = () => {
    $('#view-compact').classList.add('active');
    $('#view-grid').classList.remove('active');
    $('#asset-grid').classList.add('compact');
  };

  // Modals Close
  $('#modal-close-btn').onclick = () => $('#detail-modal').classList.add('hidden');
  $('#detail-modal').onclick = (e) => { if (e.target.id === 'detail-modal') $('#detail-modal').classList.add('hidden'); };
  
  $('#sync-close-btn').onclick = () => $('#sync-modal').classList.add('hidden');
  $('#sync-modal').onclick = (e) => { if (e.target.id === 'sync-modal') $('#sync-modal').classList.add('hidden'); };
  $('#btn-open-sync').onclick = () => $('#sync-modal').classList.remove('hidden');

  // Modal Tabs
  $$('.modal-tab').forEach(tab => {
    tab.onclick = () => {
      $$('.modal-tab').forEach(t => t.classList.remove('active'));
      $$('.tab-content').forEach(c => c.classList.add('hidden'));
      tab.classList.add('active');
      $(`#tab-${tab.dataset.tab}`).classList.remove('hidden');
    };
  });

  // Copy Buttons
  $('#btn-copy-ai-ctx').onclick = () => {
    navigator.clipboard.writeText($('#modal-ai-preview').textContent);
    const b = $('#btn-copy-ai-ctx');
    b.textContent = '✅ Copied to Clipboard!';
    setTimeout(() => { b.textContent = '📋 Copy Markdown Context'; }, 1500);
  };

  $('#btn-modal-copy-id').onclick = () => {
    if (state.activeModalAsset) {
      navigator.clipboard.writeText(state.activeModalAsset.id);
      const b = $('#btn-modal-copy-id');
      b.textContent = '✅ Copied ID';
      setTimeout(() => { b.textContent = 'Copy Asset ID'; }, 1500);
    }
  };

  // Execute Unpack
  $('#btn-execute-unpack').onclick = executeUnpack;

  // Reset Filters
  $('#btn-reset-filters').onclick = () => {
    state.searchQuery = '';
    state.selectedCategory = 'all';
    state.selectedPipeline = 'all';
    state.selectedEngine = 'all';
    state.selectedFilter = 'all';
    searchInput.value = '';
    $$('.seg-btn').forEach(b => b.classList.toggle('active', b.dataset.engine === 'all'));
    $$('.pipe-chip').forEach(c => c.classList.toggle('active', c.dataset.pipeline === 'all'));
    $$('.nav-item').forEach(i => i.classList.toggle('active', i.dataset.filter === 'all'));
    $$('.cat-item').forEach(c => c.classList.toggle('active', c.dataset.cat === 'all'));
    loadAssets();
  };

  // Store Sync Actions
  $$('.btn-login-store').forEach(btn => {
    btn.onclick = async () => {
      const p = btn.dataset.provider;
      $('#sync-log-text').textContent = `Opening browser for ${p} sign-in...`;
      try {
        const r = await api(`/api/login/${p}`, { method: 'POST' });
        $('#sync-log-text').textContent = r.message;
      } catch (err) {
        $('#sync-log-text').textContent = `⚠ ${err.message}`;
      }
    };
  });

  $$('.btn-fetch-store').forEach(btn => {
    btn.onclick = async () => {
      const p = btn.dataset.provider;
      $('#sync-log-text').textContent = `Fetching ${p} library from store...`;
      try {
        const r = await api(`/api/fetch/${p}`, { method: 'POST' });
        $('#sync-log-text').textContent = `✅ Fetched ${r.assets_seen} packages from ${r.provider}.`;
        loadAssets();
      } catch (err) {
        $('#sync-log-text').textContent = `⚠ ${err.message}`;
      }
    };
  });

  $('#btn-run-scan').onclick = async () => {
    $('#sync-log-text').textContent = 'Scanning %APPDATA%/Unity and Epic VaultCache...';
    try {
      const r = await api('/api/scan-local', { method: 'POST' });
      $('#sync-log-text').textContent = `⚡ Found ${r.matched_to_library} on-disk packages (Unity: ${r.files_scanned.unity}, Fab: ${r.files_scanned.fab}).`;
      loadAssets();
    } catch (err) {
      $('#sync-log-text').textContent = `⚠ ${err.message}`;
    }
  };

  $('#btn-run-enrich').onclick = async () => {
    $('#sync-log-text').textContent = 'Enriching asset images and trailers batch...';
    try {
      const r = await api('/api/enrich', { method: 'POST' });
      $('#sync-log-text').textContent = `🖼 Enriched ${r.enriched} assets.`;
      loadAssets();
    } catch (err) {
      $('#sync-log-text').textContent = `⚠ ${err.message}`;
    }
  };
}

// Init
async function init() {
  setupEvents();
  await initCategories();
  await loadAssets();

  // set the recipes sidebar count from real data
  api('/api/recipes').then(r => {
    $('#count-recipes').textContent = (r.recipes || []).length;
  }).catch(() => {});
}

document.addEventListener('DOMContentLoaded', init);
