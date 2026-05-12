const DATA_DIR = 'data';

async function loadAllData() {
  try {
    const manifestResp = await fetch(`${DATA_DIR}/manifest.txt`);
    if (!manifestResp.ok) throw new Error('No manifest');
    const text = await manifestResp.text();
    const files = text.trim().split('\n').filter(x => x.endsWith('.json'));
    const daily = [];
    for (const f of files) {
      const r = await fetch(`${DATA_DIR}/${f}`);
      if (r.ok) daily.push(await r.json());
    }
    return daily;
  } catch {
    return [];
  }
}

function renderHealth(health) {
  const grid = document.getElementById('health-grid');
  if (!health || !health.length) { grid.innerHTML = '<div class="empty">No health data</div>'; return; }
  grid.innerHTML = health.map(h => `
    <div class="health-item ${h.status}">
      <span>${h.feed_title || h.feed_url}</span>
      <span class="latency">${h.status === 'alive' ? h.latency_ms + 'ms' : h.status}</span>
    </div>
  `).join('');
}

function renderArticles(articles) {
  const list = document.getElementById('articles-list');
  if (!articles.length) { list.innerHTML = '<div class="empty">No articles found</div>'; return; }
  list.innerHTML = articles.map(a => {
    const c = a.classification || {};
    const scoreClass = c.score >= 7 ? 'high' : c.score <= 3 ? 'low' : '';
    return `
    <div class="article-card">
      <div class="meta">
        <span class="score ${scoreClass}">${c.score || '?'}/10</span>
        <span class="category">${c.category || 'Unclassified'}</span>
      </div>
      <h3><a href="${a.link}" target="_blank">${a.title}</a></h3>
      <div class="summary">${c.summary || ''}</div>
      <div class="tags">${(c.tags || []).map(t => `<span class="tag">${t}</span>`).join('')}</div>
      <div class="feed-info">${a.feed_title} · ${a.feed_category} · ${a.published ? new Date(a.published).toLocaleDateString() : ''}</div>
    </div>`;
  }).join('');
}

function populateCategories(articles) {
  const cats = new Set(articles.map(a => a.classification?.category).filter(Boolean));
  const sel = document.getElementById('category-filter');
  cats.forEach(c => { const o = document.createElement('option'); o.value = c; o.textContent = c; sel.appendChild(o); });
}

async function init() {
  const daily = await loadAllData();
  if (!daily.length) {
    document.getElementById('articles-list').innerHTML = '<div class="empty">No data yet. Run the collector first.</div>';
    return;
  }

  const latest = daily[0];
  const allArticles = daily.flatMap(d => d.articles || []);

  document.getElementById('stats').textContent =
    `Feeds: ${latest.feeds_count || 0} | Alive: ${latest.stats?.feeds_alive || 0} | Articles: ${allArticles.length}`;

  renderHealth(latest.health);
  populateCategories(allArticles);

  let filtered = [...allArticles];

  function applyFilters() {
    const cat = document.getElementById('category-filter').value;
    const sort = document.getElementById('sort-by').value;
    const q = document.getElementById('search').value.toLowerCase();

    let result = [...allArticles];
    if (cat !== 'all') result = result.filter(a => a.classification?.category === cat);
    if (q) result = result.filter(a => a.title.toLowerCase().includes(q) || (a.classification?.summary || '').toLowerCase().includes(q));

    switch (sort) {
      case 'score-desc': result.sort((a, b) => (b.classification?.score || 0) - (a.classification?.score || 0)); break;
      case 'score-asc': result.sort((a, b) => (a.classification?.score || 0) - (b.classification?.score || 0)); break;
      case 'date-desc': result.sort((a, b) => new Date(b.published || 0) - new Date(a.published || 0)); break;
    }
    filtered = result;
    renderArticles(filtered);
  }

  document.getElementById('category-filter').addEventListener('change', applyFilters);
  document.getElementById('sort-by').addEventListener('change', applyFilters);
  document.getElementById('search').addEventListener('input', applyFilters);

  applyFilters();
}

init();
