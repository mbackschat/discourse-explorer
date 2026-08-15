document.addEventListener('DOMContentLoaded', function() {
  var network = Object.values(window).find(function(v) { return v && v.body && v.physics; });
  if (!network) return;

  // Loading overlay: visible by default at page load; the helpers below
  // toggle it for the heavy Node-view re-render. The CSS spinner runs
  // on the compositor thread (transform-based @keyframes) so it keeps
  // animating while the main thread is blocked on nodeData.add().
  function showLoading(msg) {
    var el = document.getElementById('loading-overlay');
    if (!el) return;
    if (msg) {
      var t = el.querySelector('.loading-text');
      if (t) t.textContent = msg;
    }
    el.classList.remove('hidden');
  }
  function hideLoading() {
    var el = document.getElementById('loading-overlay');
    if (el) el.classList.add('hidden');
  }
  // Run a synchronous payload AFTER a paint, so the loading overlay (or
  // any newly-shown UI) actually renders before the work blocks the main
  // thread. Two nested rAF guarantees the prior frame painted: rAF #1
  // fires before paint, rAF #2 fires after.
  function runAfterPaint(fn) {
    requestAnimationFrame(function() {
      requestAnimationFrame(fn);
    });
  }

  var nodeData = network.body.data.nodes;
  var edgeData = network.body.data.edges;

  // Populate the live DataSets from the sibling data.js file. See
  // visualize.py for the rationale: keeps graph.html small (~500 KB) and
  // outsources the ~19 MB of node+edge payload to a sibling .js file that
  // loads via <script src> — fetchable even from file:// URLs (a plain
  // .json via fetch() would be blocked by Chrome's CORS policy for local
  // files). If GRAPH_DATA is missing, the viz will still load but show an
  // empty network — usually a sign the user moved graph.html out of its
  // <data-dir>/visualize/ directory.
  if (window.GRAPH_DATA && window.GRAPH_DATA.nodes) {
    nodeData.add(window.GRAPH_DATA.nodes);
    edgeData.add(window.GRAPH_DATA.edges);
  }

  var allNodes = nodeData.get();
  var allEdges = edgeData.get();

  // Snapshot the original cached layout positions (from layout.json via the
  // data.js bake) so the user can restore them after Enable Physics has
  // drifted nodes apart. Frozen at init; physics simulation never writes
  // back to this map.
  var _originalPositions = {};
  allNodes.forEach(function(n) {
    if (typeof n.x === 'number' && typeof n.y === 'number') {
      _originalPositions[n.id] = {x: n.x, y: n.y};
    }
  });

  // Cache original data for view toggling
  var originalNodes = allNodes.map(function(n) { return Object.assign({}, n); });
  var originalEdges = allEdges.map(function(e) { return Object.assign({}, e); });

  // Lookup table by edge ID, used by applyFilters to restore the
  // original color + width after highlightPaths overrides them. Built
  // once; doesn't need to change during the session.
  var originalEdgeById = Object.create(null);
  originalEdges.forEach(function(e) { originalEdgeById[e.id] = e; });

  // Per-node degree rank (1 = highest), computed once over the full real
  // graph. Surfaces in the detail panel as "rank #N of TOTAL" — turns the
  // raw degree number into a "is this important?" answer.
  var degreeRank = {};
  var totalNodeCount = originalNodes.length;
  (function () {
    var sorted = originalNodes.slice().sort(function (a, b) {
      return (b.degree || 0) - (a.degree || 0);
    });
    sorted.forEach(function (n, i) { degreeRank[n.id] = i + 1; });
  })();

  // Build neighbor + edge indexes
  var neighborIndex = {};
  var nodeEdgeIndex = {};
  allNodes.forEach(function(n) { neighborIndex[n.id] = []; nodeEdgeIndex[n.id] = []; });
  allEdges.forEach(function(e) {
    if (neighborIndex[e.from]) neighborIndex[e.from].push(e.to);
    if (neighborIndex[e.to]) neighborIndex[e.to].push(e.from);
    if (nodeEdgeIndex[e.from]) nodeEdgeIndex[e.from].push(e.id);
    if (nodeEdgeIndex[e.to]) nodeEdgeIndex[e.to].push(e.id);
  });

  function rebuildIndexes() {
    neighborIndex = {}; nodeEdgeIndex = {};
    allNodes.forEach(function(n) { neighborIndex[n.id] = []; nodeEdgeIndex[n.id] = []; });
    allEdges.forEach(function(e) {
      if (neighborIndex[e.from]) neighborIndex[e.from].push(e.to);
      if (neighborIndex[e.to]) neighborIndex[e.to].push(e.from);
      if (nodeEdgeIndex[e.from]) nodeEdgeIndex[e.from].push(e.id);
      if (nodeEdgeIndex[e.to]) nodeEdgeIndex[e.to].push(e.id);
    });
  }

  function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

  // ===================== State =====================
  var viewMode = 'node';   // 'node' | 'category' | 'entityType'
  var currentCategory = null;  // super-category drilled into for entityType view
  var focusState = null;   // {nodeId, hops} or null
  var pathfindState = null; // {fromId} or null
  var pathsHighlighted = false; // true while highlightPaths' edge color/width override is active; applyFilters clears
  var _pathNodeIds = null; // Set of nodes appearing in any active highlighted path; null when none active. Read by applyLabelMode to suppress non-path labels (otherwise dimmed nodes keep bright labels and the canvas stays noisy).
  var communityFilter = null; // integer community ID or null; honored by applyFilters
  var searchShowContext = true; // when false, applyFilters skips the 1-hop neighbor dim expansion for search matches
  var physOn = false;
  // Label LOD. Default 'hubs': only top-N (isHub=true) labels drawn so a
  // zoom-in doesn't flood the canvas. Super-nodes in category/entityType
  // views always keep their labels regardless of mode.
  var labelMode = 'hubs';  // 'hubs' | 'all' | 'none'

  // Label-LOD settings, configurable from the "Display" section in the
  // control panel and overridable via URL params:
  //   ?hubLabels=200&viewportThreshold=80&allCap=300
  // hubLabels defaults to whatever --hub-label-count baked into the graph
  // (GRAPH_META.hubLabelCount); the other two are JS-side defaults.
  var DEFAULT_LABEL_SETTINGS = {
    hubLabels: (window.GRAPH_META && GRAPH_META.hubLabelCount) || 100,
    viewportThreshold: 60,
    allCap: 200,
  };
  var labelSettings = Object.assign({}, DEFAULT_LABEL_SETTINGS);
  (function applyUrlParamOverrides() {
    try {
      var params = new URLSearchParams(window.location.search);
      ['hubLabels', 'viewportThreshold', 'allCap'].forEach(function(k) {
        if (!params.has(k)) return;
        var v = parseInt(params.get(k), 10);
        if (!isNaN(v) && v >= 0) labelSettings[k] = v;
      });
    } catch (e) {}
  })();

  // Detail panel navigation history. Entries are {kind, payload}:
  //   kind='node' / 'category' → payload is a nodeId string
  //   kind='edge'              → payload is an array of edge IDs
  var navHistory = [];
  var navIndex = -1;
  var navProgrammatic = false;

  function navPush(entry) {
    if (navProgrammatic) return;
    // Backward-compat: legacy callers passed a bare nodeId string.
    if (typeof entry === 'string') entry = {kind: 'node', payload: entry};
    // Coalesce repeats: clicking the same thing twice in a row is one entry.
    var top = navHistory[navIndex];
    if (top && top.kind === entry.kind && _samePayload(top.payload, entry.payload)) {
      return;
    }
    if (navIndex < navHistory.length - 1) navHistory = navHistory.slice(0, navIndex + 1);
    navHistory.push(entry);
    navIndex = navHistory.length - 1;
    navUpdateButtons();
  }
  function _samePayload(a, b) {
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) {
      if (a.length !== b.length) return false;
      for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
      return true;
    }
    return false;
  }
  function navUpdateButtons() {
    document.getElementById('nav-back').disabled = (navIndex <= 0);
    document.getElementById('nav-forward').disabled = (navIndex >= navHistory.length - 1);
  }
  function navGoTo(idx) {
    navIndex = idx;
    navProgrammatic = true;
    var entry = navHistory[idx];
    if (entry.kind === 'edge') {
      // showEdgeDetail handles its own selectEdges + highlight.
      showEdgeDetail(entry.payload);
    } else if (entry.kind === 'category') {
      network.selectNodes([entry.payload]);
      network.focus(entry.payload, {scale: 1.2, animation: {duration: 300}});
      showCategoryDetail(entry.payload);
    } else if (entry.kind === 'cluster') {
      showClusterDetail(entry.payload);
    } else if (entry.kind === 'cat-edge') {
      var pair = String(entry.payload).split('||');
      if (pair.length === 2) showCategoryEdgeDetail(pair[0], pair[1]);
    } else {
      network.selectNodes([entry.payload]);
      network.focus(entry.payload, {scale: 1.2, animation: {duration: 300}});
      showNodeDetail(entry.payload);
    }
    navProgrammatic = false;
    navUpdateButtons();
  }

  // ===================== Filtering =====================
  var debounceTimer = null;
  function debounce(fn, ms) {
    return function() { clearTimeout(debounceTimer); debounceTimer = setTimeout(fn, ms); };
  }

  function getActiveTypes() {
    var s = new Set();
    document.querySelectorAll('.type-cb:checked').forEach(function(cb) { s.add(cb.dataset.cat); });
    return s;
  }

  function getActiveRelTypes() {
    var s = new Set();
    document.querySelectorAll('.rel-cb:checked').forEach(function(cb) { s.add(cb.dataset.rel); });
    return s;
  }

  // ===================== Time-window helpers =====================
  // Source-post time gate. Edges carry `tm`/`tM` (month-bin indices since
  // 2018-01) attached at build time; build skips edges with no resolvable
  // source-topic createdAt — those pass any window unconditionally
  // (no signal, no penalty). Per-node "passes time" is derived from
  // edges-in-window for nodes that have any incident edge; isolated nodes
  // inherit the cascade behaviour and stay visible.
  function _readTimeWindow() {
    var minS = document.getElementById('time-min-slider');
    var maxS = document.getElementById('time-max-slider');
    if (!minS || !maxS) return null;
    var lo = parseInt(minS.value, 10);
    var hi = parseInt(maxS.value, 10);
    var bMin = parseInt(minS.dataset.minBin, 10);
    var bMax = parseInt(minS.dataset.maxBin, 10);
    if (isNaN(lo) || isNaN(hi) || isNaN(bMin) || isNaN(bMax)) return null;
    return {lo: lo, hi: hi, bMin: bMin, bMax: bMax,
            active: (lo > bMin) || (hi < bMax)};
  }
  function _edgePassTime(e, tw) {
    if (!tw || !tw.active) return true;
    if (e.tm == null || e.tM == null) return true;
    return e.tM >= tw.lo && e.tm <= tw.hi;
  }
  function _monthBinToLabel(bin) {
    // Mirror of visualize.py:_month_bin_label. Epoch is 2018-01.
    var year = 2018 + Math.floor(bin / 12);
    var month = 1 + (bin % 12);
    return year + '-' + (month < 10 ? '0' + month : month);
  }
  function _updateTimeFill(tw) {
    var fill = document.getElementById('time-slider-fill');
    if (!fill || !tw) return;
    var span = tw.bMax - tw.bMin;
    if (span <= 0) { fill.style.left = '0%'; fill.style.width = '0%'; return; }
    var leftPct = ((tw.lo - tw.bMin) / span) * 100;
    var widthPct = ((tw.hi - tw.lo) / span) * 100;
    fill.style.left = leftPct + '%';
    fill.style.width = widthPct + '%';
  }
  function _tsToYearMonth(unixSec) {
    var d = new Date(unixSec * 1000);
    var y = d.getUTCFullYear();
    var m = d.getUTCMonth() + 1;
    return y + '-' + (m < 10 ? '0' + m : m);
  }
  function _quarterFromMonthBin(bin) {
    var year = 2018 + Math.floor(bin / 12);
    var monthZero = bin % 12;
    var q = Math.floor(monthZero / 3) + 1;
    return year + '-Q' + q;
  }
  // Per-entity time-range chip for the node detail panel meta line. Sources
  // both the node's own topicIds and every incident edge's topicIds (mirrors
  // _unionAndRankTopicIds), resolves each via GRAPH_META.topicIndex.createdAt.
  // Returns '' when the union has no resolvable timestamp — silent skip.
  function _buildTimeRangeChip(node, connEdgeIds) {
    var topicIndex = (window.GRAPH_META && GRAPH_META.topicIndex) || {};
    var unionedTids = new Set(node.topicIds || []);
    var incidentBins = [];
    (connEdgeIds || []).forEach(function(eid) {
      var e = edgeData.get(eid);
      if (!e) return;
      (e.topicIds || []).forEach(function(tid) { unionedTids.add(tid); });
      if (typeof e.tm === 'number') incidentBins.push(e.tm);
    });
    if (unionedTids.size === 0) return '';
    var tsList = [];
    unionedTids.forEach(function(tid) {
      var d = topicIndex[tid];
      if (!d || !d.createdAt) return;
      var t = Date.parse(d.createdAt);
      if (!isNaN(t)) tsList.push(t / 1000);
    });
    if (!tsList.length) return '';
    var minTs = tsList[0], maxTs = tsList[0];
    for (var i = 1; i < tsList.length; i++) {
      if (tsList[i] < minTs) minTs = tsList[i];
      if (tsList[i] > maxTs) maxTs = tsList[i];
    }
    var minLabel = _tsToYearMonth(minTs);
    var maxLabel = _tsToYearMonth(maxTs);
    var rangeLabel = (minLabel === maxLabel) ? minLabel : (minLabel + ' – ' + maxLabel);
    // Peaked-quarter annotation: only meaningful when ≥4 incident edges
    // span ≥2 quarters AND one quarter holds ≥25% of edges. Below that,
    // the "peak" is noise — better to just show the range.
    var peakedSuffix = '';
    if (incidentBins.length >= 4) {
      var byQ = Object.create(null);
      incidentBins.forEach(function(b) {
        var q = _quarterFromMonthBin(b);
        byQ[q] = (byQ[q] || 0) + 1;
      });
      var entries = Object.keys(byQ).map(function(q) { return [q, byQ[q]]; });
      entries.sort(function(a, b) { return b[1] - a[1]; });
      if (entries.length > 1
          && entries[0][1] >= 3
          && entries[0][1] >= incidentBins.length * 0.25) {
        peakedSuffix = ' · peaked ' + entries[0][0] + ' (' + entries[0][1] + ' edges)';
      }
    }
    return '<span class="time-range-chip" title="Source-post time range across this node\'s topic provenance + incident edges (resolved via GRAPH_META.topicIndex.createdAt).">Active ' +
           rangeLabel + peakedSuffix + '</span>';
  }

  // ===================== Stats / Query command builders =====================
  // Generate a copy-paste-ready CLI command for the inspected entity, scoped
  // to the corpus's data dir (shipped in GRAPH_META.dataDir). Defaults are
  // entity-type-aware: Topic → SQL on posts.topic_id, User → SQL on posts.username,
  // Tag/Category → view-scoped SQL, everything else → free-text search across
  // post bodies. For edges, the SQL ANDs both endpoint labels into the same
  // post — useful for finding evidence of co-occurrence.
  function _shQuote(s) {
    // Wrap in double quotes; escape \ and " (the only chars that need
    // escaping inside a "..." shell arg).
    return '"' + String(s).replace(/[\\"]/g, '\\$&') + '"';
  }
  function _sqlEscape(s) {
    // Single-quoted SQL string literal: ' → ''. The outer shell layer
    // wraps this whole SQL in "...", so single quotes don't conflict.
    return String(s).replace(/'/g, "''");
  }
  function _statsCmd(args) {
    var dataDir = (window.GRAPH_META && GRAPH_META.dataDir) || '<DATA_DIR>';
    return 'uv run discourse-explorer stats --path ' + _shQuote(dataDir) + ' ' + args;
  }
  function _queryCmd(question) {
    var dataDir = (window.GRAPH_META && GRAPH_META.dataDir) || '<DATA_DIR>';
    return 'uv run discourse-explorer query ' + _shQuote(dataDir) + ' ' + _shQuote(question);
  }
  function _buildStatsCommandForNode(node) {
    var label = node.id || node.label;
    var cat = node.superCategory || '';
    var etype = (node.entityType || '').toLowerCase();
    if (cat === 'Topic' && node.topicIds && node.topicIds.length) {
      var tid = parseInt(node.topicIds[0], 10);
      if (!isNaN(tid)) {
        return _statsCmd('sql ' + _shQuote(
          "SELECT post_number, username, length(plain_text) AS chars, plain_text " +
          "FROM posts WHERE topic_id = " + tid +
          " ORDER BY post_number LIMIT 50"
        ));
      }
    }
    if (cat === 'User' || etype === 'user') {
      return _statsCmd('sql ' + _shQuote(
        "SELECT topic_id, post_number, length(plain_text) AS chars " +
        "FROM posts WHERE username = '" + _sqlEscape(label) + "' " +
        "ORDER BY topic_id, post_number LIMIT 50"
      ));
    }
    if (cat === 'Tag' || etype === 'tag') {
      return _statsCmd('sql ' + _shQuote(
        "SELECT t.id, t.title, t.created_at " +
        "FROM topic_tags tt JOIN topics t ON tt.topic_id = t.id " +
        "WHERE tt.tag = '" + _sqlEscape(label) + "' " +
        "ORDER BY t.created_at DESC LIMIT 50"
      ));
    }
    if (cat === 'Category' || etype === 'category') {
      return _statsCmd('sql ' + _shQuote(
        "SELECT id, title, created_at, last_posted_at " +
        "FROM topics WHERE category_name = '" + _sqlEscape(label) + "' " +
        "ORDER BY created_at DESC LIMIT 50"
      ));
    }
    // Generic fallback for Component / Issue / Document / etc.: free-text
    // search across post bodies. Uses the existing `stats search` subcommand,
    // which is plain-text ILIKE-style — works for any entity name.
    return _statsCmd('search ' + _shQuote(label));
  }
  function _buildStatsCommandForEdge(edge) {
    var fromNode = nodeData.get(edge.from);
    var toNode = nodeData.get(edge.to);
    var fromLabel = edge.from;
    var toLabel = edge.to;
    return _statsCmd('sql ' + _shQuote(
      "SELECT topic_id, post_number, username, plain_text " +
      "FROM posts " +
      "WHERE plain_text ILIKE '%" + _sqlEscape(fromLabel) + "%' " +
      "AND plain_text ILIKE '%" + _sqlEscape(toLabel) + "%' " +
      "LIMIT 50"
    ));
  }
  function _buildQueryCommandForNode(node) {
    return _queryCmd('Tell me about ' + (node.id || node.label) + '.');
  }
  function _buildQueryCommandForEdge(edge) {
    var fromNode = nodeData.get(edge.from);
    var toNode = nodeData.get(edge.to);
    var fromLabel = edge.from;
    var toLabel = edge.to;
    return _queryCmd('How does ' + fromLabel + ' connect to ' + toLabel + '?');
  }
  // Render the Stats + Query buttons for an action row. anchorPrefix is
  // appended with -stats / -query to give each its own copy-toast id.
  function _attrEsc(s) {
    // HTML-escape for use inside a "..."-quoted attribute value. esc()
    // (textContent → innerHTML) only handles &, <, >; attributes also
    // need " escaped or it breaks the attribute boundary.
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  // Split-button [Query][▾] mirroring the Copy split-button. Primary
  // copies the natural-language `query` command (entity → "Tell me about
  // X", edge → "How does A connect to B?"). The ▾ menu offers Stats
  // (the structural-SQL `stats` command) for the analytical drill-down.
  // Stashing both commands on the toggle's data-attrs avoids escaping
  // the SQL's literal " characters into an inline onclick.
  function _renderStatsQuerySplit(cmds, anchorPrefix) {
    if (!cmds.query && !cmds.stats) return '';
    var safe = String(anchorPrefix).replace(/[^a-zA-Z0-9-]/g, '_');
    var anchorId = safe + '-qs';
    var qAttr = _attrEsc(cmds.query || '');
    var sAttr = _attrEsc(cmds.stats || '');
    var titleAttr = cmds.query ? (' title="Copy: ' + qAttr + '"')
                               : (' title="Copy: ' + sAttr + '"');
    var primaryCmd = cmds.query || cmds.stats;
    return '<span class="copy-split">' +
           '<button class="action-btn copy-main statsq-primary" id="' + anchorId +
           '" data-qs-cmd="' + _attrEsc(primaryCmd) + '"' + titleAttr +
           '>' + (cmds.query ? 'Query' : 'Stats') + '</button>' +
           '<button class="action-btn copy-toggle statsq-toggle"' +
           ' data-qs-query="' + qAttr + '"' +
           ' data-qs-stats="' + sAttr + '"' +
           ' data-qs-anchor="' + anchorId +
           '" onclick="openQsMenu(this)" title="More CLI commands">&#9662;</button>' +
           '</span>';
  }
  function _ensureQsMenu() {
    var m = document.getElementById('qs-menu');
    if (m) return m;
    m = document.createElement('div');
    m.id = 'qs-menu';
    m.className = 'copy-menu';
    m.innerHTML =
      '<div class="copy-menu-item" data-format="query">Copy as Query</div>' +
      '<div class="copy-menu-item" data-format="stats">Copy as Stats command</div>';
    document.body.appendChild(m);
    m.addEventListener('click', function(evt) {
      var item = evt.target.closest('.copy-menu-item');
      if (!item) return;
      var fmt = item.dataset.format;
      var cmd = (fmt === 'query') ? m.dataset.qsQuery : m.dataset.qsStats;
      var anchor = m.dataset.qsAnchor;
      if (cmd) copyToClipboard(cmd, anchor);
      _closeQsMenu();
    });
    return m;
  }
  function _closeQsMenu() {
    var m = document.getElementById('qs-menu');
    if (m) m.style.display = 'none';
    document.removeEventListener('mousedown', _outsideQsHandler, true);
    document.removeEventListener('keydown', _escQsHandler, true);
  }
  function _outsideQsHandler(evt) {
    var m = document.getElementById('qs-menu');
    if (!m || m.style.display === 'none') return;
    if (m.contains(evt.target)) return;
    if (evt.target.closest('.statsq-toggle')) return;
    _closeQsMenu();
  }
  function _escQsHandler(evt) {
    if (evt.key === 'Escape') _closeQsMenu();
  }
  function openQsMenu(toggleBtn) {
    var m = _ensureQsMenu();
    m.dataset.qsQuery = toggleBtn.dataset.qsQuery || '';
    m.dataset.qsStats = toggleBtn.dataset.qsStats || '';
    m.dataset.qsAnchor = toggleBtn.dataset.qsAnchor || '';
    var rect = toggleBtn.getBoundingClientRect();
    m.style.display = 'block';
    var menuRect = m.getBoundingClientRect();
    var left = rect.right - menuRect.width;
    if (left < 6) left = 6;
    if (left + menuRect.width > window.innerWidth - 6) left = window.innerWidth - menuRect.width - 6;
    m.style.left = left + 'px';
    m.style.top = (rect.bottom + 4) + 'px';
    setTimeout(function() {
      document.addEventListener('mousedown', _outsideQsHandler, true);
      document.addEventListener('keydown', _escQsHandler, true);
    }, 0);
  }
  window.openQsMenu = openQsMenu;

  // Auto-fit camera state (used by applyFilters end-of-pass).
  // Layout is anchored at build time, so filtering only hides nodes —
  // visible ones stay wherever they were originally placed (often a
  // small canvas region while the rest is empty). A camera-only refit on
  // filter change preserves the spatial mental map (relayout would
  // destroy it) while making the visible set fill the viewport.
  var _autoFitDebounce = null;
  var _autoFitInitialized = false;
  var _lastAutoFitVisibleSize = -1;

  function applyFilters() {
    if (viewMode === 'category') return;
    var hadFocus = focusState !== null;
    focusState = null;

    var activeTypes = getActiveTypes();
    var activeRels = getActiveRelTypes();
    var minDeg = parseInt(document.getElementById('degree-slider').value, 10);
    var minWt = parseFloat(document.getElementById('weight-slider').value);
    var q = (document.getElementById('search-input').value || '').toLowerCase();

    // Time-window precompute: when active, walk edges once to collect
    // the set of nodes that have at least one in-window incident edge.
    // This drives the per-node time check used by visibleIds AND
    // passableNonSearch (so search-context + pin neighbors honour the
    // time gate symmetrically with type/community gates).
    var tw = _readTimeWindow();
    var nodeTimeOk = null;
    var edgesInWindow = 0;
    if (tw && tw.active) {
      nodeTimeOk = new Set();
      allEdges.forEach(function(e) {
        if (_edgePassTime(e, tw)) {
          edgesInWindow++;
          nodeTimeOk.add(e.from);
          nodeTimeOk.add(e.to);
        }
      });
    } else {
      edgesInWindow = allEdges.length;
    }
    function _nodePassTime(n) { return !nodeTimeOk || nodeTimeOk.has(n.id); }

    // Pinned node: currently selected/inspected node stays visible with its neighbors
    var pinned = network.getSelectedNodes();
    var pinnedId = (pinned && pinned.length === 1) ? pinned[0] : null;
    var pinnedNeighbors = new Set();
    if (pinnedId) {
      pinnedNeighbors.add(pinnedId);
      (neighborIndex[pinnedId] || []).forEach(function(nid) { pinnedNeighbors.add(nid); });
    }

    var visibleIds = new Set();
    var matchIds = new Set();
    // Per-row leave-one-out tallies for the sidebar. For each entity
    // type T, M_type[T] = how many T-typed nodes pass all NON-type
    // filters — answers "if I re-checked T, what would I gain". Same
    // shape for degMaxNoThreshold (nodes ignoring the degree gate but
    // honouring all other filters).
    var typeL = Object.create(null);
    var typeM = Object.create(null);
    var degMaxNoThreshold = 0;

    // Nodes that pass every active filter EXCEPT the search query — the
    // candidate set for search-context neighbor dimming below. Without this
    // gate, jdoe's 100+ Topic/Component neighbors would still show up
    // dimmed even when only the User entity-type checkbox is selected,
    // contradicting the legend (`Topic: 0`) and the Nodes stat.
    var passableNonSearch = new Set();
    allNodes.forEach(function(n) {
      var cat = n.superCategory || 'Other';
      var typeOk = activeTypes.has(cat);
      var deg = n.degree || 0;
      var degOk = deg >= minDeg;
      // Full-text: match against label, free-form description, and the
      // raw entity type. The table view (renderTable) already searches
      // these fields; bringing the canvas search to parity surfaces
      // entities whose name doesn't contain the query but whose
      // description does — common when searching for concepts
      // ("permission", "migration") that appear in prose, not in names.
      var searchOk = !q || (
        // Search the FULL node id (canonical text) AND the truncated
        // canvas label. Without the n.id leg, queries that target the
        // tail of a long topic title would fail to match — the
        // truncated label would lose the back half after `…`.
        (n.id || '').toLowerCase().indexOf(q) !== -1 ||
        (n.label || '').toLowerCase().indexOf(q) !== -1 ||
        (n.fullDescription || '').toLowerCase().indexOf(q) !== -1 ||
        (n.entityType || '').toLowerCase().indexOf(q) !== -1
      );
      // Community lock: when active, only nodes in the locked community pass.
      var commOk = (communityFilter === null) || (n.community === communityFilter);
      // Time gate: derived from the per-edge in-window pass at the top of
      // applyFilters. Nodes with no incident in-window edge are filtered out.
      var timeOk = _nodePassTime(n);
      if (typeOk && degOk && commOk && timeOk) passableNonSearch.add(n.id);
      if (typeOk && degOk && searchOk && commOk && timeOk) {
        visibleIds.add(n.id);
        if (q && searchOk) matchIds.add(n.id);
        typeL[cat] = (typeL[cat] || 0) + 1;
      }
      // M_type: passes deg + search + comm + time (regardless of type checkbox).
      if (degOk && searchOk && commOk && timeOk) {
        typeM[cat] = (typeM[cat] || 0) + 1;
      }
      // M for the Min Degree slider: passes type + search + comm + time
      // (regardless of the degree threshold).
      if (typeOk && searchOk && commOk && timeOk) {
        degMaxNoThreshold++;
      }
    });

    var neighborIds = new Set();
    if (q && matchIds.size > 0 && searchShowContext) {
      matchIds.forEach(function(mid) {
        (neighborIndex[mid] || []).forEach(function(nid) {
          // Search-context neighbors must still pass type/degree/community
          // filters — they only get a pass on the search predicate.
          if (!visibleIds.has(nid) && passableNonSearch.has(nid)) neighborIds.add(nid);
        });
      });
    }

    nodeData.update(allNodes.map(function(n) {
      if (visibleIds.has(n.id)) return {id: n.id, hidden: false, opacity: 1};
      if (neighborIds.has(n.id)) return {id: n.id, hidden: false, opacity: 0.25};
      // Pinned node itself: explicit click is sticky; the (+pin) stat suffix
      // flags the override. Pin neighbors are derived UI — they must still
      // pass every active filter (type / degree / community), exactly like
      // search-context neighbors, otherwise unchecking a type leaves the pin's
      // halo glowing in the surprising shape of the pre-filter neighborhood.
      if (pinnedId && n.id === pinnedId) return {id: n.id, hidden: false, opacity: 1};
      if (pinnedNeighbors.has(n.id) && passableNonSearch.has(n.id)) return {id: n.id, hidden: false, opacity: 0.3};
      return {id: n.id, hidden: true};
    }));

    var allVisible = new Set(visibleIds);
    neighborIds.forEach(function(nid) { allVisible.add(nid); });
    pinnedNeighbors.forEach(function(nid) { allVisible.add(nid); });
    var visEdgeCount = 0;
    // Per-rel leave-one-out tallies + min-weight slider M.
    var relL = Object.create(null);
    var relM = Object.create(null);
    var weightMaxNoThreshold = 0;
    edgeData.update(allEdges.map(function(e) {
      var relCat = e.relCategory || 'Other';
      var relOk = activeRels.has(relCat);
      var weightOk = (e.edgeWeight || 1) >= minWt;
      var timeOkE = _edgePassTime(e, tw);
      // Strict endpointsVisible: BOTH endpoints pass all node filters.
      // (Different from `allVisible`, which loosens to include
      // pinned-neighbour and search-1-hop dimmed nodes.)
      var endpointsVisible = visibleIds.has(e.from) && visibleIds.has(e.to);
      if (endpointsVisible && weightOk && timeOkE) {
        relM[relCat] = (relM[relCat] || 0) + 1;
        if (relOk) relL[relCat] = (relL[relCat] || 0) + 1;
      }
      if (relOk && endpointsVisible && timeOkE) weightMaxNoThreshold++;
      // Pinned edges: always show edges touching pinned node regardless of rel/weight filter
      var canvasEndpointsVisible = allVisible.has(e.from) && allVisible.has(e.to);
      var isPinnedEdge = pinnedId && (e.from === pinnedId || e.to === pinnedId) && canvasEndpointsVisible;
      var show = isPinnedEdge || (canvasEndpointsVisible && weightOk && relOk && timeOkE);
      if (show) visEdgeCount++;
      var update = {id: e.id, hidden: !show};
      // If a previous highlightPaths run overrode color + width, restore
      // the originals here so cleared paths actually disappear instead
      // of staying drawn dim and thin.
      if (pathsHighlighted) {
        var orig = originalEdgeById[e.id];
        if (orig) { update.color = orig.color; update.width = orig.width; }
      }
      return update;
    }));
    if (pathsHighlighted) {
      // Path nodes also picked up borderWidth: 3; restore the default.
      nodeData.update(allNodes.map(function (n) { return {id: n.id, borderWidth: 1}; }));
      pathsHighlighted = false;
      _pathNodeIds = null;
    }

    // Per-row sidebar update. Both type-rows and rel-rows share the
    // same `.row-count` + `.gain-pill` structure (visualize.py emits
    // both via _filter_row), so one update routine covers both.
    function _updateFilterRow(label, currentL, leaveOneOutM) {
      var cb = label.querySelector('input[type="checkbox"]');
      var box = label.querySelector('.row-count');
      var visSpan = label.querySelector('.row-count-visible');
      var pill = label.querySelector('.gain-pill');
      if (!cb || !box || !visSpan) return;
      var total = parseInt(box.dataset.total, 10) || 0;
      visSpan.textContent = currentL;
      if (currentL === total) box.classList.add('equal');
      else box.classList.remove('equal');
      if (pill) {
        // Show the +M gain pill only when the row's filter is OFF
        // AND there's a non-zero gain to recover.
        if (!cb.checked && leaveOneOutM > 0) {
          pill.textContent = '+' + leaveOneOutM;
          pill.style.display = '';
        } else {
          pill.style.display = 'none';
        }
      }
    }
    document.querySelectorAll('.type-filter').forEach(function (label) {
      var cb = label.querySelector('.type-cb');
      if (!cb) return;
      _updateFilterRow(label, typeL[cb.dataset.cat] || 0, typeM[cb.dataset.cat] || 0);
    });
    document.querySelectorAll('.rel-filter').forEach(function (label) {
      var cb = label.querySelector('.rel-cb');
      if (!cb) return;
      _updateFilterRow(label, relL[cb.dataset.rel] || 0, relM[cb.dataset.rel] || 0);
    });

    // Slider counts: "L (M)" with a "−drop" pill when the threshold
    // hides items. Pill is clickable to reset the slider to its min.
    function _updateSlider(countId, pillId, L, M) {
      var countEl = document.getElementById(countId);
      var pill = document.getElementById(pillId);
      if (countEl) {
        if (L === M) {
          countEl.textContent = String(L);
        } else {
          countEl.textContent = L + ' (' + M + ')';
        }
      }
      if (pill) {
        var drop = M - L;
        if (drop > 0) {
          pill.textContent = '−' + drop;
          pill.style.display = '';
        } else {
          pill.style.display = 'none';
        }
      }
    }
    _updateSlider('degree-count', 'degree-drop-pill', visibleIds.size, degMaxNoThreshold);
    var weightL = 0;
    Object.keys(relL).forEach(function (k) { weightL += relL[k]; });
    _updateSlider('weight-count', 'weight-drop-pill', weightL, weightMaxNoThreshold);

    // Time-window readout + drop pill. Same pattern as the degree/weight
    // sliders: show "−N edges" when narrowed, hide when full-range.
    if (tw) {
      var twLabel = document.getElementById('time-window-label');
      if (twLabel) {
        twLabel.textContent = _monthBinToLabel(tw.lo) + ' → ' + _monthBinToLabel(tw.hi);
      }
      var twPill = document.getElementById('time-drop-pill');
      if (twPill) {
        var dropped = allEdges.length - edgesInWindow;
        if (tw.active && dropped > 0) {
          twPill.textContent = '−' + dropped;
          twPill.style.display = '';
        } else {
          twPill.style.display = 'none';
        }
      }
      _updateTimeFill(tw);
    }

    var dimSuffix = neighborIds.size > 0 ? ' (+' + neighborIds.size + ' dim)' : '';
    // Pin-context: pinnedNeighbors that survived strict gating and weren't
    // already counted as visibleIds or search-neighborIds. Surfacing this as
    // a stat suffix keeps the count honest about what's on the canvas — the
    // user can read a non-zero number and know exactly where the dim halo
    // came from instead of being surprised by it.
    var pinContextCount = 0;
    if (pinnedId && pinnedNeighbors.size > 0) {
      pinnedNeighbors.forEach(function(nid) {
        if (passableNonSearch.has(nid) && !visibleIds.has(nid) && !neighborIds.has(nid)) pinContextCount++;
      });
    }
    var pinContextSuffix = pinContextCount > 0 ? ' (+' + pinContextCount + ' pin-context)' : '';
    setStats([
      {label: 'Nodes', value: visibleIds.size.toLocaleString() + ' / ' + allNodes.length.toLocaleString() +
                              dimSuffix +
                              (pinnedId ? ' (+1 pinned)' : '') +
                              pinContextSuffix},
      {label: 'Edges', value: visEdgeCount.toLocaleString() + ' / ' + allEdges.length.toLocaleString()},
    ]);
    var sr = document.getElementById('search-results');
    sr.textContent = q ? (matchIds.size + ' match' + (matchIds.size === 1 ? '' : 'es')) : '';
    // Leaf breadcrumb segment depends on which type-cb is checked; refresh
    // so toggling entity-type filters updates the trail.
    if (typeof renderBreadcrumb === 'function') renderBreadcrumb();

    // Auto-fit camera to visible nodes when the filter changes the
    // visible-set size. Skipped on first call (init does its own fit)
    // and when nothing is visible (network.fit on an empty array
    // misbehaves). Debounced 300 ms so slider drags (live-applied at
    // 100 ms) don't trigger constant camera motion. focusOnNode handles
    // its own fit, but applyFilters always runs with focusState already
    // cleared (line above) so no extra check needed here.
    if (_autoFitInitialized && visibleIds.size !== _lastAutoFitVisibleSize && visibleIds.size > 0) {
      if (_autoFitDebounce) clearTimeout(_autoFitDebounce);
      var idsForFit = Array.from(visibleIds);
      _autoFitDebounce = setTimeout(function() {
        network.fit({
          nodes: idsForFit,
          animation: {duration: 250, easingFunction: 'easeInOutQuad'},
        });
      }, 300);
    }
    _lastAutoFitVisibleSize = visibleIds.size;
    _autoFitInitialized = true;

    updateGlancePanel(visibleIds);
    // Filters change which nodes are non-hidden, which changes the
    // in-viewport set used by hubs / all viewport-aware labelling.
    applyLabelMode();
    // If we just dropped focus mode (a filter edit implicitly exits focus),
    // re-render the open node panel so the Focus 1/2-hop button drops its
    // active + × state. Defer one tick to avoid re-entrancy from any
    // listeners that might fire during the panel rebuild.
    if (hadFocus && window.lastDetailKind === 'node' && window.lastDetailNodeId) {
      var nodeIdToRefresh = window.lastDetailNodeId;
      setTimeout(function() { showNodeDetail(nodeIdToRefresh); }, 0);
    }
  }

  // ===================== "At a glance" panel =====================
  // Summarizes the currently-visible filtered set: top entities by
  // degree, dominant rel-types, dominant super-categories. Updates from
  // applyFilters; in Category view (where applyFilters early-returns),
  // we just leave whatever was last shown — the glance panel is a
  // node-view tool. Click-through reuses existing drill helpers.
  function updateGlancePanel(visibleIds) {
    var el = document.getElementById('glance-panel');
    if (!el) return;
    if (viewMode !== 'node') {
      el.innerHTML = '<div class="glance-empty">Switch to Node view for details.</div>';
      return;
    }
    if (!visibleIds || visibleIds.size === 0) {
      el.innerHTML = '<div class="glance-empty">No matches in current filter.</div>';
      return;
    }

    // Walk visible nodes once to collect top-by-degree + per-category.
    var byCat = Object.create(null);
    var topNodes = [];  // keep top 5 by degree; lightweight running min
    visibleIds.forEach(function(id) {
      var n = nodeData.get(id);
      if (!n) return;
      var cat = n.superCategory || 'Other';
      byCat[cat] = (byCat[cat] || 0) + 1;
      var deg = n.degree || 0;
      if (topNodes.length < 5) {
        topNodes.push({id: n.id, label: n.id || n.label, degree: deg, cat: cat});
        topNodes.sort(function(a, b) { return b.degree - a.degree; });
      } else if (deg > topNodes[topNodes.length - 1].degree) {
        topNodes[topNodes.length - 1] = {id: n.id, label: n.id || n.label, degree: deg, cat: cat};
        topNodes.sort(function(a, b) { return b.degree - a.degree; });
      }
    });

    // Walk visible edges once for rel-type counts.
    var byRel = Object.create(null);
    edgeData.get().forEach(function(e) {
      if (e.hidden) return;
      var rc = e.relCategory || 'Other';
      byRel[rc] = (byRel[rc] || 0) + 1;
    });

    var sortByCount = function(o) {
      return Object.keys(o).map(function(k) { return [k, o[k]]; })
        .sort(function(a, b) { return b[1] - a[1]; });
    };
    var topCats = sortByCount(byCat).slice(0, 5);
    var topRels = sortByCount(byRel).slice(0, 5);

    var html = '';

    // When locked to a cluster, the visible set IS the cluster, so the
    // three sub-lists below act as the cluster's auto-summary. The banner
    // makes that connection explicit and offers a click-through to the
    // full cluster detail panel.
    if (communityFilter !== null) {
      var sizes = (GRAPH_META.communitySizes || []);
      var size = sizes[communityFilter] || 0;
      html += '<div class="glance-cluster-banner" onclick="showClusterDetail(' + communityFilter +
              ')" title="Open the full cluster summary">' +
              '<div class="glance-cluster-title">Showing: Cluster #' + (communityFilter + 1) + '</div>' +
              '<div class="glance-cluster-sub">' + size + ' members &middot; click for full summary</div>' +
              '</div>';
    }

    // Top entities (clickable: open node detail).
    if (topNodes.length) {
      html += '<div class="glance-h" title="Ranked by global degree among the currently visible set; entries shift as filters narrow">Top visible entities by degree</div>';
      topNodes.forEach(function(n) {
        var color = GRAPH_META.superCategoryColors[n.cat] || '#888';
        var nidJs = String(n.id).replace(/'/g, "\\'");
        html += '<div class="glance-row" onclick="showAndFocusNode(\'' + nidJs + '\')"' +
                ' title="Click to highlight on the canvas and open this entity\'s detail">' +
                '<span class="conn-dot" style="background:' + color + '"></span>' +
                '<span class="glance-label">' + esc(n.label) + '</span>' +
                '<span class="glance-count">' + n.degree + '</span>' +
                '</div>';
      });
    }

    // Top categories (clickable: drill to that super-cat).
    if (topCats.length) {
      html += '<div class="glance-h" title="Counts within the currently visible set">Visible by super-category</div>';
      topCats.forEach(function(kv) {
        var cat = kv[0], cnt = kv[1];
        var catJs = String(cat).replace(/'/g, "\\'");
        var color = GRAPH_META.superCategoryColors[cat] || '#888';
        html += '<div class="glance-row" onclick="drillToSuperCat(\'' + catJs + '\')"' +
                ' title="Click to filter the canvas to this category">' +
                '<span class="conn-dot" style="background:' + color + '"></span>' +
                '<span class="glance-label">' + esc(cat) + '</span>' +
                '<span class="glance-count">' + cnt + '</span>' +
                '</div>';
      });
    }

    // Top rel-types (clickable: filter to that rel).
    if (topRels.length) {
      html += '<div class="glance-h" title="Edge counts within the currently visible set">Visible by relationship</div>';
      topRels.forEach(function(kv) {
        var rel = kv[0], cnt = kv[1];
        var relJs = String(rel).replace(/'/g, "\\'");
        var color = GRAPH_META.relationshipColors[rel] || '#666';
        html += '<div class="glance-row" onclick="filterToRelType(\'' + relJs + '\')"' +
                ' title="Click to filter the canvas to this relationship type">' +
                '<span class="conn-dot" style="background:' + color + '"></span>' +
                '<span class="glance-label">' + esc(rel) + '</span>' +
                '<span class="glance-count">' + cnt + '</span>' +
                '</div>';
      });
    }

    el.innerHTML = html;
  }

  // ===================== Label LOD =====================
  // Three modes for STATIC labels:
  //  - hubs : super-nodes + global top-N hubs, plus viewport-aware
  //           overlay (when ≤ VIEWPORT_LABEL_THRESHOLD non-hidden nodes
  //           are inside the canvas, label all of them).
  //  - all  : super-nodes + every visible-in-viewport node, capped to
  //           top-ALL_LABELS_CAP by degree when more than that fit.
  //           Without the cap, low-zoom views render the full hairball
  //           of labels and are unreadable.
  //  - none : super-nodes only.
  //
  // Cursor-hover labelling is universal (works in all three modes):
  // pointing at any non-hidden node temporarily reveals its label if the
  // static logic above wasn't already showing it. See hoverNode /
  // blurNode handlers further down.
  //
  // Focus mode (focusOnNode + breadcrumb focus-mode-chip) is just a visibility
  // filter — the active label mode still rules. Both call sites schedule
  // applyLabelMode() to run AFTER the fit animation settles (450 ms,
  // matching the 400 ms fit animation + a 50 ms buffer) so the
  // viewport-aware logic sees the post-fit camera, not the wide pre-fit
  // one.
  // Read from labelSettings on every applyLabelMode pass so the UI sliders
  // (and URL-param overrides) take effect live without a re-render.
  var FIT_ANIM_LABEL_DELAY = 450;

  // Recompute every node's `isHub` flag from the current
  // labelSettings.hubLabels (top-N by degree). Cheap on the canonical
  // 16k-node corpus (~5 ms). Called once at init when URL-param override
  // differs from build-time, and on every Hub-label slider tick.
  function recomputeIsHub() {
    var n = labelSettings.hubLabels;
    var sorted = originalNodes.slice()
      .filter(function(x) { return !x.isSuperNode; })
      .sort(function(a, b) { return (b.degree || 0) - (a.degree || 0); });
    var hubSet = new Set();
    var k = Math.min(n, sorted.length);
    for (var i = 0; i < k; i++) hubSet.add(sorted[i].id);
    nodeData.update(originalNodes.map(function(node) {
      return {id: node.id, isHub: hubSet.has(node.id)};
    }));
  }

  function _inViewportNonHidden() {
    var view = network.getViewPosition();
    var scale = network.getScale();
    var canvas = document.getElementById('mynetwork');
    var halfW = canvas.clientWidth / (2 * scale);
    var halfH = canvas.clientHeight / (2 * scale);
    var minX = view.x - halfW, maxX = view.x + halfW;
    var minY = view.y - halfH, maxY = view.y + halfH;
    var positions = network.getPositions();
    var ids = new Set();
    nodeData.get().forEach(function(n) {
      if (n.hidden) return;
      var p = positions[n.id];
      if (!p) return;
      if (p.x >= minX && p.x <= maxX && p.y >= minY && p.y <= maxY) {
        ids.add(n.id);
      }
    });
    return ids;
  }

  function _topNByDegree(idSet, n) {
    var arr = [];
    idSet.forEach(function(id) {
      var node = nodeData.get(id);
      if (node) arr.push({id: id, deg: node.degree || 0});
    });
    arr.sort(function(a, b) { return b.deg - a.deg; });
    var top = new Set();
    var k = Math.min(n, arr.length);
    for (var i = 0; i < k; i++) top.add(arr[i].id);
    return top;
  }

  function applyLabelMode() {
    // Computed once and shared with the readout below — also lets us
    // surface "N nodes in viewport" in any mode (None mode skips the
    // labelSet but the count is still useful context).
    var inViewport = _inViewportNonHidden();
    var labelSet = null;  // explicit node-id set (in addition to super-nodes + isHub)

    if (labelMode === 'hubs') {
      if (inViewport.size > 0 && inViewport.size <= labelSettings.viewportThreshold) {
        labelSet = inViewport;
      }
    } else if (labelMode === 'all') {
      if (inViewport.size === 0) {
        labelSet = null;
      } else if (inViewport.size <= labelSettings.allCap) {
        labelSet = inViewport;
      } else {
        labelSet = _topNByDegree(inViewport, labelSettings.allCap);
      }
    }
    // none mode: labelSet stays null; static labels are super-nodes only.

    var labelledCount = 0;
    var updates = nodeData.get().map(function(n) {
      var keep;
      // When path highlighting is active, the canvas should read as
      // "only the paths" — non-path nodes are dimmed AND their labels
      // hidden, regardless of label mode / hub status / viewport. Without
      // this override, a zoom or dragEnd would re-show labels on the
      // dimmed nodes (since applyLabelMode runs on those events).
      if (pathsHighlighted && _pathNodeIds) {
        keep = _pathNodeIds.has(n.id);
      } else if (n.isSuperNode) keep = true;
      else if (labelMode === 'all') keep = labelSet !== null && labelSet.has(n.id);
      else if (labelMode === 'none') keep = false;
      else keep = !!n.isHub || (labelSet !== null && labelSet.has(n.id));
      if (keep) labelledCount++;
      return {id: n.id, font: {size: keep ? 11 : 0}};
    });
    nodeData.update(updates);
    var lc = document.getElementById('label-density-count');
    if (lc) {
      lc.textContent = labelledCount.toLocaleString() + ' label' + (labelledCount === 1 ? '' : 's') +
                       ' · ' + inViewport.size.toLocaleString() + ' node' + (inViewport.size === 1 ? '' : 's') +
                       ' in viewport';
    }
    // Layer the "In viewport" stat block on top of currentStats. Done
    // here (not in applyFilters) so zoom + pan recompute it without
    // running the filter pass. Node view only — Category and EntityType
    // viewports are super-nodes only, "In viewport" is uninteresting.
    if (viewMode === 'node') setStatsViewportCount(inViewport.size);
  }

  function labelModeShort() {
    return labelMode === 'all' ? 'All' : labelMode === 'none' ? 'None' : 'Hubs';
  }

  // Stat-block layout: small uppercase label on top, larger
  // tabular-nums value below — easier to scan than running prose.
  // currentStats holds the "primary" blocks (set by applyFilters /
  // focusOnNode / switchView); viewport count is layered on top by
  // applyLabelMode without disturbing currentStats. Annotations like
  // "+5 expanded" go through appendStatsNote and are wiped on the
  // next render.
  var currentStats = [];
  function renderStats(blocks) {
    var el = document.getElementById('stats');
    if (!el) return;
    el.innerHTML = blocks.map(function(b) {
      return '<div class="stat-block">' +
             '<div class="stat-label">' + b.label + '</div>' +
             '<div class="stat-value">' + b.value + '</div></div>';
    }).join('');
  }
  function setStats(blocks) {
    currentStats = blocks;
    renderStats(currentStats);
  }
  function setStatsViewportCount(count) {
    var blocks = currentStats.filter(function(b) { return b.label !== 'In viewport'; });
    blocks.push({label: 'In viewport', value: count.toLocaleString()});
    renderStats(blocks);
  }
  function appendStatsNote(text) {
    var el = document.getElementById('stats');
    if (!el) return;
    var note = document.createElement('div');
    note.className = 'stat-note';
    note.textContent = text;
    el.appendChild(note);
  }

  // Re-evaluate viewport-aware labels on zoom + pan. vis.js fires `zoom`
  // continuously during pinch / scroll-wheel; debounce so we update once
  // the gesture settles. `dragEnd` fires once on pan-completion. Even in
  // `none` mode we may have a focus override, so don't early-return.
  var labelDebounceTimer = null;
  function scheduleLabelUpdate() {
    if (labelDebounceTimer) clearTimeout(labelDebounceTimer);
    labelDebounceTimer = setTimeout(applyLabelMode, 150);
  }
  network.on('zoom', scheduleLabelUpdate);
  network.on('dragEnd', scheduleLabelUpdate);

  // Cursor-hover labelling is universal — works in all three label
  // modes. If the hovered node's label is currently hidden (font.size
  // is falsy), reveal it on hover and hide on blur. If the static logic
  // already shows the label, this is a no-op.
  var hoverLabelNodeId = null;
  network.on('hoverNode', function(params) {
    var n = nodeData.get(params.node);
    if (!n || n.hidden || n.isSuperNode) return;
    var fontSize = n.font && n.font.size;
    if (fontSize) return;  // already labelled by the static logic
    hoverLabelNodeId = params.node;
    nodeData.update({id: params.node, font: {size: 11}});
  });
  network.on('blurNode', function(params) {
    if (hoverLabelNodeId === null) return;
    var id = hoverLabelNodeId;
    hoverLabelNodeId = null;
    // Guard: vis.js's DataSet.update() INSERTS a new minimal node when
    // the id isn't found. If the user moves the mouse off a real node
    // and clicks the Category toggle in the same gesture, switchView
    // clears nodeData before the blur fires — without this guard, the
    // update spawns a phantom node with the leftover real-node id.
    if (!nodeData.get(id)) return;
    nodeData.update({id: id, font: {size: 0}});
  });

  // ===================== Filter bindings =====================
  document.querySelectorAll('.type-cb').forEach(function(cb) { cb.addEventListener('change', applyFilters); });
  document.querySelectorAll('.rel-cb').forEach(function(cb) { cb.addEventListener('change', applyFilters); });
  var degSlider = document.getElementById('degree-slider');
  var degVal = document.getElementById('degree-val');
  degSlider.addEventListener('input', function() { degVal.textContent = this.value; debounce(applyFilters, 150)(); });
  var wtSlider = document.getElementById('weight-slider');
  var wtVal = document.getElementById('weight-val');
  wtSlider.addEventListener('input', function() { wtVal.textContent = this.value; debounce(applyFilters, 150)(); });
  document.getElementById('search-input').addEventListener('input', debounce(applyFilters, 200));
  var sc = document.getElementById('show-search-context');
  if (sc) {
    sc.addEventListener('change', function() {
      searchShowContext = this.checked;
      applyFilters();
    });
  }

  // ===================== Label-LOD settings sliders =====================
  // Bind a labelSettings slider: extends max if URL-param override exceeds
  // the HTML default, mirrors current value into the slider + readout, and
  // wires the input handler to live-apply.
  function _bindLodSlider(sliderId, valId, key, applyFn) {
    var sl = document.getElementById(sliderId);
    var vl = document.getElementById(valId);
    if (!sl || !vl) return;
    var cur = labelSettings[key];
    if (cur > parseInt(sl.max, 10)) sl.max = String(cur);
    sl.value = cur;
    vl.textContent = cur;
    sl.addEventListener('input', function() {
      var v = parseInt(this.value, 10);
      labelSettings[key] = v;
      vl.textContent = v;
      applyFn();
    });
  }
  var _onHubChange = debounce(function() { recomputeIsHub(); applyLabelMode(); }, 100);
  var _onLodChange = debounce(applyLabelMode, 100);
  _bindLodSlider('hub-label-slider', 'hub-label-val', 'hubLabels', _onHubChange);
  _bindLodSlider('viewport-threshold-slider', 'viewport-threshold-val', 'viewportThreshold', _onLodChange);
  _bindLodSlider('all-cap-slider', 'all-cap-val', 'allCap', _onLodChange);

  var _lodResetBtn = document.getElementById('lod-reset');
  if (_lodResetBtn) {
    _lodResetBtn.addEventListener('click', function() {
      labelSettings = Object.assign({}, DEFAULT_LABEL_SETTINGS);
      ['hubLabels', 'viewportThreshold', 'allCap'].forEach(function(k) {
        var sliderId = ({hubLabels: 'hub-label-slider', viewportThreshold: 'viewport-threshold-slider', allCap: 'all-cap-slider'})[k];
        var valId = ({hubLabels: 'hub-label-val', viewportThreshold: 'viewport-threshold-val', allCap: 'all-cap-val'})[k];
        var sl = document.getElementById(sliderId), vl = document.getElementById(valId);
        if (sl) sl.value = labelSettings[k];
        if (vl) vl.textContent = labelSettings[k];
      });
      recomputeIsHub();
      applyLabelMode();
    });
  }

  // If URL-param overrode hubLabels, the build-time `isHub` flags are
  // stale — recompute once before first paint.
  if (labelSettings.hubLabels !== DEFAULT_LABEL_SETTINGS.hubLabels) recomputeIsHub();

  // Entity type All / None
  document.getElementById('select-all-types').addEventListener('click', function() {
    document.querySelectorAll('.type-cb').forEach(function(cb) { cb.checked = true; }); applyFilters();
  });
  document.getElementById('select-none-types').addEventListener('click', function() {
    document.querySelectorAll('.type-cb').forEach(function(cb) { cb.checked = false; }); applyFilters();
  });
  // Relationship type All / None
  document.getElementById('select-all-rels').addEventListener('click', function() {
    document.querySelectorAll('.rel-cb').forEach(function(cb) { cb.checked = true; }); applyFilters();
  });
  document.getElementById('select-none-rels').addEventListener('click', function() {
    document.querySelectorAll('.rel-cb').forEach(function(cb) { cb.checked = false; }); applyFilters();
  });

  // Gain-pill click on a filter row → re-enable that row's checkbox
  // and re-run filters. Stop propagation so the click doesn't toggle
  // the surrounding <label>'s checkbox a second time.
  document.querySelectorAll('.type-filter, .rel-filter').forEach(function (label) {
    var pill = label.querySelector('.gain-pill');
    if (!pill) return;
    pill.addEventListener('click', function (evt) {
      evt.preventDefault();
      evt.stopPropagation();
      var cb = label.querySelector('input[type="checkbox"]');
      if (!cb) return;
      cb.checked = true;
      applyFilters();
    });
  });
  // Drop-pill clicks on slider rows → reset the slider to its min and
  // re-run filters. Recovers the dropped items.
  function _bindDropPill(pillId, slider, valEl) {
    var pill = document.getElementById(pillId);
    if (!pill) return;
    pill.addEventListener('click', function (evt) {
      evt.preventDefault();
      evt.stopPropagation();
      slider.value = slider.min;
      valEl.textContent = slider.min;
      applyFilters();
    });
  }
  _bindDropPill('degree-drop-pill', degSlider, degVal);
  _bindDropPill('weight-drop-pill', wtSlider, wtVal);

  // Time-window dual-thumb slider. Two stacked <input type="range"> share
  // a track; we keep them from crossing (lo ≤ hi) and re-run applyFilters
  // on every drag tick (debounced). Section + sliders only present when
  // GRAPH_META.timeBounds is non-null AND spans ≥2 months — visualize.py
  // skips emitting the HTML otherwise.
  var timeMinSlider = document.getElementById('time-min-slider');
  var timeMaxSlider = document.getElementById('time-max-slider');
  if (timeMinSlider && timeMaxSlider) {
    var _twDebounced = debounce(applyFilters, 150);
    function _onTimeInput() {
      var lo = parseInt(timeMinSlider.value, 10);
      var hi = parseInt(timeMaxSlider.value, 10);
      // Don't let the thumbs cross — clamp the one that just moved.
      if (lo > hi) {
        if (this === timeMinSlider) timeMinSlider.value = String(hi);
        else timeMaxSlider.value = String(lo);
      }
      // Live-update the readout label + fill before applyFilters fires —
      // gives the slider drag a snappy visual response even on big graphs.
      var tw = _readTimeWindow();
      var twLabel = document.getElementById('time-window-label');
      if (tw && twLabel) {
        twLabel.textContent = _monthBinToLabel(tw.lo) + ' → ' + _monthBinToLabel(tw.hi);
      }
      _updateTimeFill(tw);
      _twDebounced();
    }
    timeMinSlider.addEventListener('input', _onTimeInput);
    timeMaxSlider.addEventListener('input', _onTimeInput);
    // When two range thumbs perfectly overlap (lo === hi), only the
    // topmost (later in DOM) captures clicks. Without intervention, the
    // user could drag the left thumb to meet the right and never grab
    // the left thumb back. Fix: raise whichever thumb the user just
    // touched so it stays grabbable. Last-touched = on top.
    function _raiseTimeThumb(evt) {
      this.style.zIndex = '3';
      var other = (this === timeMinSlider) ? timeMaxSlider : timeMinSlider;
      other.style.zIndex = '1';
    }
    timeMinSlider.addEventListener('pointerdown', _raiseTimeThumb);
    timeMaxSlider.addEventListener('pointerdown', _raiseTimeThumb);
    // Initialize the fill bar to match the default full-range state.
    _updateTimeFill(_readTimeWindow());
  }
  var timeDropPill = document.getElementById('time-drop-pill');
  if (timeDropPill && timeMinSlider && timeMaxSlider) {
    timeDropPill.addEventListener('click', function(evt) {
      evt.preventDefault();
      evt.stopPropagation();
      timeMinSlider.value = timeMinSlider.min;
      timeMaxSlider.value = timeMaxSlider.max;
      _updateTimeFill(_readTimeWindow());
      applyFilters();
    });
  }

  // Panel minimize/expand
  document.getElementById('cp-toggle').addEventListener('click', function() {
    var cp = document.getElementById('control-panel');
    cp.classList.toggle('minimized');
    this.innerHTML = cp.classList.contains('minimized') ? '&#x25BE;' : '&#x25B4;';
  });

  // Collapsible sections
  document.querySelectorAll('#control-panel h4[data-section]').forEach(function(h4) {
    h4.addEventListener('click', function() {
      var name = this.dataset.section;
      var body = document.querySelector('.section-body[data-for="' + name + '"]');
      var arrow = this.querySelector('.toggle-arrow');
      if (body) body.classList.toggle('collapsed');
      if (arrow) arrow.classList.toggle('collapsed');
    });
  });

  // ===================== Back / Forward =====================
  document.getElementById('nav-back').addEventListener('click', function() {
    if (navIndex <= 0) return;
    navGoTo(navIndex - 1);
  });
  document.getElementById('nav-forward').addEventListener('click', function() {
    if (navIndex >= navHistory.length - 1) return;
    navGoTo(navIndex + 1);
  });

  // ===================== Toolbar =====================
  // Auto-disable handler shared between manual stop + stabilization-done.
  function _disablePhysics() {
    physOn = false;
    network.setOptions({physics: {enabled: false}});
    var btn = document.getElementById('physics-toggle');
    btn.textContent = 'Enable Physics';
    btn.classList.remove('active');
  }

  document.getElementById('physics-toggle').addEventListener('click', function() {
    if (physOn) { _disablePhysics(); return; }
    physOn = true;
    // barnesHut tuned for "spread overlapping nodes once, then settle".
    // Compared to vis.js defaults: stronger repulsion (gravConst -5000 vs
    // -2000) + avoidOverlap so dense hub clusters actually unpack;
    // matching centralGravity 0.3 (vis.js default) so peripheral nodes
    // don't drift outward indefinitely. Auto-disables on
    // stabilizationIterationsDone so the layout freezes once equilibrium
    // is reached rather than running the simulation forever (which is
    // what made nodes "drift apart without any chance to get compact"
    // with the previous tuning).
    network.setOptions({physics: {
      enabled: true,
      solver: 'barnesHut',
      barnesHut: {
        gravitationalConstant: -5000,
        centralGravity: 0.3,
        springLength: 150,
        springConstant: 0.04,
        avoidOverlap: 0.5,
      },
      stabilization: {enabled: true, iterations: 250, fit: false},
    }});
    network.once('stabilizationIterationsDone', _disablePhysics);
    // Active state-aware label so the user can see physics is running,
    // not just "enabled" abstractly.
    this.textContent = 'Settling layout — click to stop';
    this.classList.add('active');
  });

  document.getElementById('reset-layout-btn').addEventListener('click', function() {
    // Restore the cached layout positions captured at init. Disables
    // physics first (otherwise the simulation immediately re-applies
    // forces and the restore is invisible) then writes x/y back via
    // nodeData.update.
    if (physOn) _disablePhysics();
    var updates = [];
    Object.keys(_originalPositions).forEach(function(id) {
      var p = _originalPositions[id];
      updates.push({id: id, x: p.x, y: p.y});
    });
    if (updates.length) {
      nodeData.update(updates);
      network.fit({animation: {duration: 400, easingFunction: 'easeInOutQuad'}});
    }
  });
  var labelModeSel = document.getElementById('label-mode-select');
  if (labelModeSel) {
    labelModeSel.value = labelMode;
    labelModeSel.addEventListener('change', function() {
      labelMode = this.value;
      applyLabelMode();
    });
  }
  document.getElementById('fit-btn').addEventListener('click', function() {
    network.fit({animation: {duration: 400, easingFunction: 'easeInOutQuad'}});
  });
  document.getElementById('reset-btn').addEventListener('click', function() {
    cancelPathfind();
    document.getElementById('search-input').value = '';
    degSlider.value = 2; degVal.textContent = '2';
    wtSlider.value = 1; wtVal.textContent = '1';
    if (timeMinSlider && timeMaxSlider) {
      timeMinSlider.value = timeMinSlider.min;
      timeMaxSlider.value = timeMaxSlider.max;
      _updateTimeFill(_readTimeWindow());
    }
    document.querySelectorAll('.type-cb').forEach(function(cb) { cb.checked = true; });
    document.querySelectorAll('.rel-cb').forEach(function(cb) { cb.checked = true; });
    communityFilter = null;
    // Reset returns to the Node View landing with all filters wide.
    if (viewMode !== 'node') switchView('node');
    else { applyFilters(); network.fit({animation: {duration: 400, easingFunction: 'easeInOutQuad'}}); }
  });

  // ===================== Focus mode =====================
  function focusOnNode(nodeId, hops) {
    hops = hops || 1;
    var visible = new Set([nodeId]);
    var frontier = [nodeId];
    for (var h = 0; h < hops; h++) {
      var next = [];
      frontier.forEach(function(nid) {
        (neighborIndex[nid] || []).forEach(function(nbr) {
          if (!visible.has(nbr)) { visible.add(nbr); next.push(nbr); }
        });
      });
      frontier = next;
    }
    focusState = {nodeId: nodeId, hops: hops, memberCount: visible.size};
    nodeData.update(allNodes.map(function(n) { return {id: n.id, hidden: !visible.has(n.id)}; }));
    edgeData.update(allEdges.map(function(e) {
      return {id: e.id, hidden: !(visible.has(e.from) && visible.has(e.to))};
    }));
    network.fit({nodes: Array.from(visible), animation: {duration: 400, easingFunction: 'easeInOutQuad'}});
    renderBreadcrumb();
    // Re-render the open panel so the Focus button picks up its .active +
    // × state. Cheap (~10-50 ms) and only fires on the user's explicit
    // focus click, not on every applyFilters pass.
    if (window.lastDetailKind === 'node' && window.lastDetailNodeId === nodeId) {
      showNodeDetail(nodeId);
    }
    setStats([
      {label: 'Focus', value: visible.size.toLocaleString() + ' nodes'},
      {label: 'Hops', value: String(hops)},
    ]);
    // Wait for the fit animation to settle before recomputing viewport-
    // aware labels. Running applyLabelMode now would see the pre-fit
    // camera and miss most of the focus set; the existing zoom-event
    // hook fires during the animation but the timing isn't always
    // reliable across browsers — this setTimeout makes it deterministic.
    setTimeout(applyLabelMode, FIT_ANIM_LABEL_DELAY);
  }
  // expose globally for onclick
  window.focusOnNode = focusOnNode;

  // Exit focus and restore filters. Called from the breadcrumb focus chip's ×
  // (the cluster-lock-chip-style affordance — single discoverable surface
  // instead of a separate toolbar button).
  function _exitFocus() {
    if (!focusState) return;
    focusState = null;
    applyFilters();
    network.fit({animation: {duration: 400}});
    setTimeout(applyLabelMode, FIT_ANIM_LABEL_DELAY);
    // Re-render the open panel so the Focus button drops its × and active state.
    if (window.lastDetailKind === 'node' && window.lastDetailNodeId) {
      showNodeDetail(window.lastDetailNodeId);
    }
  }
  window._exitFocus = _exitFocus;

  // ===================== Expand hidden neighbors =====================
  function expandNeighborhood(nodeId) {
    var neighbors = neighborIndex[nodeId] || [];
    var revealed = [];
    neighbors.forEach(function(nid) {
      var n = nodeData.get(nid);
      if (n && n.hidden) revealed.push(nid);
    });
    if (!revealed.length) return;
    nodeData.update(revealed.map(function(nid) { return {id: nid, hidden: false, opacity: 0.6}; }));
    allEdges.forEach(function(e) {
      if ((e.from === nodeId && revealed.indexOf(e.to) >= 0) ||
          (e.to === nodeId && revealed.indexOf(e.from) >= 0)) {
        edgeData.update([{id: e.id, hidden: false}]);
      }
    });
    appendStatsNote('+' + revealed.length + ' expanded');
  }
  window.expandNeighborhood = expandNeighborhood;

  // ===================== Pathfinding =====================
  function startPathfind(fromId) {
    pathfindState = {fromId: fromId};
    document.getElementById('pathfind-status').style.display = 'block';
    document.getElementById('mynetwork').style.cursor = 'crosshair';
    // Visually mark the source node so the user remembers which node
    // they're pathing FROM once they move the cursor away to pick a
    // destination. Restored on cancel / completion.
    nodeData.update({id: fromId, borderWidth: 5, borderWidthSelected: 5});
  }
  window.startPathfind = startPathfind;

  function cancelPathfind() {
    var prevFrom = pathfindState && pathfindState.fromId;
    pathfindState = null;
    document.getElementById('pathfind-status').style.display = 'none';
    document.getElementById('mynetwork').style.cursor = '';
    if (prevFrom) {
      nodeData.update({id: prevFrom, borderWidth: 1, borderWidthSelected: 2});
    }
  }
  window.cancelPathfind = cancelPathfind;

  // BFS shortest path with optional node + edge blocklists. Used as the
  // primitive by Yen's k-shortest-paths below — Yen excludes nodes /
  // edges from earlier candidates to find diverse alternatives.
  function bfsShortestPath(fromId, toId, blockedNodes, blockedEdges) {
    blockedNodes = blockedNodes || _EMPTY_SET;
    blockedEdges = blockedEdges || _EMPTY_SET;
    if (fromId === toId) return [fromId];
    var visited = Object.create(null);
    visited[fromId] = true;
    var queue = [[fromId]];
    while (queue.length > 0) {
      var path = queue.shift();
      var cur = path[path.length - 1];
      var neighbours = neighborIndex[cur] || [];
      for (var i = 0; i < neighbours.length; i++) {
        var nbr = neighbours[i];
        if (visited[nbr]) continue;
        if (blockedNodes.has(nbr)) continue;
        if (blockedEdges.has(cur + '|' + nbr)) continue;
        visited[nbr] = true;
        var newPath = path.concat([nbr]);
        if (nbr === toId) return newPath;
        queue.push(newPath);
      }
    }
    return null;
  }

  // Yen's k-shortest simple paths. For unweighted graphs (which we treat
  // ours as for path purposes), each iteration peels off the previous
  // best, then for every "spur" position along it tries a shortest path
  // around the edge taken at that position. Candidate paths are
  // deduplicated and sorted; the next-shortest unique one is admitted to
  // A. Stops early when no more spurs yield a path.
  function findKShortestPaths(fromId, toId, K) {
    K = K || 3;
    var first = bfsShortestPath(fromId, toId);
    if (!first) return [];
    var A = [first];
    var B = [];

    for (var k = 1; k < K; k++) {
      var prev = A[k - 1];
      for (var i = 0; i < prev.length - 1; i++) {
        var spurNode = prev[i];
        var rootPath = prev.slice(0, i + 1);

        // Block edges that any earlier path took from this same root
        // prefix — forces the spur to diverge from established choices.
        var blockedEdges = new Set();
        for (var aIdx = 0; aIdx < A.length; aIdx++) {
          var p = A[aIdx];
          if (p.length <= i + 1) continue;
          var sharesPrefix = true;
          for (var j = 0; j <= i; j++) {
            if (p[j] !== rootPath[j]) { sharesPrefix = false; break; }
          }
          if (sharesPrefix) {
            blockedEdges.add(p[i] + '|' + p[i + 1]);
            blockedEdges.add(p[i + 1] + '|' + p[i]);
          }
        }

        // Block root-path nodes (except the spur node itself) so we
        // don't loop back through the prefix.
        var blockedNodes = new Set(rootPath);
        blockedNodes.delete(spurNode);

        var spurPath = bfsShortestPath(spurNode, toId, blockedNodes, blockedEdges);
        if (!spurPath) continue;
        var totalPath = rootPath.slice(0, -1).concat(spurPath);
        var key = totalPath.join('|');
        var dup = false;
        for (var bIdx = 0; bIdx < B.length; bIdx++) {
          if (B[bIdx].key === key) { dup = true; break; }
        }
        if (!dup) B.push({path: totalPath, length: totalPath.length - 1, key: key});
      }
      if (B.length === 0) break;
      B.sort(function (a, b) { return a.length - b.length; });
      A.push(B.shift().path);
    }
    return A;
  }

  // Distinct colors per displayed path. Up to K=3 is the visible default;
  // wraps for higher K but K=3 is plenty for the user's "alternative
  // routes" intent.
  var _PATH_COLORS = ['#ffeb3b', '#00e5ff', '#ff7043'];
  var _EMPTY_SET = new Set();

  function highlightPaths(paths) {
    if (!paths || paths.length === 0) return;
    pathsHighlighted = true;
    var allPathNodes = new Set();
    var edgeColorByKey = Object.create(null);
    for (var k = 0; k < paths.length; k++) {
      var p = paths[k];
      var col = _PATH_COLORS[k % _PATH_COLORS.length];
      for (var i = 0; i < p.length; i++) allPathNodes.add(p[i]);
      for (var j = 0; j < p.length - 1; j++) {
        // Don't overwrite an earlier path's color on a shared edge —
        // first-path-wins keeps the primary route visually dominant.
        var keyAB = p[j] + '|' + p[j + 1];
        var keyBA = p[j + 1] + '|' + p[j];
        if (edgeColorByKey[keyAB] === undefined) edgeColorByKey[keyAB] = col;
        if (edgeColorByKey[keyBA] === undefined) edgeColorByKey[keyBA] = col;
      }
    }

    _pathNodeIds = allPathNodes;
    nodeData.update(allNodes.map(function (n) {
      return allPathNodes.has(n.id)
        ? {id: n.id, hidden: false, opacity: 1, borderWidth: 3, font: {size: 11}}
        : {id: n.id, hidden: false, opacity: 0.06, font: {size: 0}};
    }));
    edgeData.update(allEdges.map(function (e) {
      var c = edgeColorByKey[e.from + '|' + e.to];
      return c
        ? {id: e.id, hidden: false, color: {color: c, opacity: 1}, width: 4}
        : {id: e.id, hidden: false, color: {color: '#333333', opacity: 0.05}, width: 0.5};
    }));
    network.fit({nodes: Array.from(allPathNodes), animation: {duration: 500, easingFunction: 'easeInOutQuad'}});

    // Detail panel: one block per path, color-keyed to its canvas line.
    var html = '<h3>' + paths.length + ' shortest path' + (paths.length === 1 ? '' : 's') + '</h3>';
    paths.forEach(function (path, idx) {
      var col = _PATH_COLORS[idx % _PATH_COLORS.length];
      html += '<div class="path-block" style="border-left:3px solid ' + col +
              ';padding-left:8px;margin:8px 0">';
      html += '<div class="path-header" style="color:' + col +
              '">Path ' + (idx + 1) + ' &middot; ' + (path.length - 1) + ' hop' +
              (path.length === 2 ? '' : 's') + '</div>';
      path.forEach(function (nid, sidx) {
        var n = nodeData.get(nid);
        var cat = n ? (n.superCategory || 'Other') : 'Other';
        var ccolor = GRAPH_META.superCategoryColors[cat] || '#888';
        html += '<div class="path-step">';
        html += '<span class="entity-badge" style="background:' + ccolor + '33; color:' + ccolor + '">' + cat + '</span> ';
        html += '<span class="conn-item" data-node-id="' + esc(nid) + '">' + esc(n ? (n.id || n.label) : nid) + '</span>';
        html += '</div>';
        if (sidx < path.length - 1) html += '<div class="path-arrow">&darr;</div>';
      });
      html += '</div>';
    });
    // No "Clear paths" button — every dismissal route (X button, click
    // empty canvas, click another node/edge) now invokes
    // clearPathsIfActive() which routes through applyFilters' canonical
    // path-clear branch. Closing the panel = leaving path mode.
    document.getElementById('detail-content').innerHTML = html;
    document.getElementById('detail-panel').style.display = 'block';
  }
  window.applyFilters = applyFilters;

  // ===================== Category View =====================
  var categoryDataCache = null;
  var entityTypeViewCache = {};  // {categoryName: {nodes, edges}}

  // Cached per-category aggregates used by both the super-node hover
  // tooltips and the showCategoryDetail panel: top hubs (within-category
  // degree), top entity-types (histogram), and top concrete bridge
  // entities per other category. Computed lazily on first access; the
  // graph is read-only so a session-long cache is safe.
  var _categoryStatsCache = Object.create(null);
  function _categoryStats(cat) {
    if (_categoryStatsCache[cat]) return _categoryStatsCache[cat];
    var members = [];
    var memberSet = new Set();
    var byEntityType = Object.create(null);
    originalNodes.forEach(function(n) {
      if (n.isSuperNode) return;
      if (n.superCategory !== cat) return;
      members.push(n);
      memberSet.add(n.id);
      var et = n.entityType || 'Other';
      byEntityType[et] = (byEntityType[et] || 0) + 1;
    });
    var internalDeg = Object.create(null);
    members.forEach(function(m) { internalDeg[m.id] = 0; });
    var bridgesByOtherCat = Object.create(null);  // otherCat -> {otherId -> {count, label, otherCat, entityType}}
    originalEdges.forEach(function(e) {
      var fromIn = memberSet.has(e.from);
      var toIn = memberSet.has(e.to);
      if (fromIn && toIn) {
        internalDeg[e.from]++;
        internalDeg[e.to]++;
      } else if (fromIn || toIn) {
        var external = fromIn ? e.to : e.from;
        var en = nodeData.get(external) || _originalNodeById(external) || {};
        var oCat = en.superCategory || 'Other';
        if (!bridgesByOtherCat[oCat]) bridgesByOtherCat[oCat] = Object.create(null);
        var b = bridgesByOtherCat[oCat][external];
        if (!b) {
          b = bridgesByOtherCat[oCat][external] = {
            count: 0, label: en.id || en.label || external, otherCat: oCat,
            entityType: en.entityType || '', otherId: external,
          };
        }
        b.count++;
      }
    });
    var topHubs = members.map(function(m) {
      return {id: m.id, label: m.id || m.label, deg: internalDeg[m.id], entityType: m.entityType || ''};
    }).sort(function(a, b) { return b.deg - a.deg; }).slice(0, 10);
    var topEntityTypes = Object.keys(byEntityType).map(function(k) { return [k, byEntityType[k]]; })
      .sort(function(a, b) { return b[1] - a[1]; });
    // Sort bridges per other-cat by count desc.
    var bridgesSorted = Object.create(null);
    Object.keys(bridgesByOtherCat).forEach(function(oc) {
      bridgesSorted[oc] = Object.keys(bridgesByOtherCat[oc])
        .map(function(id) { return bridgesByOtherCat[oc][id]; })
        .sort(function(a, b) { return b.count - a.count; });
    });
    var stats = {
      memberCount: members.length,
      topHubs: topHubs,
      topEntityTypes: topEntityTypes,
      bridgesByOtherCat: bridgesSorted,
    };
    _categoryStatsCache[cat] = stats;
    return stats;
  }

  // Plain-text vis.js node tooltip; \n is honoured as a line break by
  // vis-network 9.1.2 in plain-string titles. Surfaces top hubs +
  // entity-type count so the user gets the gist on hover without
  // having to click + open the detail panel.
  function _categoryHoverText(cat) {
    var s = _categoryStats(cat);
    var lines = [cat + ' · ' + s.memberCount.toLocaleString() + ' members'];
    if (s.topHubs.length) {
      var hubLabels = s.topHubs.slice(0, 3).map(function(h) { return h.label; });
      lines.push('Top hubs: ' + hubLabels.join(', '));
    }
    if (s.topEntityTypes.length) {
      lines.push(s.topEntityTypes.length + ' entity type' + (s.topEntityTypes.length === 1 ? '' : 's'));
    }
    return lines.join('\n');
  }

  // originalNodes is an array; build a one-time index for O(1) access
  // from inside _categoryStats. nodeData.get works in Node view but
  // returns nothing in Category view (only super-nodes loaded), so we
  // need a path that always reads from originalNodes.
  var _origByIdMap = null;
  function _originalNodeById(id) {
    if (!_origByIdMap) {
      _origByIdMap = Object.create(null);
      originalNodes.forEach(function(n) { _origByIdMap[n.id] = n; });
    }
    return _origByIdMap[id];
  }

  // Drill to a specific node from any view: switches to Node view if
  // needed, then selects + focuses + opens the detail panel. Reused by
  // the Category-view hub list and the Category-edge panel's per-side
  // contributor rows (where nodeData.get(id) doesn't return real nodes
  // because Category view only loads super-nodes). switchView('node')
  // defers its heavy block via runAfterPaint, so the post-switch work
  // is queued onto the same paint pipeline — runs after the DataSet
  // finishes hydrating.
  function drillToNode(nodeId) {
    var selectAndShow = function() {
      network.selectNodes([nodeId]);
      network.focus(nodeId, {scale: 1.2, animation: {duration: 400, easingFunction: 'easeInOutQuad'}});
      showNodeDetail(nodeId);
    };
    if (viewMode !== 'node') {
      switchView('node');
      runAfterPaint(selectAndShow);
    } else {
      selectAndShow();
    }
  }
  window.drillToNode = drillToNode;

  function buildCategoryView() {
    if (categoryDataCache) return categoryDataCache;
    var colors = GRAPH_META.superCategoryColors;
    var counts = GRAPH_META.typeCounts;
    var catEdges = GRAPH_META.categoryEdges;

    var nodes = [];
    Object.keys(counts).forEach(function(cat) {
      nodes.push({
        id: '__cat__' + cat, label: cat + '\n(' + counts[cat] + ')',
        color: colors[cat] || '#888', superCategory: cat,
        size: 25 + Math.sqrt(counts[cat]) * 2.5,
        font: {size: 16, color: '#eee', strokeWidth: 3, strokeColor: '#1a1a2e'},
        isSuperNode: true, nodeCount: counts[cat], degree: 0,
        title: _categoryHoverText(cat),
      });
    });

    var edges = [];
    Object.keys(catEdges).forEach(function(key) {
      var parts = key.split('|');
      var a = parts[0], b = parts[1];
      var d = catEdges[key];
      var isSelf = a === b;
      var pairLabel = isSelf ? (a + ' (internal)') : (a + ' ↔ ' + b);
      edges.push({
        from: '__cat__' + a, to: '__cat__' + b,
        width: 1.5 + Math.log1p(d.count) * 2.5,
        label: String(d.count),
        title: pairLabel + '\n' + d.count.toLocaleString() + ' edges\ntotal weight ' + d.weight.toFixed(0),
        font: {size: 12, color: '#ccc', strokeWidth: 2, strokeColor: '#1a1a2e', align: 'top'},
        color: {color: isSelf ? '#ffffff' : '#aaaaaa', opacity: isSelf ? 0.25 : 0.5},
        edgeWeight: d.weight,
        smooth: isSelf ? {type: 'curvedCW', roundness: 0.4} : false,
      });
    });

    categoryDataCache = {nodes: nodes, edges: edges};
    return categoryDataCache;
  }

  // Mid-level "Entity Type" view: super-nodes for each entity_type within
  // a single super-category. Sits between Category View (7-ish supers) and
  // Node View (~16k). Inter-type edges are aggregated within the category
  // from GRAPH_META.entityTypeEdges — cross-category edges are out of
  // scope here since the user has already committed to one category.
  function buildEntityTypeView(category) {
    if (entityTypeViewCache[category]) return entityTypeViewCache[category];
    var color = GRAPH_META.superCategoryColors[category] || '#888';
    var counts = GRAPH_META.entityTypeCounts || {};
    var etEdges = GRAPH_META.entityTypeEdges || {};
    var prefix = category + '|';

    var nodes = [];
    Object.keys(counts).forEach(function(k) {
      if (k.indexOf(prefix) !== 0) return;
      var etype = k.substring(prefix.length);
      var cnt = counts[k];
      nodes.push({
        id: '__type__' + k,
        label: etype + '\n(' + cnt + ')',
        color: color, superCategory: category, entityType: etype,
        size: 20 + Math.sqrt(cnt) * 2.5,
        font: {size: 14, color: '#eee', strokeWidth: 3, strokeColor: '#1a1a2e'},
        isSuperNode: true, isEntityTypeNode: true, nodeCount: cnt, degree: 0,
      });
    });

    var edges = [];
    Object.keys(etEdges).forEach(function(key) {
      var parts = key.split('||');  // "Cat|Type" || "Cat|Type"
      if (parts.length !== 2) return;
      if (parts[0].indexOf(prefix) !== 0 || parts[1].indexOf(prefix) !== 0) return;
      var d = etEdges[key];
      var isSelf = parts[0] === parts[1];
      edges.push({
        from: '__type__' + parts[0], to: '__type__' + parts[1],
        width: 1 + Math.log1p(d.count) * 2,
        label: String(d.count),
        title: d.count + ' edges (weight ' + d.weight.toFixed(0) + ')',
        font: {size: 11, color: '#ccc', strokeWidth: 2, strokeColor: '#1a1a2e', align: 'top'},
        color: {color: isSelf ? '#ffffff' : '#aaaaaa', opacity: isSelf ? 0.25 : 0.45},
        edgeWeight: d.weight,
        smooth: isSelf ? {type: 'curvedCW', roundness: 0.4} : false,
      });
    });

    entityTypeViewCache[category] = {nodes: nodes, edges: edges};
    return entityTypeViewCache[category];
  }

  function switchView(mode, opts) {
    opts = opts || {};
    // Self-transition for entityType view is meaningful only when the
    // category changes; for category/node it's a no-op like before.
    if (mode === viewMode && !(mode === 'entityType' && opts.category && opts.category !== currentCategory)) return;
    viewMode = mode;
    cancelPathfind();
    focusState = null;
    // Drop any pending hover-blur target so a delayed blurNode after
    // we clear the DataSet can't spawn a phantom node.
    hoverLabelNodeId = null;
    network.unselectAll();
    closeDetailPanel();

    if (mode === 'category') {
      currentCategory = null;
      var cv = buildCategoryView();
      nodeData.clear(); edgeData.clear();
      nodeData.add(cv.nodes); edgeData.add(cv.edges);
      allNodes = nodeData.get(); allEdges = edgeData.get();
      rebuildIndexes();

      network.setOptions({physics: {enabled: true, solver: 'forceAtlas2Based',
        forceAtlas2Based: {gravitationalConstant: -200, centralGravity: 0.01, springLength: 200}}});
      setTimeout(function() {
        network.setOptions({physics: {enabled: false}});
        network.fit({animation: {duration: 400}});
      }, 2500);

      document.getElementById('control-panel').classList.add('category-mode');
      setStats([
        {label: 'Categories', value: cv.nodes.length.toLocaleString()},
        {label: 'Aggregate edges', value: cv.edges.length.toLocaleString()},
      ]);
    } else if (mode === 'entityType') {
      currentCategory = opts.category;
      var ev = buildEntityTypeView(currentCategory);
      nodeData.clear(); edgeData.clear();
      nodeData.add(ev.nodes); edgeData.add(ev.edges);
      allNodes = nodeData.get(); allEdges = edgeData.get();
      rebuildIndexes();

      network.setOptions({physics: {enabled: true, solver: 'forceAtlas2Based',
        forceAtlas2Based: {gravitationalConstant: -150, centralGravity: 0.02, springLength: 160}}});
      setTimeout(function() {
        network.setOptions({physics: {enabled: false}});
        network.fit({animation: {duration: 400}});
      }, 2000);

      document.getElementById('control-panel').classList.add('category-mode');
      setStats([
        {label: currentCategory + ' types', value: ev.nodes.length.toLocaleString()},
        {label: 'Aggregate edges', value: ev.edges.length.toLocaleString()},
      ]);
    } else {
      // Node view re-render is the slow path (16k+ nodeData.add blocks
      // the main thread for 1-3 s). Show the loading overlay first,
      // wait two rAF for it to paint, then run the synchronous heavy
      // block. Without the deferral the user sees a frozen window for
      // the duration; with it, they see the spinner.
      showLoading('Loading Node view (' + originalNodes.length.toLocaleString() + ' nodes)…');
      runAfterPaint(function() {
        currentCategory = null;
        nodeData.clear(); edgeData.clear();
        nodeData.add(originalNodes); edgeData.add(originalEdges);
        allNodes = nodeData.get(); allEdges = edgeData.get();
        rebuildIndexes();

        document.getElementById('control-panel').classList.remove('category-mode');
        applyFilters();
        network.fit({animation: {duration: 400}});
        applyLabelMode();
        renderBreadcrumb();
        syncViewToggleGroup();
        hideLoading();
      });
      return;  // applyLabelMode + breadcrumb + toggle sync run inside the rAF block
    }
    applyLabelMode();
    renderBreadcrumb();
    syncViewToggleGroup();
  }

  // Segmented Category/Node toggle lives in the control-panel header.
  // Entity Type view shares the 'category' segment (it's a sub-level of
  // the category overview); explicit jump to Node from any mode is a
  // click on the Node half.
  document.querySelectorAll('#view-toggle-group button[data-mode]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var target = this.dataset.mode;
      if (target === 'category' && viewMode !== 'category') switchView('category');
      else if (target === 'node' && viewMode !== 'node') switchView('node');
    });
  });

  function syncViewToggleGroup() {
    // 'entityType' is shown as Category-active — the user is still within
    // the category-overview drill, just one level deeper.
    var activeHalf = (viewMode === 'node') ? 'node' : 'category';
    document.querySelectorAll('#view-toggle-group button[data-mode]').forEach(function(btn) {
      if (btn.dataset.mode === activeHalf) btn.classList.add('active');
      else btn.classList.remove('active');
    });
  }

  // ===================== Breadcrumb =====================
  // Chevron-separated path rendered above the toolbar. The visible segments
  // track viewMode: one in 'category', two in 'entityType', three (with the
  // active category checkbox as the leaf) in 'node'. Clicking a segment
  // jumps back up the hierarchy; the current segment has `.current` and is
  // inert.
  function renderBreadcrumb() {
    var el = document.getElementById('breadcrumb');
    if (!el) return;
    var parts = [];
    if (viewMode === 'category') {
      parts.push({label: 'All', cls: 'current', seg: 'all'});
    } else if (viewMode === 'entityType') {
      parts.push({label: 'All', cls: '', seg: 'all'});
      parts.push({label: currentCategory || '?', cls: 'current', seg: 'cat'});
    } else {
      // node view: infer leaf from current filter state. Single-cat shows
      // "All › Rule" with Rule as the current (inert) leaf; multi/no cat
      // shows the generic "All › Nodes" leaf instead. The cat segment is
      // intentionally not its own clickable level — the only up-navigation
      // is "All", which returns to Category view.
      parts.push({label: 'All', cls: '', seg: 'all'});
      var activeCats = [];
      document.querySelectorAll('.type-cb:checked').forEach(function(cb) { activeCats.push(cb.dataset.cat); });
      if (activeCats.length === 1) {
        parts.push({label: activeCats[0], cls: 'current', seg: 'cat', cat: activeCats[0]});
      } else {
        parts.push({label: 'Nodes', cls: 'current', seg: 'leaf'});
      }
    }
    var segHtml = parts.map(function(p, i) {
      var sep = i > 0 ? '<span class="breadcrumb-sep"> &rsaquo; </span>' : '';
      var dataCat = p.cat ? (' data-cat="' + esc(p.cat) + '"') : '';
      return sep + '<span class="breadcrumb-seg ' + p.cls + '" data-seg="' + p.seg + '"' + dataCat + '>' + esc(p.label) + '</span>';
    }).join('');
    var lockHtml = '';
    if (communityFilter !== null) {
      var sizes = (window.GRAPH_META && GRAPH_META.communitySizes) || [];
      var size = sizes[communityFilter] || 0;
      lockHtml = ' <span class="cluster-lock-chip">' +
                 '<span class="cluster-lock-chip-label" data-action="open-cluster"' +
                 ' title="Open the cluster summary">Cluster #' + (communityFilter + 1) +
                 ' (' + size + ')</span>' +
                 '<span class="cluster-lock-chip-close" data-action="unlock"' +
                 ' title="Unlock the canvas">&times;</span>' +
                 '</span>';
    }
    var focusHtml = '';
    if (focusState) {
      // Focus mode chip — single discoverable surface for both reopening
      // the focused node's panel (label) and exiting focus (×). Replaces
      // the old toolbar Exit Focus button which users routinely overlooked.
      var seedNode = nodeData.get(focusState.nodeId);
      var seedLabel = (seedNode && seedNode.id) || focusState.nodeId;
      var member = focusState.memberCount || 0;
      focusHtml = ' <span class="focus-mode-chip">' +
                  '<span class="focus-mode-chip-label" data-action="open-focus"' +
                  ' title="Reopen detail panel for the focused node">Focus ' +
                  focusState.hops + '-hop · ' + esc(seedLabel) +
                  ' (' + member + ')</span>' +
                  '<span class="focus-mode-chip-close" data-action="exit-focus"' +
                  ' title="Exit focus and restore filters">&times;</span>' +
                  '</span>';
    }
    el.innerHTML = segHtml + lockHtml + focusHtml;
  }
  document.getElementById('breadcrumb').addEventListener('click', function(evt) {
    var action = evt.target.closest('[data-action]');
    if (action) {
      var which = action.dataset.action;
      if (which === 'unlock') { filterToCommunity(null); return; }
      if (which === 'open-cluster') {
        if (communityFilter !== null) showClusterDetail(communityFilter);
        return;
      }
      if (which === 'exit-focus') { _exitFocus(); return; }
      if (which === 'open-focus') {
        if (focusState) showNodeDetail(focusState.nodeId);
        return;
      }
    }
    var seg = evt.target.closest('.breadcrumb-seg');
    if (!seg || seg.classList.contains('current')) return;
    var which = seg.dataset.seg;
    if (which === 'all') switchView('category');
    else if (which === 'cat') {
      var cat = seg.dataset.cat || currentCategory;
      if (cat) switchView('entityType', {category: cat});
    }
  });

  // ===================== Detail panel: show node =====================
  function showNodeDetail(nodeId) {
    var node = nodeData.get(nodeId);
    if (!node) return;
    clearEdgeSelection();
    clearPathsIfActive();
    applyNodeSelection([nodeId]);
    window.lastDetailNodeId = nodeId;
    window.lastDetailKind = 'node';
    var rb = document.getElementById('reopen-detail-btn'); if (rb) rb.disabled = false;
    navPush({kind: 'node', payload: nodeId});
    var catColor = GRAPH_META.superCategoryColors[node.superCategory || 'Other'] || '#888';

    var sCat = esc(node.superCategory || 'Other');
    var html = '<div class="entity-badge" style="background:' + catColor + '33; color:' + catColor + '" onclick="drillToSuperCat(\'' + sCat + '\')" title="Filter to ' + sCat + '">' +
               sCat + '</div>';
    var titleNidJs = esc(nodeId).replace(/'/g, "\\'");
    html += '<h3 class="detail-title" onclick="recenterOnNode(\'' + titleNidJs +
            '\')" title="Click to recenter the canvas on this node">' + esc(nodeId || node.label) + '</h3>';

    // Count hidden vs visible neighbors up front — surfaces in the
    // meta line as "X of Y connections visible" so the user knows
    // whether they're seeing the full picture.
    var connected = network.getConnectedNodes(nodeId);
    var hiddenCount = 0;
    connected.forEach(function(cid) { var cn = nodeData.get(cid); if (cn && cn.hidden) hiddenCount++; });
    var visibleConn = connected.length - hiddenCount;

    html += '<div class="detail-meta">';
    if (node.entityType) {
      var eType = esc(node.entityType);
      html += '<span class="drillable" onclick="drillToEntityType(\'' + eType + '\')" title="Filter to ' + eType + '">' + eType + '</span> &bull; ';
    }
    var deg = node.degree || 0;
    html += 'Degree: ' + deg;
    var rank = degreeRank[nodeId];
    if (rank && totalNodeCount > 0) {
      var pct = (rank / totalNodeCount) * 100;
      var rankSuffix = '';
      if (pct <= 1) rankSuffix = ' · top 1%';
      else if (pct <= 5) rankSuffix = ' · top 5%';
      else if (pct <= 10) rankSuffix = ' · top 10%';
      html += ' <span class="rank-badge">#' + rank + ' of ' + totalNodeCount.toLocaleString() + rankSuffix + '</span>';
    }
    if (connected.length > 0 && hiddenCount > 0) {
      html += ' &bull; <span class="conn-stat">' + visibleConn + ' of ' + connected.length + ' connections visible</span>';
    }
    if (node.isArticulation) {
      html += ' <span class="articulation-badge" title="Articulation point: removing this node would disconnect part of the graph">Cut node</span>';
    }
    if (typeof node.community === 'number' && node.community >= 0) {
      var sizes = (GRAPH_META.communitySizes || []);
      var commSize = sizes[node.community] || 0;
      var isLocked = (communityFilter === node.community);
      var badgeTitle = isLocked
        ? 'Currently locked to this cluster — click to unlock'
        : 'Lock canvas to this Louvain community (' + commSize + ' nodes)';
      html += ' <span class="community-badge' + (isLocked ? ' active' : '') +
              '" onclick="filterToCommunity(' + node.community +
              ')" title="' + badgeTitle + '">Cluster #' +
              (node.community + 1) + ' (' + commSize + ' nodes)' +
              (isLocked ? ' &times;' : '') + '</span>';
    }
    var _connEdgeIds = network.getConnectedEdges(nodeId);
    var _timeChip = _buildTimeRangeChip(node, _connEdgeIds);
    if (_timeChip) html += ' ' + _timeChip;
    html += '</div>';

    // Actions (above description)
    var nid = esc(nodeId).replace(/'/g, "\\'");
    var labelJs = esc(nodeId || node.label).replace(/'/g, "\\'");
    html += '<div class="detail-actions">';
    // Focus 1-hop / 2-hop: when active for THIS node, the button shows .active
    // styling + a trailing × so click toggles back to "exit focus" — mirrors
    // the Cluster #N badge's lock/unlock affordance, single discoverable
    // surface instead of a separate Exit-Focus button somewhere else.
    var f1Active = focusState && focusState.nodeId === nodeId && focusState.hops === 1;
    var f2Active = focusState && focusState.nodeId === nodeId && focusState.hops === 2;
    html += '<button class="action-btn' + (f1Active ? ' active' : '') + '" onclick="' +
            (f1Active ? '_exitFocus()' : ("focusOnNode('" + nid + "', 1)")) +
            '" title="' + (f1Active ? 'Exit focus and restore filters' : 'Hide everything except this node and its direct neighbours') +
            '">Focus 1-hop' + (f1Active ? ' &times;' : '') + '</button>';
    html += '<button class="action-btn' + (f2Active ? ' active' : '') + '" onclick="' +
            (f2Active ? '_exitFocus()' : ("focusOnNode('" + nid + "', 2)")) +
            '" title="' + (f2Active ? 'Exit focus and restore filters' : 'Hide everything except this node and its 1- and 2-hop neighbours') +
            '">Focus 2-hop' + (f2Active ? ' &times;' : '') + '</button>';
    html += '<button class="action-btn" onclick="recenterOnNode(\'' + nid + '\')" title="Pan + zoom the camera to center this node in view (does not change visibility)">Recenter</button>';
    html += '<button class="action-btn" onclick="startPathfind(\'' + nid + '\')" title="Find path to: click another node on the canvas to highlight up to 3 shortest paths between this node and the target">Path to...</button>';
    html += '<button class="action-btn" onclick="openTable(\'' + labelJs + '\')" title="Open the entity table pre-filtered to this entity\'s name">Table</button>';
    html += '<button class="action-btn" onclick="openConnTable(\'' + nid + '\')" title="Open a sortable, searchable table of every node connected to this one">Connections</button>';
    html += _renderStatsQuerySplit({
      stats: _buildStatsCommandForNode(node),
      query: _buildQueryCommandForNode(node),
    }, 'sq-node-' + nid);
    html += renderCopySplit('node', nodeId, 'copy-name-btn');

    if (hiddenCount > 0) {
      html += '<button class="action-btn" onclick="expandNeighborhood(\'' + nid + '\')" title="Reveal the ' + hiddenCount + ' neighbours currently hidden by filters">Show ' + hiddenCount + ' hidden</button>';
    }
    html += '</div>';

    if (node.fullDescription) {
      var descPhrases = String(node.fullDescription)
        .split('\x1f')
        .map(function(s) { return s.trim(); })
        .filter(function(s) { return s; });
      if (descPhrases.length) {
        html += '<div class="detail-desc">';
        if (descPhrases.length > 1) {
          var topicCount = (node.topicIds && node.topicIds.length) || 0;
          var annotation = descPhrases.length + ' phrases';
          if (topicCount > 0) annotation += ' · ' + topicCount + ' topic' + (topicCount === 1 ? '' : 's');
          // Phrase order does not align with topicIds order — LightRAG sorts
          // descriptions by (timestamp, -length) independently of source_id.
          // The annotation reports the multiplicity, not a per-phrase mapping.
          html += '<div class="desc-meta" title="LightRAG concatenates one description per chunk extraction; phrase order does not map to source-topic order">' + esc(annotation) + '</div>';
        }
        descPhrases.forEach(function(p) {
          // A phrase may itself contain LLM-summarized paragraph breaks
          // (\n\n). Render each as its own <p> inside the .desc-phrase
          // wrapper so within-phrase spacing is correct and the dashed
          // separator only fires at actual phrase boundaries.
          var paras = String(p).split(/\n\n+/).map(function(s) { return s.trim(); }).filter(function(s) { return s; });
          if (!paras.length) return;
          html += '<div class="desc-phrase">';
          paras.forEach(function(para) {
            html += '<p>' + esc(para).replace(/\n/g, '<br>') + '</p>';
          });
          html += '</div>';
        });
        html += '</div>';
      }
    }

    // Personalized PageRank "Related" — surfaces structurally close
    // nodes (1-, 2-, 3-hop) weighted by random-walk-with-restart
    // mass. Direct neighbours are tagged so the user can tell when a
    // result is just a 1-hop already in the connections list and when
    // it's a deeper structural relation.
    var pprResults = personalizedPageRank(nodeId, {topK: 10});
    if (pprResults.length > 0) {
      var directSet = new Set(neighborIndex[nodeId] || []);
      html += '<h4 class="related-heading" title="Personalized PageRank — random walk with restart from this node, top 10 by stationary score">Related</h4>';
      html += '<div class="connections related-section">';
      pprResults.forEach(function (r) {
        var rn = nodeData.get(r.id);
        if (!rn) return;
        var rcat = rn.superCategory || 'Other';
        var rcatColor = GRAPH_META.superCategoryColors[rcat] || '#888';
        var directBadge = directSet.has(r.id) ? ' <span class="related-1hop">1-hop</span>' : '';
        html += '<div class="conn-item related-row" data-node-id="' + esc(r.id) + '">';
        html += '<span class="conn-dot" style="background:' + rcatColor + '"></span>';
        html += esc(r.id || rn.label);
        html += directBadge;
        html += '</div>';
      });
      html += '</div>';
    }

    // Group connections by super-category. (The per-rel-type texture
    // is already visible via the inline rel-chip on each row below — we
    // tried a histogram pill row up here and it was redundant.)
    var connEdges = network.getConnectedEdges(nodeId);
    var topicIdsAlreadyShown = new Set();
    if (connected.length > 0) {
      var topicIndex = (window.GRAPH_META && GRAPH_META.topicIndex) || {};
      var groups = {};
      var groupExpandableCount = {};
      connected.forEach(function(cid) {
        var cn = nodeData.get(cid);
        var cat = cn ? (cn.superCategory || 'Other') : 'Other';
        if (!groups[cat]) groups[cat] = [];
        var edgeDesc = '', edgeRelCat = '';
        for (var i = 0; i < connEdges.length; i++) {
          var e = edgeData.get(connEdges[i]);
          if (e && ((e.from === nodeId && e.to === cid) || (e.from === cid && e.to === nodeId))) {
            edgeDesc = _firstPhrase(e.fullDescription);
            edgeRelCat = e.relCategory || '';
            break;
          }
        }
        var topicId = '';
        if (cn && cn.entityType === 'topic' && cn.topicIds && cn.topicIds.length) {
          topicId = String(cn.topicIds[0]);
          topicIdsAlreadyShown.add(topicId);
          if (topicIndex[topicId]) {
            groupExpandableCount[cat] = (groupExpandableCount[cat] || 0) + 1;
          }
        }
        groups[cat].push({id: cid, label: cn ? (cid || cn.label) : cid, desc: edgeDesc, relCat: edgeRelCat, hidden: cn && cn.hidden, topicId: topicId});
      });

      html += '<div class="connections">';
      var sortedCats = Object.keys(groups).sort(function(a,b) { return groups[b].length - groups[a].length; });
      sortedCats.forEach(function(cat) {
        var gc = GRAPH_META.superCategoryColors[cat] || '#888';
        var expandableInGroup = groupExpandableCount[cat] || 0;
        html += '<div class="conn-group">';
        html += '<div class="conn-group-header">';
        html += '<h5 style="color:' + gc + ';cursor:pointer" onclick="drillToSuperCat(\'' + esc(cat) + '\')" title="Filter to ' + esc(cat) + '">' + esc(cat) + ' (' + groups[cat].length + ')</h5>';
        if (expandableInGroup >= 2) {
          html += '<button class="conn-expand-all" onclick="event.stopPropagation();_toggleAllConnExpand(this)" title="Show topic details for every Topic row in this group">Expand all</button>';
        }
        html += '</div>';
        groups[cat].forEach(function(item) {
          var opacity = item.hidden ? 'opacity:0.4;' : '';
          var topicInfo = item.topicId ? topicIndex[item.topicId] : null;
          html += '<div class="conn-item" data-node-id="' + esc(item.id) + '" style="' + opacity + '">';
          html += '<span class="conn-dot" style="background:' + gc + '"></span>' + esc(item.label);
          if (item.relCat) {
            var rc = GRAPH_META.relationshipColors[item.relCat] || '#666';
            var relJs = String(item.relCat).replace(/'/g, "\\'");
            // Inline rel-chip is clickable: scope the canvas to this rel-bucket.
            // stopPropagation so the click doesn't also trigger the row's
            // .conn-item navigation handler on #detail-content.
            html += ' <span class="rel-chip" style="color:' + rc + ';border-color:' + rc +
                    '44" onclick="event.stopPropagation();filterToRelType(\'' + relJs +
                    '\')" title="Filter to ' + esc(item.relCat) + '">' + esc(item.relCat) + '</span>';
          }
          // Topic neighbors: drop the (usually-boilerplate) edge desc; the
          // expand button surfaces the topic's own metadata + excerpt instead.
          if (item.desc && !topicInfo) html += ' <span class="conn-edge-desc">&mdash; ' + esc(item.desc) + '</span>';
          if (topicInfo) {
            html += ' <button class="conn-expand" onclick="event.stopPropagation();_toggleConnExpand(this)" title="Show topic details" aria-expanded="false">&#9656;</button>';
          }
          html += '</div>';
          if (topicInfo) {
            var meta = [];
            if (topicInfo.createdAt) meta.push(String(topicInfo.createdAt).slice(0, 10));
            if (topicInfo.postCount) meta.push(topicInfo.postCount + ' post' + (topicInfo.postCount === 1 ? '' : 's'));
            if (topicInfo.firstPostBy) meta.push('started by ' + topicInfo.firstPostBy);
            var tidJs = String(item.topicId).replace(/'/g, "\\'");
            html += '<div class="conn-topic-detail" style="display:none">';
            if (meta.length) html += '<div class="topic-row-meta">' + esc(meta.join(' · ')) + '</div>';
            if (topicInfo.excerpt) html += '<div class="topic-row-excerpt">' + esc(topicInfo.excerpt) + '</div>';
            html += '<button class="conn-topic-open" onclick="event.stopPropagation();openTopicModal(\'' + tidJs + '\')" title="Open topic preview modal">Open topic preview</button>';
            html += '</div>';
          }
        });
        html += '</div>';
      });
      html += '</div>';
    }

    var rankedTopicIds = _unionAndRankTopicIds(node.topicIds, connEdges);
    var residualTopicIds = rankedTopicIds.filter(function(tid) { return !topicIdsAlreadyShown.has(String(tid)); });
    if (residualTopicIds.length > 0) {
      var hasOverlap = topicIdsAlreadyShown.size > 0;
      // Open by default when residual is the only provenance signal (no Topic neighbors above).
      // Collapse when most provenance is already shown above as Topic neighbors — keeps the panel quiet.
      html += '<details class="source-topics-residual"' + (hasOverlap ? '' : ' open') + '>';
      var headerLabel = hasOverlap ? 'Source topics — not shown above' : 'Source topics';
      html += '<summary>' + headerLabel + ' (' + residualTopicIds.length + ')</summary>';
      html += _renderSourceTopics(residualTopicIds, {omitHeader: true});
      html += '</details>';
    }

    document.getElementById('detail-content').innerHTML = html;
    document.getElementById('detail-panel').style.display = 'block';
  }
  window.showNodeDetail = showNodeDetail;

  // First non-empty \x1f-separated phrase from an edge's fullDescription.
  // Use this — not e.title — for connection-row descriptions: the title carries
  // the rel-cat as its first line, which would otherwise duplicate the rel-chip.
  function _firstPhrase(fullDescription) {
    if (!fullDescription) return '';
    var parts = String(fullDescription).split('\x1f');
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i].replace(/\s+/g, ' ').trim();
      if (p) return p;
    }
    return '';
  }

  function _toggleConnExpand(btn) {
    var item = btn.closest('.conn-item');
    if (!item) return;
    var detail = item.nextElementSibling;
    if (!detail || !detail.classList.contains('conn-topic-detail')) return;
    var willOpen = detail.style.display === 'none';
    detail.style.display = willOpen ? '' : 'none';
    btn.innerHTML = willOpen ? '&#9662;' : '&#9656;';
    btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  }
  window._toggleConnExpand = _toggleConnExpand;

  function _toggleAllConnExpand(btn) {
    var group = btn.closest('.conn-group');
    if (!group) return;
    var expands = group.querySelectorAll('.conn-expand');
    if (!expands.length) return;
    var anyClosed = Array.prototype.some.call(expands, function(e) {
      return e.getAttribute('aria-expanded') !== 'true';
    });
    var willOpen = anyClosed;
    Array.prototype.forEach.call(expands, function(e) {
      var item = e.closest('.conn-item');
      if (!item) return;
      var detail = item.nextElementSibling;
      if (!detail || !detail.classList.contains('conn-topic-detail')) return;
      detail.style.display = willOpen ? '' : 'none';
      e.innerHTML = willOpen ? '&#9662;' : '&#9656;';
      e.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    });
    btn.textContent = willOpen ? 'Collapse all' : 'Expand all';
  }
  window._toggleAllConnExpand = _toggleAllConnExpand;

  // Source-topic provenance — closes the graph→source loop. Renders the
  // chunks each node/edge was extracted from, resolved at build time
  // (visualize.py:_load_chunk_to_topic) and shipped in GRAPH_META.topicIndex.
  // Hub nodes can reference 100+ topics; cap at 10 with a "+N more"
  // chip so the panel doesn't scroll forever.
  //
  // Click a row → openTopicModal(tid). We don't use a plain <a href> to
  // ../topics/<id>.json because Chrome refuses file://→file:// link
  // navigation under its "Not allowed to load local resource" policy
  // (the same reason data.js is .js not .json — fetch() is blocked too).
  // The modal renders the title + meta + excerpt from topicIndex and
  // offers two fallbacks: window.open (best-effort, may still fail
  // under file://) and a Copy file path button.
  var _SOURCE_TOPICS_CAP = 10;

  // Node source_id only marks first-definition; per-topic provenance lives on incident edges.
  function _unionAndRankTopicIds(nodeTopicIds, connEdges) {
    var counts = new Map();
    function bump(tid) { counts.set(tid, (counts.get(tid) || 0) + 1); }
    (nodeTopicIds || []).forEach(bump);
    (connEdges || []).forEach(function(cid) {
      var e = edgeData.get(cid);
      if (e && e.topicIds) e.topicIds.forEach(bump);
    });
    return Array.from(counts.entries())
      .sort(function(a, b) { return b[1] - a[1]; })
      .map(function(entry) { return entry[0]; });
  }

  function _renderSourceTopics(topicIds, opts) {
    if (!topicIds || !topicIds.length) return '';
    opts = opts || {};
    var index = (window.GRAPH_META && GRAPH_META.topicIndex) || {};
    var rows = topicIds.slice(0, _SOURCE_TOPICS_CAP);
    var hidden = topicIds.length - rows.length;
    var html = '<div class="connections">';
    if (!opts.omitHeader) html += '<h4>Source topics</h4>';
    rows.forEach(function(tid) {
      var info = index[tid];
      var title = (info && info.title) || ('Topic ' + tid);
      var date = (info && info.createdAt) ? info.createdAt.slice(0, 10) : '';
      var postCount = (info && info.postCount) || 0;
      var byline = (info && info.firstPostBy) ? info.firstPostBy : '';
      var excerpt = (info && info.excerpt) ? info.excerpt : '';
      var meta = [];
      if (date) meta.push(date);
      if (postCount) meta.push(postCount + ' post' + (postCount === 1 ? '' : 's'));
      if (byline) meta.push('by ' + byline);
      var tidJs = String(tid).replace(/'/g, "\\'");
      html += '<div class="topic-row" onclick="openTopicModal(\'' + tidJs +
              '\')" title="Open topic preview">' +
              '<span class="topic-row-link">' + esc(title) + '</span>';
      if (meta.length) {
        html += ' <span class="topic-row-meta">' + esc(meta.join(' · ')) + '</span>';
      }
      if (excerpt) {
        html += '<div class="topic-row-excerpt">' + esc(excerpt) + '</div>';
      }
      html += '</div>';
    });
    if (hidden > 0) {
      html += '<div class="topic-row-more">+ ' + hidden + ' more topic' +
              (hidden === 1 ? '' : 's') + ' (provenance is per-chunk; hub entities accumulate many)</div>';
    }
    html += '</div>';
    return html;
  }

  // Topic preview modal. Built around what's already in topicIndex
  // (title, date, post count, author, first-post excerpt) — no extra
  // network or file fetches, so it works offline + over file://.
  // The "Open raw JSON" button uses window.open() which may still fail
  // under file:// (browser-specific); the "Copy file path" button is
  // the universal fallback for users who want to inspect the JSON
  // directly in their preferred tool.
  function openTopicModal(tid) {
    var index = (window.GRAPH_META && GRAPH_META.topicIndex) || {};
    var info = index[tid] || {};
    var title = info.title || ('Topic ' + tid);
    var date = info.createdAt || '';
    var postCount = info.postCount || 0;
    var byline = info.firstPostBy || '';
    var posts = info.posts || [];
    var jsonHref = '../topics/' + encodeURIComponent(tid) + '.json';
    var modal = document.getElementById('topic-modal');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'topic-modal';
      document.body.appendChild(modal);
      modal.addEventListener('click', function(evt) {
        // Backdrop click closes; clicks inside the inner card don't.
        if (evt.target === modal) closeTopicModal();
      });
    }
    var meta = [];
    if (date) meta.push(esc(date));
    if (postCount) meta.push(postCount + ' post' + (postCount === 1 ? '' : 's'));
    if (byline) meta.push('by ' + esc(byline));

    var threadHtml = '';
    if (posts.length) {
      threadHtml = '<div class="topic-modal-thread">';
      posts.forEach(function(p) {
        var postMeta = [];
        if (p.postNumber) postMeta.push('#' + p.postNumber);
        var who = p.displayName || p.username || '';
        if (who) postMeta.push(who);
        if (p.createdAt) postMeta.push(String(p.createdAt).slice(0, 10));
        threadHtml +=
          '<div class="topic-modal-post">' +
            '<div class="topic-modal-post-meta">' + esc(postMeta.join(' · ')) + '</div>' +
            '<div class="topic-modal-post-body">' + esc(p.plainText || '') + '</div>' +
          '</div>';
      });
      threadHtml += '</div>';
    } else if (info.excerpt) {
      // Back-compat path for builds that only emitted the short excerpt.
      threadHtml = '<div class="topic-modal-thread topic-modal-empty">' +
                   esc(info.excerpt) + '</div>';
    } else {
      threadHtml = '<div class="topic-modal-thread topic-modal-empty">' +
                   '(No post content available)</div>';
    }

    modal.innerHTML =
      '<div class="topic-modal-card">' +
        '<button class="topic-modal-close" onclick="closeTopicModal()" title="Close (Esc)">&times;</button>' +
        '<h3 class="topic-modal-title">' + esc(title) + '</h3>' +
        '<div class="topic-modal-meta">Topic #' + esc(tid) +
          (meta.length ? ' · ' + meta.join(' · ') : '') + '</div>' +
        threadHtml +
        '<div class="topic-modal-actions">' +
          '<button class="action-btn" onclick="window.open(\'' + jsonHref +
            '\', \'_blank\') || alert(\'Browser blocked the popup. Use Copy file path.\')" ' +
            'title="Try opening the topic JSON in a new tab. Chrome may block this under file:// — use Copy file path as a fallback.">Open raw JSON</button>' +
          '<button id="topic-modal-copy-btn" class="action-btn" onclick="copyTopicPath(\'' + tidJs(tid) + '\')" ' +
            'title="Copy the absolute path to the topic JSON so you can open it in your preferred viewer">Copy file path</button>' +
        '</div>' +
      '</div>';
    modal.style.display = 'flex';
  }
  function closeTopicModal() {
    var m = document.getElementById('topic-modal');
    if (m) m.style.display = 'none';
  }
  function tidJs(tid) { return String(tid).replace(/'/g, "\\'"); }
  function copyTopicPath(tid) {
    // Resolve the topic JSON path relative to the current document.
    // window.location.href is graph.html → strip the filename, append
    // ../topics/<id>.json, and hand the absolute URL to clipboard.
    var here = window.location.href.split('?')[0].split('#')[0];
    var dir = here.substring(0, here.lastIndexOf('/') + 1);
    var url = new URL('../topics/' + encodeURIComponent(tid) + '.json', dir).href;
    copyToClipboard(url, 'topic-modal-copy-btn');
  }
  window.openTopicModal = openTopicModal;
  window.closeTopicModal = closeTopicModal;
  window.copyTopicPath = copyTopicPath;
  // Esc closes the topic modal; doesn't intercept other Esc handling
  // because the keydown listener short-circuits when the modal isn't
  // visible.
  document.addEventListener('keydown', function(evt) {
    if (evt.key !== 'Escape') return;
    var m = document.getElementById('topic-modal');
    if (m && m.style.display !== 'none') closeTopicModal();
  });

  // Glance-panel + table-row navigation: select the node, focus the
  // camera on it (animated), and open its detail panel. Same compound
  // behaviour the #detail-content connection rows already use via the
  // delegated handler — extracted as a helper so glance + future call
  // sites get the same UX.
  function showAndFocusNode(nodeId) {
    var n = nodeData.get(nodeId);
    if (!n) return;
    network.selectNodes([nodeId]);
    network.focus(nodeId, {scale: 1.2, animation: {duration: 400, easingFunction: 'easeInOutQuad'}});
    showNodeDetail(nodeId);
  }
  window.showAndFocusNode = showAndFocusNode;

  // Recenter only — used by the "click the title to recenter" affordance
  // on node + category detail panels. Doesn't reopen the panel (it's
  // already open) and doesn't push to nav history (we're not changing
  // what's inspected, just where the camera is).
  function recenterOnNode(nodeId) {
    if (!nodeData.get(nodeId)) return;
    network.selectNodes([nodeId]);
    network.focus(nodeId, {scale: 1.2, animation: {duration: 400, easingFunction: 'easeInOutQuad'}});
  }
  window.recenterOnNode = recenterOnNode;

  // Recenter on an edge: fit the camera to both endpoints, plus a brief
  // selection so the edge highlights. Used by the title click on the
  // edge detail panel.
  function recenterOnEdge(edgeId) {
    var e = edgeData.get(edgeId);
    if (!e) return;
    network.setSelection({nodes: [], edges: [edgeId]}, {unselectAll: true});
    network.fit({nodes: [e.from, e.to], animation: {duration: 400, easingFunction: 'easeInOutQuad'}});
  }
  window.recenterOnEdge = recenterOnEdge;

  // Personalized PageRank from a seed node. Random-walk-with-restart on
  // the undirected adjacency: each iteration distributes (1-α) of every
  // node's mass equally to its neighbours, plus α back to the seed.
  // After ~30 iterations on a sparse 16k/24k graph (~10 ms in practice)
  // the stationary distribution scores each other node by structural
  // proximity to the seed — surfaces 2- and 3-hop nodes that share many
  // intermediate neighbours, which 1-hop / 2-hop focus alone misses.
  function personalizedPageRank(seedId, opts) {
    opts = opts || {};
    var alpha = opts.alpha || 0.15;
    var iterations = opts.iterations || 30;
    var topK = opts.topK || 10;
    if (!neighborIndex[seedId]) return [];

    var p = Object.create(null);
    p[seedId] = 1.0;
    for (var iter = 0; iter < iterations; iter++) {
      var p_next = Object.create(null);
      p_next[seedId] = alpha;
      var ids = Object.keys(p);
      for (var i = 0; i < ids.length; i++) {
        var id = ids[i];
        var neighbours = neighborIndex[id] || [];
        if (!neighbours.length) continue;
        var contribution = (1 - alpha) * p[id] / neighbours.length;
        for (var j = 0; j < neighbours.length; j++) {
          var nid = neighbours[j];
          p_next[nid] = (p_next[nid] || 0) + contribution;
        }
      }
      p = p_next;
    }

    var ranked = [];
    var keys = Object.keys(p);
    for (var k = 0; k < keys.length; k++) {
      if (keys[k] === seedId) continue;
      ranked.push({id: keys[k], score: p[keys[k]]});
    }
    ranked.sort(function(a, b) { return b.score - a.score; });
    return ranked.slice(0, topK);
  }
  window.personalizedPageRank = personalizedPageRank;

  // ===================== Copy-as-* (name / one-liner / Markdown) =====================
  // Each detail panel surfaces a split-button: the main half copies the
  // entity name (matching the original Copy behavior), and the ▾ half
  // opens a small menu with three formats. Markdown mirrors what the
  // panel renders, so the user gets a take-home artifact with the same
  // facts they were already looking at.
  //
  // Format dispatcher: kind ∈ {'node','edge','cluster','category','cat-edge'},
  // payload = nodeId | edgeId | communityId (number) | catNodeId | "catA||catB".
  function getCopyText(format, kind, payload) {
    if (kind === 'cluster') payload = Number(payload);
    if (format === 'name') return _copyName(kind, payload);
    if (format === 'oneliner') return _copyOneLiner(kind, payload);
    if (format === 'markdown') return _copyMarkdown(kind, payload);
    return '';
  }

  function _copyName(kind, payload) {
    if (kind === 'node') {
      var n = nodeData.get(payload); return String((n && n.id) || payload);
    }
    if (kind === 'category') {
      var n2 = nodeData.get(payload); return (n2 && n2.superCategory) || String(payload);
    }
    if (kind === 'cluster') return 'Cluster #' + (payload + 1);
    if (kind === 'edge') {
      var e = edgeData.get(payload); if (!e) return '';
      var fn = nodeData.get(e.from), tn = nodeData.get(e.to);
      var fl = e.from, tl = e.to;
      return fl + ' -> ' + tl;
    }
    if (kind === 'cat-edge') {
      var p = String(payload).split('||');
      if (p.length !== 2) return '';
      return p[0] === p[1] ? (p[0] + ' (internal)') : (p[0] + ' ↔ ' + p[1]);
    }
    return '';
  }

  // First sentence of `text`, trimmed and capped at `maxChars`. Falls back
  // to the whole text when no sentence-ender is present (.! or ?).
  function _firstSentence(text, maxChars) {
    if (!text) return '';
    var s = String(text).replace(/\s+/g, ' ').trim();
    var idx = s.search(/[.!?](\s|$)/);
    var first = idx === -1 ? s : s.slice(0, idx + 1);
    if (first.length > maxChars) first = first.slice(0, maxChars - 1).trim() + '…';
    return first;
  }

  // Histogram of neighbour entity-types (or super-category fallback).
  function _topNeighborTypes(nodeId) {
    var counts = Object.create(null);
    (neighborIndex[nodeId] || []).forEach(function(nid) {
      var n = nodeData.get(nid); if (!n) return;
      var key = n.entityType || n.superCategory || 'Other';
      counts[key] = (counts[key] || 0) + 1;
    });
    return Object.keys(counts).map(function(k) { return [k, counts[k]]; })
      .sort(function(a, b) { return b[1] - a[1]; });
  }

  function _ordinal(n) {
    if (n === 1) return 'largest';
    var m100 = n % 100, m10 = n % 10;
    var suff = (m100 >= 11 && m100 <= 13) ? 'th'
             : (m10 === 1) ? 'st' : (m10 === 2) ? 'nd' : (m10 === 3) ? 'rd' : 'th';
    return n + suff + '-largest';
  }

  function _copyOneLiner(kind, payload) {
    if (kind === 'node') {
      var n = nodeData.get(payload); if (!n) return '';
      var head = (n.id || payload) + (n.entityType ? ' (' + n.entityType + ')' : (n.superCategory ? ' (' + n.superCategory + ')' : ''));
      var deg = n.degree || 0;
      var pieces = [head + ' — ' + deg + ' connection' + (deg === 1 ? '' : 's')];
      var topNT = _topNeighborTypes(payload).slice(0, 2);
      if (topNT.length) {
        pieces.push('mostly ' + topNT.map(function(kv) { return kv[0] + ' (' + kv[1] + ')'; }).join(' + '));
      }
      var first = _firstSentence(n.fullDescription, 160);
      var line = pieces.join(', ');
      if (first) line += '. "' + first + '"';
      return line;
    }
    if (kind === 'category') {
      var cn = nodeData.get(payload);
      var name = (cn && cn.superCategory) || String(payload);
      var size = (cn && cn.nodeCount) || 0;
      var topHubs = originalNodes
        .filter(function(x) { return x.superCategory === name && !x.isSuperNode; })
        .sort(function(a, b) { return (b.degree || 0) - (a.degree || 0); })
        .slice(0, 2)
        .map(function(x) { return x.label || x.id; });
      var catEdges = (window.GRAPH_META && GRAPH_META.categoryEdges) || {};
      var topNeighbour = null;
      Object.keys(catEdges).forEach(function(key) {
        var parts = key.split('|');
        if (parts[0] === parts[1]) return;
        if (parts[0] !== name && parts[1] !== name) return;
        var other = parts[0] === name ? parts[1] : parts[0];
        var cnt = catEdges[key].count;
        if (!topNeighbour || cnt > topNeighbour.count) topNeighbour = {cat: other, count: cnt};
      });
      var line = name + ' — ' + size.toLocaleString() + ' members';
      if (topHubs.length) line += '; top hubs: ' + topHubs.join(', ');
      if (topNeighbour) line += '; most-linked to ' + topNeighbour.cat + ' (' + topNeighbour.count + ' edges)';
      return line;
    }
    if (kind === 'cluster') {
      var sizes = (GRAPH_META.communitySizes || []);
      if (payload < 0 || payload >= sizes.length) return '';
      var memberIds = new Set();
      var byCat = Object.create(null);
      originalNodes.forEach(function(n) {
        if (n.community !== payload) return;
        memberIds.add(n.id);
        var cat = n.superCategory || 'Other';
        byCat[cat] = (byCat[cat] || 0) + 1;
      });
      var memberCount = memberIds.size;
      var internalDeg = Object.create(null);
      memberIds.forEach(function(id) { internalDeg[id] = 0; });
      var bridgeEdgeCount = 0;
      originalEdges.forEach(function(e) {
        var fIn = memberIds.has(e.from), tIn = memberIds.has(e.to);
        if (fIn && tIn) { internalDeg[e.from]++; internalDeg[e.to]++; }
        else if (fIn || tIn) bridgeEdgeCount++;
      });
      var topCats = Object.keys(byCat).map(function(k) { return [k, byCat[k]]; })
        .sort(function(a, b) { return b[1] - a[1]; }).slice(0, 2);
      var topHubs = Object.keys(internalDeg).map(function(id) {
        var nd = nodeData.get(id) || {}; return {label: id || nd.label, deg: internalDeg[id]};
      }).sort(function(a, b) { return b.deg - a.deg; }).slice(0, 2);
      var line = 'Cluster of ' + memberCount + ' members (' + _ordinal(payload + 1) + ' of ' + sizes.length.toLocaleString() + ')';
      if (topCats.length) {
        line += '; mostly ' + topCats.map(function(kv) {
          var p = memberCount > 0 ? Math.round((kv[1] / memberCount) * 100) : 0;
          return kv[0] + ' (' + kv[1] + ', ' + p + '%)';
        }).join(' + ');
      }
      if (topHubs.length && topHubs[0].deg > 0) {
        line += '; led by ' + topHubs.map(function(h) { return h.label; }).join(', ');
      }
      if (bridgeEdgeCount > 0) line += '; ' + bridgeEdgeCount.toLocaleString() + ' bridges out';
      return line;
    }
    if (kind === 'edge') {
      var e = edgeData.get(payload); if (!e) return '';
      var fn2 = nodeData.get(e.from), tn2 = nodeData.get(e.to);
      var fl2 = e.from, tl2 = e.to;
      var verb = e.relCategory || 'related to';
      var phrases = String(e.fullDescription || '').split('\x1f')
        .map(function(s) { return s.trim(); }).filter(Boolean);
      var desc = phrases.length ? _firstSentence(phrases[0], 160) : '';
      var line = fl2 + ' ' + verb + ' ' + tl2;
      if (desc) line += ' — ' + desc;
      return line;
    }
    if (kind === 'cat-edge') {
      var p = String(payload).split('||');
      if (p.length !== 2) return '';
      var s = _categoryEdgeStats(p[0], p[1]);
      var head = s.isSuperNode || s.isSelf ? (s.catA + ' (internal)') : (s.catA + ' ↔ ' + s.catB);
      var line = head + ': ' + s.totalCount.toLocaleString() + ' edges';
      if (s.topRel.length) {
        line += '; mostly ' + s.topRel.slice(0, 2).map(function(kv) {
          return kv[0] + ' (' + kv[1].toLocaleString() + ')';
        }).join(', ');
      }
      if (s.topBridges.length) {
        var b0 = s.topBridges[0];
        line += '; top: ' + b0.fromLabel + ' ↔ ' + b0.toLabel +
                ' (' + b0.relCategory + ', w=' + b0.weight.toFixed(1) + ')';
      }
      return line;
    }
    return '';
  }

  function _copyMarkdown(kind, payload) {
    if (kind === 'node') return _mdNode(payload);
    if (kind === 'category') return _mdCategory(payload);
    if (kind === 'cluster') return _mdCluster(payload);
    if (kind === 'edge') return _mdEdge(payload);
    if (kind === 'cat-edge') return _mdCatEdge(payload);
    return '';
  }

  function _mdCatEdge(payload) {
    var p = String(payload).split('||');
    if (p.length !== 2) return '';
    var s = _categoryEdgeStats(p[0], p[1]);
    var lines = [];
    lines.push('# ' + (s.isSelf ? (s.catA + ' (internal)') : (s.catA + ' ↔ ' + s.catB)));
    lines.push('');
    var meta = ['**Edges:** ' + s.totalCount.toLocaleString(),
                '**Total weight:** ' + s.totalWeight.toFixed(0),
                '**Avg weight:** ' + s.avgWeight.toFixed(2)];
    if (s.topRel.length) meta.push('**Rel-types:** ' + s.topRel.length);
    meta.forEach(function(m) { lines.push('- ' + m); });
    if (s.topRel.length) {
      lines.push('');
      lines.push('## Relationship types');
      s.topRel.forEach(function(kv) {
        var pct = s.totalCount > 0 ? Math.round((kv[1] / s.totalCount) * 100) : 0;
        lines.push('- ' + kv[0] + ' — ' + kv[1].toLocaleString() + ' (' + pct + '%)');
      });
    }
    if (s.topBridges.length) {
      lines.push('');
      lines.push('## Top concrete bridges (by weight)');
      s.topBridges.forEach(function(br) {
        lines.push('- ' + br.fromLabel + ' ↔ ' + br.toLabel +
                   ' [' + br.relCategory + '] (w=' + br.weight.toFixed(1) + ')');
      });
    }
    if (s.isSelf) {
      if (s.topContributorsA.length) {
        lines.push('');
        lines.push('## Top contributors');
        s.topContributorsA.forEach(function(c) {
          lines.push('- ' + c.label + ' — ' + c.count + ' edge' + (c.count === 1 ? '' : 's'));
        });
      }
    } else {
      if (s.topContributorsA.length) {
        lines.push('');
        lines.push('## From ' + s.catA);
        s.topContributorsA.forEach(function(c) {
          lines.push('- ' + c.label + ' — ' + c.count + ' edge' + (c.count === 1 ? '' : 's'));
        });
      }
      if (s.topContributorsB.length) {
        lines.push('');
        lines.push('## From ' + s.catB);
        s.topContributorsB.forEach(function(c) {
          lines.push('- ' + c.label + ' — ' + c.count + ' edge' + (c.count === 1 ? '' : 's'));
        });
      }
    }
    return lines.join('\n');
  }

  function _mdNode(nodeId) {
    var n = nodeData.get(nodeId); if (!n) return '';
    var lines = [];
    lines.push('# ' + (n.id || nodeId));
    lines.push('');
    var meta = [];
    if (n.entityType) meta.push('**Type:** ' + n.entityType);
    if (n.superCategory) meta.push('**Category:** ' + n.superCategory);
    var deg = n.degree || 0;
    var rank = degreeRank[nodeId];
    if (rank && totalNodeCount > 0) {
      var pct = (rank / totalNodeCount) * 100;
      var suff = '';
      if (pct <= 1) suff = ', top 1%';
      else if (pct <= 5) suff = ', top 5%';
      else if (pct <= 10) suff = ', top 10%';
      meta.push('**Degree:** ' + deg + ' (#' + rank + ' of ' + totalNodeCount.toLocaleString() + suff + ')');
    } else {
      meta.push('**Degree:** ' + deg);
    }
    if (typeof n.community === 'number' && n.community >= 0) {
      var sizes = (GRAPH_META.communitySizes || []);
      meta.push('**Cluster:** #' + (n.community + 1) + ' (' + (sizes[n.community] || 0) + ' members)');
    }
    if (n.isArticulation) meta.push('**Cut node**');
    meta.forEach(function(m) { lines.push('- ' + m); });

    if (n.fullDescription) {
      var descPhrasesMd = String(n.fullDescription)
        .split('\x1f')
        .map(function(s) { return s.trim(); })
        .filter(function(s) { return s; });
      if (descPhrasesMd.length) {
        lines.push('');
        lines.push('## Description');
        if (descPhrasesMd.length > 1) {
          var topicCountMd = (n.topicIds && n.topicIds.length) || 0;
          var annotationMd = descPhrasesMd.length + ' phrases';
          if (topicCountMd > 0) annotationMd += ' · ' + topicCountMd + ' topic' + (topicCountMd === 1 ? '' : 's');
          lines.push('');
          lines.push('_' + annotationMd + '_');
        }
        // Multi-phrase: emit each as its own H3 subsection. Subsections survive
        // every Markdown renderer (no loose-list continuation rules to
        // misinterpret) and stay machine-parseable as a structured doc.
        // Single-phrase: skip the subsection, just emit prose under ## Description.
        if (descPhrasesMd.length === 1) {
          lines.push('');
          lines.push(descPhrasesMd[0]);
        } else {
          descPhrasesMd.forEach(function(p, idx) {
            lines.push('');
            lines.push('### Phrase ' + (idx + 1));
            lines.push('');
            lines.push(p);
          });
        }
      }
    }

    var ppr = personalizedPageRank(nodeId, {topK: 10});
    if (ppr.length > 0) {
      var directSet = new Set(neighborIndex[nodeId] || []);
      lines.push('');
      lines.push('## Related (Personalized PageRank)');
      ppr.forEach(function(r) {
        var rn = nodeData.get(r.id); if (!rn) return;
        var direct = directSet.has(r.id) ? ' _(1-hop)_' : '';
        lines.push('- ' + (r.id || rn.label) + ' — ' + r.score.toFixed(4) + direct);
      });
    }

    var connected = network.getConnectedNodes(nodeId);
    var connEdges = network.getConnectedEdges(nodeId);
    var topicIdsAlreadyShown = new Set();
    if (connected.length > 0) {
      var topicIndex = (window.GRAPH_META && GRAPH_META.topicIndex) || {};
      var groups = {};
      connected.forEach(function(cid) {
        var cn = nodeData.get(cid);
        var cat = cn ? (cn.superCategory || 'Other') : 'Other';
        if (!groups[cat]) groups[cat] = [];
        var edgeDesc = '', relCat = '';
        for (var i = 0; i < connEdges.length; i++) {
          var e = edgeData.get(connEdges[i]);
          if (e && ((e.from === nodeId && e.to === cid) || (e.from === cid && e.to === nodeId))) {
            edgeDesc = _firstPhrase(e.fullDescription);
            relCat = e.relCategory || '';
            break;
          }
        }
        var topicId = '';
        if (cn && cn.entityType === 'topic' && cn.topicIds && cn.topicIds.length) {
          topicId = String(cn.topicIds[0]);
          topicIdsAlreadyShown.add(topicId);
        }
        groups[cat].push({label: cn ? (cid || cn.label) : cid, desc: edgeDesc, relCat: relCat, topicId: topicId});
      });
      var sortedCats = Object.keys(groups).sort(function(a, b) { return groups[b].length - groups[a].length; });
      lines.push('');
      lines.push('## Connections');
      sortedCats.forEach(function(cat) {
        lines.push('');
        lines.push('### ' + cat + ' (' + groups[cat].length + ')');
        var rows = groups[cat];
        var cap = 25;
        rows.slice(0, cap).forEach(function(r) {
          var prefix = r.relCat ? ('**' + r.relCat + '** ') : '';
          var topicInfo = r.topicId ? topicIndex[r.topicId] : null;
          // Topic neighbors: drop the (boilerplate) edge desc; surface topic
          // metadata as a 2-space-indented italic continuation line instead.
          if (topicInfo) {
            var titleLink = '[' + r.label + '](../topics/' + r.topicId + '.json)';
            lines.push('- ' + prefix + titleLink + ' (#' + r.topicId + ')');
            var meta = [];
            if (topicInfo.createdAt) meta.push(String(topicInfo.createdAt).slice(0, 10));
            if (topicInfo.postCount) meta.push(topicInfo.postCount + ' post' + (topicInfo.postCount === 1 ? '' : 's'));
            if (topicInfo.firstPostBy) meta.push('started by ' + topicInfo.firstPostBy);
            if (meta.length) lines.push('  _' + meta.join(' · ') + '_');
          } else {
            var suffix = r.desc ? (' — ' + r.desc) : '';
            lines.push('- ' + prefix + r.label + suffix);
          }
        });
        if (rows.length > cap) lines.push('- _… +' + (rows.length - cap) + ' more_');
      });
    }
    var rankedTopicIdsMd = _unionAndRankTopicIds(n.topicIds, connEdges);
    var residualTopicIdsMd = rankedTopicIdsMd.filter(function(tid) { return !topicIdsAlreadyShown.has(String(tid)); });
    _appendMdSourceTopics(lines, residualTopicIdsMd, {hasOverlap: topicIdsAlreadyShown.size > 0});
    return lines.join('\n');
  }

  // Shared between _mdNode and _mdEdge. Lean output: just a markdown
  // link to the topic JSON path + the topic id for findability. No
  // excerpt or other metadata in the markdown — the panel already
  // shows that on screen, and the markdown is meant for sharing /
  // referencing, not duplicating content. Cap matches the in-panel
  // _SOURCE_TOPICS_CAP so hub entities don't dump 100 rows.
  function _appendMdSourceTopics(lines, topicIds, opts) {
    if (!topicIds || !topicIds.length) return;
    opts = opts || {};
    var index = (window.GRAPH_META && GRAPH_META.topicIndex) || {};
    var cap = (typeof _SOURCE_TOPICS_CAP === 'number') ? _SOURCE_TOPICS_CAP : 10;
    var rows = topicIds.slice(0, cap);
    var hidden = topicIds.length - rows.length;
    lines.push('');
    lines.push(opts.hasOverlap ? '## Source topics — not shown above' : '## Source topics');
    rows.forEach(function(tid) {
      var info = index[tid] || {};
      var title = info.title || ('Topic ' + tid);
      var meta = [];
      if (info.createdAt) meta.push(String(info.createdAt).slice(0, 10));
      if (info.postCount) meta.push(info.postCount + ' post' + (info.postCount === 1 ? '' : 's'));
      if (info.firstPostBy) meta.push('by ' + info.firstPostBy);
      var line = '- [' + title + '](../topics/' + tid + '.json) (#' + tid + ')';
      if (meta.length) line += ' — ' + meta.join(' · ');
      lines.push(line);
    });
    if (hidden > 0) {
      lines.push('- _… +' + hidden + ' more topic' + (hidden === 1 ? '' : 's') + '_');
    }
  }

  function _mdEdge(edgeId) {
    var e = edgeData.get(edgeId); if (!e) return '';
    var fn = nodeData.get(e.from), tn = nodeData.get(e.to);
    var fl = e.from, tl = e.to;
    var lines = [];
    lines.push('# ' + fl + ' → ' + tl);
    lines.push('');
    var meta = [];
    if (e.relCategory) meta.push('**Type:** ' + e.relCategory);
    if (e.edgeWeight) meta.push('**Weight:** ' + e.edgeWeight.toFixed(1));
    meta.forEach(function(m) { lines.push('- ' + m); });
    var phrases = String(e.fullDescription || '')
      .split('\x1f')
      .map(function(s) { return s.trim(); })
      .filter(function(s) { return s; });
    if (phrases.length) {
      lines.push('');
      lines.push('## Description');
      phrases.forEach(function(p) { lines.push('- ' + p); });
    }
    _appendMdSourceTopics(lines, e.topicIds);
    return lines.join('\n');
  }

  function _mdCategory(catNodeId) {
    var n = nodeData.get(catNodeId); if (!n) return '';
    var cat = n.superCategory || catNodeId;
    var lines = ['# Category: ' + cat, ''];
    lines.push('- **Members:** ' + (n.nodeCount || 0));
    var catEdges = (window.GRAPH_META && GRAPH_META.categoryEdges) || {};
    var connections = [];
    Object.keys(catEdges).forEach(function(key) {
      var parts = key.split('|');
      if (parts[0] === cat || parts[1] === cat) {
        var other = parts[0] === cat ? parts[1] : parts[0];
        var d = catEdges[key];
        connections.push({cat: other, count: d.count, isSelf: parts[0] === parts[1]});
      }
    });
    connections.sort(function(a, b) { return b.count - a.count; });
    if (connections.length) {
      lines.push('');
      lines.push('## Inter-category edges');
      connections.forEach(function(c) {
        var label = c.isSelf ? (c.cat + ' (internal)') : c.cat;
        lines.push('- ' + label + ' — ' + c.count + ' edges');
      });
    }
    return lines.join('\n');
  }

  function _mdCluster(communityId) {
    var sizes = (GRAPH_META.communitySizes || []);
    if (communityId < 0 || communityId >= sizes.length) return '';
    var memberIds = new Set();
    var byCat = Object.create(null);
    var artCount = 0;
    originalNodes.forEach(function(n) {
      if (n.community !== communityId) return;
      memberIds.add(n.id);
      var cat = n.superCategory || 'Other';
      byCat[cat] = (byCat[cat] || 0) + 1;
      if (n.isArticulation) artCount++;
    });
    var internalDeg = Object.create(null);
    memberIds.forEach(function(id) { internalDeg[id] = 0; });
    var byRel = Object.create(null);
    var bridgeEdgeCount = 0;
    var bridgeByCounter = Object.create(null);
    originalEdges.forEach(function(e) {
      var fromIn = memberIds.has(e.from), toIn = memberIds.has(e.to);
      if (fromIn && toIn) {
        internalDeg[e.from]++;
        internalDeg[e.to]++;
        var rc = e.relCategory || 'Other';
        byRel[rc] = (byRel[rc] || 0) + 1;
      } else if (fromIn || toIn) {
        bridgeEdgeCount++;
        var ext = fromIn ? e.to : e.from;
        bridgeByCounter[ext] = (bridgeByCounter[ext] || 0) + 1;
      }
    });
    var memberCount = memberIds.size;
    var rank = communityId + 1;
    var total = sizes.length;
    var pct = total > 0 ? (rank / total) * 100 : 100;
    var suff = '';
    if (pct <= 1) suff = ', top 1%';
    else if (pct <= 5) suff = ', top 5%';
    else if (pct <= 10) suff = ', top 10%';
    var lines = [];
    lines.push('# Cluster #' + rank);
    lines.push('');
    var meta = ['**Members:** ' + memberCount,
                '**Rank:** #' + rank + ' of ' + total.toLocaleString() + suff];
    if (artCount > 0) meta.push('**Cut nodes:** ' + artCount);
    meta.forEach(function(m) { lines.push('- ' + m); });

    var sortByCount = function(o) {
      return Object.keys(o).map(function(k) { return [k, o[k]]; })
        .sort(function(a, b) { return b[1] - a[1]; });
    };
    var topCats = sortByCount(byCat);
    if (topCats.length) {
      lines.push('');
      lines.push('## Composition');
      topCats.forEach(function(kv) {
        var memPct = memberCount > 0 ? Math.round((kv[1] / memberCount) * 100) : 0;
        lines.push('- ' + kv[0] + ' — ' + kv[1] + ' (' + memPct + '%)');
      });
    }
    var hubs = Object.keys(internalDeg).map(function(id) {
      var n = nodeData.get(id) || {};
      return {label: id || n.label, deg: internalDeg[id]};
    }).sort(function(a, b) { return b.deg - a.deg; }).slice(0, 10);
    if (hubs.length && hubs[0].deg > 0) {
      lines.push('');
      lines.push('## Top members (within-cluster degree)');
      hubs.forEach(function(h) {
        if (h.deg === 0) return;
        lines.push('- ' + h.label + ' — ' + h.deg);
      });
    }
    var topRels = sortByCount(byRel).slice(0, 8);
    if (topRels.length) {
      lines.push('');
      lines.push('## Top relationships (within cluster)');
      topRels.forEach(function(kv) { lines.push('- ' + kv[0] + ' — ' + kv[1]); });
    }
    if (bridgeEdgeCount > 0) {
      var topBridges = Object.keys(bridgeByCounter).map(function(id) {
        var n = nodeData.get(id) || {};
        return {label: id || n.label, count: bridgeByCounter[id]};
      }).sort(function(a, b) { return b.count - a.count; }).slice(0, 5);
      lines.push('');
      lines.push('## Bridges to other clusters — ' + bridgeEdgeCount.toLocaleString() + ' edges');
      topBridges.forEach(function(b) {
        lines.push('- ' + b.label + ' — ' + b.count + ' edge' + (b.count === 1 ? '' : 's'));
      });
    }
    return lines.join('\n');
  }

  // Split-button HTML. Kind+payload are stored on the toggle (▾) button so
  // the menu handler can reconstruct the format on demand. The main click
  // does the legacy plain-name copy so muscle memory is preserved.
  function renderCopySplit(kind, payload, anchorId) {
    var pl = String(payload).replace(/'/g, "\\'");
    var name = _copyName(kind, kind === 'cluster' ? Number(payload) : payload);
    var nameJs = String(name).replace(/'/g, "\\'");
    var titleAttr = name ? (' title="Copy &quot;' + esc(name) + '&quot;"') : '';
    return '<span class="copy-split">' +
           '<button class="action-btn copy-main" id="' + anchorId +
           '" onclick="copyToClipboard(\'' + nameJs + '\', \'' + anchorId + '\')"' +
           titleAttr + '>Copy</button>' +
           '<button class="action-btn copy-toggle" data-kind="' + kind +
           '" data-payload="' + esc(pl) + '" data-anchor="' + anchorId +
           '" onclick="openCopyMenu(this)" title="More copy formats">&#9662;</button>' +
           '</span>';
  }

  function _ensureCopyMenu() {
    var m = document.getElementById('copy-menu');
    if (m) return m;
    m = document.createElement('div');
    m.id = 'copy-menu';
    m.className = 'copy-menu';
    m.innerHTML =
      '<div class="copy-menu-item" data-format="name">Copy name</div>' +
      '<div class="copy-menu-item" data-format="oneliner">Copy one-liner</div>' +
      '<div class="copy-menu-item" data-format="markdown">Copy as Markdown</div>';
    document.body.appendChild(m);
    m.addEventListener('click', function(evt) {
      var item = evt.target.closest('.copy-menu-item');
      if (!item) return;
      var fmt = item.dataset.format;
      var kind = m.dataset.kind, payload = m.dataset.payload, anchor = m.dataset.anchor;
      if (!kind || payload === undefined) return;
      var text = getCopyText(fmt, kind, payload);
      if (text) copyToClipboard(text, anchor);
      _closeCopyMenu();
    });
    return m;
  }

  function _closeCopyMenu() {
    var m = document.getElementById('copy-menu');
    if (m) m.style.display = 'none';
    document.removeEventListener('mousedown', _outsideCopyHandler, true);
    document.removeEventListener('keydown', _escCopyHandler, true);
  }

  function _outsideCopyHandler(evt) {
    var m = document.getElementById('copy-menu');
    if (!m || m.style.display === 'none') return;
    if (m.contains(evt.target)) return;
    if (evt.target.closest('.copy-toggle')) return;
    _closeCopyMenu();
  }

  function _escCopyHandler(evt) {
    if (evt.key === 'Escape') _closeCopyMenu();
  }

  function openCopyMenu(toggleBtn) {
    var m = _ensureCopyMenu();
    m.dataset.kind = toggleBtn.dataset.kind;
    m.dataset.payload = toggleBtn.dataset.payload;
    m.dataset.anchor = toggleBtn.dataset.anchor || '';
    var rect = toggleBtn.getBoundingClientRect();
    m.style.display = 'block';
    // Show first to measure width, then nudge to stay on-screen.
    var menuRect = m.getBoundingClientRect();
    var left = rect.right - menuRect.width;
    if (left < 6) left = 6;
    if (left + menuRect.width > window.innerWidth - 6) left = window.innerWidth - menuRect.width - 6;
    m.style.left = left + 'px';
    m.style.top = (rect.bottom + 4) + 'px';
    setTimeout(function() {
      document.addEventListener('mousedown', _outsideCopyHandler, true);
      document.addEventListener('keydown', _escCopyHandler, true);
    }, 0);
  }
  window.openCopyMenu = openCopyMenu;
  window.getCopyText = getCopyText;

  // Copy-to-clipboard with a small "Copied!" toast.
  // Used by the Copy button in node + edge detail panels — saves the
  // user from selecting + Cmd-C-ing across panel internals.
  function copyToClipboard(text, anchorId) {
    var doneCallback = function () {
      if (!anchorId) return;
      var el = document.getElementById(anchorId);
      if (!el) return;
      var prev = el.textContent;
      el.textContent = 'Copied!';
      setTimeout(function () { el.textContent = prev; }, 900);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(doneCallback, function () {});
    } else {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed'; ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); doneCallback(); } catch (e) {}
      document.body.removeChild(ta);
    }
  }
  window.copyToClipboard = copyToClipboard;

  // Category view: clicking a super-node
  function showCategoryDetail(nodeId) {
    var node = nodeData.get(nodeId);
    if (!node || !node.isSuperNode) return;
    clearEdgeSelection();
    clearPathsIfActive();
    applyNodeSelection([nodeId]);
    window.lastDetailNodeId = nodeId;
    window.lastDetailKind = 'category';
    var rb = document.getElementById('reopen-detail-btn'); if (rb) rb.disabled = false;
    navPush({kind: 'category', payload: nodeId});
    var cat = node.superCategory;
    var catColor = GRAPH_META.superCategoryColors[cat] || '#888';
    var s = _categoryStats(cat);

    var html = '<div class="entity-badge" style="background:' + catColor + '33; color:' + catColor + '">' +
               esc(cat) + '</div>';
    var titleCatNidJs = esc(nodeId).replace(/'/g, "\\'");
    html += '<h3 class="detail-title" onclick="recenterOnNode(\'' + titleCatNidJs +
            '\')" title="Click to recenter the canvas on this super-node">' + esc(cat) + '</h3>';
    html += '<div class="detail-meta">' + s.memberCount.toLocaleString() + ' members';
    if (s.topEntityTypes.length) {
      if (s.topEntityTypes.length > 1) {
        html += ' &bull; ' + s.topEntityTypes.length + ' entity types';
      }
    }
    html += '</div>';

    // Actions live at the top of the panel — matches node + cluster
    // panels and stays visible without scrolling past the data sections.
    html += '<div class="detail-actions">';
    html += '<button class="action-btn" onclick="recenterOnNode(\'' + titleCatNidJs + '\')" title="Pan + zoom the camera to center this super-node in view">Recenter</button>';
    html += '<button class="action-btn" onclick="drillIntoCategory(\'' + esc(cat) + '\')" title="Switch to Node view and filter to entities in this category">Drill into ' + esc(cat) + '</button>';
    html += renderCopySplit('category', nodeId, 'copy-cat-btn');
    html += '</div>';

    // Composition by entity type — small bar histogram. Skipped when
    // the category contains a single entity type (a 100%-only bar
    // wastes vertical space and tells the user nothing).
    if (s.topEntityTypes.length > 1) {
      html += '<div class="connections"><h4>Composition by entity type</h4>';
      var maxEt = s.topEntityTypes[0][1];
      s.topEntityTypes.forEach(function(kv) {
        var et = kv[0], cnt = kv[1];
        var pct = maxEt > 0 ? (cnt / maxEt) * 100 : 0;
        var memPct = s.memberCount > 0 ? Math.round((cnt / s.memberCount) * 100) : 0;
        html += '<div class="cluster-bar-row" title="' + esc(et) + ': ' + cnt + ' members (' + memPct + '% of category)">' +
                '<span class="cluster-bar-label">' + esc(et) + '</span>' +
                '<span class="cluster-bar-track"><span class="cluster-bar-fill" style="width:' + pct.toFixed(1) +
                '%; background:' + catColor + '"></span></span>' +
                '<span class="cluster-bar-count">' + cnt + ' (' + memPct + '%)</span>' +
                '</div>';
      });
      html += '</div>';
    }

    // Top members by within-category degree. Clickable to drill straight
    // into Node view + open the entity's detail.
    if (s.topHubs.length && s.topHubs[0].deg > 0) {
      html += '<div class="connections"><h4>Top members (within-category degree)</h4>';
      s.topHubs.forEach(function(h) {
        if (h.deg === 0) return;
        var typeChip = h.entityType
          ? ' <span class="cat-hub-type">' + esc(h.entityType) + '</span>'
          : '';
        html += '<div class="conn-item" data-node-id="' + esc(h.id) + '"' +
                ' title="Drill into this entity (switches to Node view)">' +
                '<span class="conn-dot" style="background:' + catColor + '"></span>' +
                esc(h.label) + typeChip +
                ' <span class="conn-edge-desc">&mdash; ' + h.deg + ' within-category</span>' +
                '</div>';
      });
      html += '</div>';
    }

    // Inter-category edges + top concrete bridge entities per pair. The
    // bridges row is what makes the aggregate edges actionable: you can
    // drill from "Component–Issue: 3,200 edges" straight into the
    // entities behind the link.
    var catEdges = GRAPH_META.categoryEdges;
    var connections = [];
    Object.keys(catEdges).forEach(function(key) {
      var parts = key.split('|');
      if (parts[0] === cat || parts[1] === cat) {
        var other = parts[0] === cat ? parts[1] : parts[0];
        var d = catEdges[key];
        connections.push({cat: other, count: d.count, weight: d.weight, isSelf: parts[0] === parts[1]});
      }
    });
    connections.sort(function(a, b) { return b.count - a.count; });

    if (connections.length) {
      html += '<div class="connections"><h4>Inter-category edges</h4>';
      connections.forEach(function(c) {
        var gc = GRAPH_META.superCategoryColors[c.cat] || '#888';
        var label = c.isSelf ? (esc(c.cat) + ' (internal)') : esc(c.cat);
        html += '<div class="conn-item">' +
                '<span class="conn-dot" style="background:' + gc + '"></span>' +
                label + ' <span class="conn-edge-desc">&mdash; ' + c.count.toLocaleString() + ' edges</span>' +
                '</div>';
        if (!c.isSelf) {
          var brs = (s.bridgesByOtherCat[c.cat] || []).slice(0, 3);
          brs.forEach(function(b) {
            html += '<div class="conn-item bridge-row" data-node-id="' + esc(b.otherId) + '"' +
                    ' title="Drill into this bridge entity">' +
                    '<span class="bridge-arrow">&rsaquo;</span>' +
                    esc(b.label) +
                    ' <span class="conn-edge-desc">&mdash; ' + b.count + ' edge' + (b.count === 1 ? '' : 's') + '</span>' +
                    '</div>';
          });
        }
      });
      html += '</div>';
    }

    document.getElementById('detail-content').innerHTML = html;
    document.getElementById('detail-panel').style.display = 'block';
  }

  // ===================== Detail panel: category-edge =====================
  // Aggregate-edge panel for the inter-category links shown in Category
  // view (e.g. "Component ↔ Issue: 3,200 edges"). Surfaces what's
  // actually flowing across the link: relationship-type histogram, top
  // concrete bridges by weight, and top contributing entities per side.
  // Single pass over originalEdges per pair, session-cached.
  var _categoryEdgeStatsCache = Object.create(null);
  function _categoryEdgeStats(catA, catB) {
    var pair = [catA, catB].sort();
    var key = pair[0] + '||' + pair[1];
    if (_categoryEdgeStatsCache[key]) return _categoryEdgeStatsCache[key];
    var a = pair[0], b = pair[1];
    var isSelf = (a === b);
    var membersA = new Set(), membersB;
    if (isSelf) {
      originalNodes.forEach(function(n) {
        if (!n.isSuperNode && n.superCategory === a) membersA.add(n.id);
      });
      membersB = membersA;
    } else {
      membersB = new Set();
      originalNodes.forEach(function(n) {
        if (n.isSuperNode) return;
        if (n.superCategory === a) membersA.add(n.id);
        else if (n.superCategory === b) membersB.add(n.id);
      });
    }
    var totalCount = 0, totalWeight = 0;
    var byRel = Object.create(null);
    var bridges = [];
    var contribByA = Object.create(null);
    var contribByB = Object.create(null);
    originalEdges.forEach(function(e) {
      var fromInA = membersA.has(e.from), fromInB = membersB.has(e.from);
      var toInA = membersA.has(e.to), toInB = membersB.has(e.to);
      var matches = isSelf
        ? (fromInA && toInA)
        : ((fromInA && toInB) || (toInA && fromInB));
      if (!matches) return;
      totalCount++;
      var w = e.edgeWeight || 1;
      totalWeight += w;
      var rc = e.relCategory || 'Other';
      byRel[rc] = (byRel[rc] || 0) + 1;
      bridges.push({id: e.id, from: e.from, to: e.to, relCategory: rc, weight: w});
      if (isSelf) {
        contribByA[e.from] = (contribByA[e.from] || 0) + 1;
        contribByA[e.to] = (contribByA[e.to] || 0) + 1;
      } else {
        var aSide = fromInA ? e.from : e.to;
        var bSide = fromInA ? e.to : e.from;
        contribByA[aSide] = (contribByA[aSide] || 0) + 1;
        contribByB[bSide] = (contribByB[bSide] || 0) + 1;
      }
    });
    bridges.sort(function(x, y) { return y.weight - x.weight; });
    var topBridges = bridges.slice(0, 10).map(function(br) {
      var fn = nodeData.get(br.from) || _originalNodeById(br.from) || {};
      var tn = nodeData.get(br.to) || _originalNodeById(br.to) || {};
      return {
        id: br.id, from: br.from, to: br.to,
        fromLabel: br.from, toLabel: br.to,
        relCategory: br.relCategory, weight: br.weight,
      };
    });
    var topRel = Object.keys(byRel).map(function(r) { return [r, byRel[r]]; })
      .sort(function(x, y) { return y[1] - x[1]; });
    var sortContrib = function(obj) {
      return Object.keys(obj).map(function(id) {
        var n = nodeData.get(id) || _originalNodeById(id) || {};
        return {id: id, label: id || n.label, count: obj[id], entityType: n.entityType || ''};
      }).sort(function(x, y) { return y.count - x.count; });
    };
    var topA = sortContrib(contribByA).slice(0, 5);
    var topB = isSelf ? topA : sortContrib(contribByB).slice(0, 5);
    var stats = {
      catA: a, catB: b, isSelf: isSelf,
      totalCount: totalCount, totalWeight: totalWeight,
      avgWeight: totalCount > 0 ? (totalWeight / totalCount) : 0,
      distinctPairs: bridges.length,
      topRel: topRel,
      topBridges: topBridges,
      topContributorsA: topA,
      topContributorsB: topB,
    };
    _categoryEdgeStatsCache[key] = stats;
    return stats;
  }

  function showCategoryEdgeDetail(catA, catB) {
    if (!catA || !catB) return;
    var s = _categoryEdgeStats(catA, catB);
    var a = s.catA, b = s.catB;  // canonical (sorted)
    clearEdgeSelection();
    clearNodeSelection();
    clearPathsIfActive();
    window.lastDetailNodeId = null;
    window.lastDetailKind = 'cat-edge';
    window.lastDetailCatPair = [a, b];
    var rb = document.getElementById('reopen-detail-btn'); if (rb) rb.disabled = false;
    navPush({kind: 'cat-edge', payload: a + '||' + b});

    var aColor = GRAPH_META.superCategoryColors[a] || '#888';
    var bColor = GRAPH_META.superCategoryColors[b] || '#888';
    var aJs = String(a).replace(/'/g, "\\'");
    var bJs = String(b).replace(/'/g, "\\'");
    var pairKey = a + '||' + b;
    var pairKeyJs = pairKey.replace(/'/g, "\\'");

    var html = '';
    // Header — dual entity-badge with an arrow between them.
    html += '<div class="cat-edge-header">';
    html += '<span class="entity-badge" style="background:' + aColor + '33; color:' + aColor + '">' + esc(a) + '</span>';
    html += '<span class="cat-edge-arrow">' + (s.isSelf ? '&#x21BB;' : '&harr;') + '</span>';
    if (!s.isSelf) {
      html += '<span class="entity-badge" style="background:' + bColor + '33; color:' + bColor + '">' + esc(b) + '</span>';
    }
    html += '</div>';
    var titleText = s.isSelf ? (a + ' (internal)') : (a + ' ↔ ' + b);
    html += '<h3 class="detail-title">' + esc(titleText) + '</h3>';
    html += '<div class="detail-meta">' +
            s.totalCount.toLocaleString() + ' edges' +
            ' &bull; total weight ' + s.totalWeight.toFixed(0) +
            ' &bull; avg ' + s.avgWeight.toFixed(2);
    if (s.topRel.length) {
      html += ' &bull; ' + s.topRel.length + ' rel-type' + (s.topRel.length === 1 ? '' : 's');
    }
    html += '</div>';

    // Actions at the top of the panel — matches the standard.
    html += '<div class="detail-actions">';
    if (s.isSelf) {
      html += '<button class="action-btn" onclick="drillIntoCategory(\'' + aJs + '\')" title="Switch to Node view filtered to ' + esc(a) + '">Drill into ' + esc(a) + '</button>';
    } else {
      html += '<button class="action-btn" onclick="filterToTwoCategories(\'' + aJs + '\',\'' + bJs + '\')" title="Switch to Node view with both ' + esc(a) + ' and ' + esc(b) + ' visible (other categories hidden)">Filter to both</button>';
      html += '<button class="action-btn" onclick="drillIntoCategory(\'' + aJs + '\')" title="Switch to Node view filtered to ' + esc(a) + '">Drill into ' + esc(a) + '</button>';
      html += '<button class="action-btn" onclick="drillIntoCategory(\'' + bJs + '\')" title="Switch to Node view filtered to ' + esc(b) + '">Drill into ' + esc(b) + '</button>';
    }
    html += renderCopySplit('cat-edge', pairKey, 'copy-cat-edge-btn');
    html += '</div>';

    // 1. Relationship-type histogram.
    if (s.topRel.length) {
      html += '<div class="connections"><h4>Relationship types</h4>';
      var maxRel = s.topRel[0][1];
      s.topRel.forEach(function(kv) {
        var rel = kv[0], cnt = kv[1];
        var pct = maxRel > 0 ? (cnt / maxRel) * 100 : 0;
        var totPct = s.totalCount > 0 ? Math.round((cnt / s.totalCount) * 100) : 0;
        var rcColor = (GRAPH_META.relationshipColors || {})[rel] || '#666';
        var relJs = String(rel).replace(/'/g, "\\'");
        html += '<div class="cluster-bar-row" onclick="filterToRelType(\'' + relJs + '\')" title="Filter the canvas to ' + esc(rel) + ' edges (Node view)">' +
                '<span class="cluster-bar-label">' +
                '<span class="conn-dot" style="background:' + rcColor + '"></span>' + esc(rel) + '</span>' +
                '<span class="cluster-bar-track"><span class="cluster-bar-fill" style="width:' + pct.toFixed(1) +
                '%; background:' + rcColor + '"></span></span>' +
                '<span class="cluster-bar-count">' + cnt + ' (' + totPct + '%)</span>' +
                '</div>';
      });
      html += '</div>';
    }

    // 2. Top concrete bridges by weight.
    if (s.topBridges.length) {
      html += '<div class="connections"><h4>Top concrete bridges (by weight)</h4>';
      s.topBridges.forEach(function(br) {
        var rc = br.relCategory || 'related to';
        var rcColor = (GRAPH_META.relationshipColors || {})[rc] || '#666';
        html += '<div class="bridge-edge-row conn-item" data-edge-id="' + esc(br.id) + '"' +
                ' title="Drill into this concrete edge in Node view">' +
                '<span class="bridge-edge-endpoint" data-node-id="' + esc(br.from) + '" title="Drill into ' + esc(br.fromLabel) + '">' + esc(br.fromLabel) + '</span>' +
                ' <span class="bridge-edge-arrow">&harr;</span> ' +
                '<span class="bridge-edge-endpoint" data-node-id="' + esc(br.to) + '" title="Drill into ' + esc(br.toLabel) + '">' + esc(br.toLabel) + '</span>' +
                ' <span class="rel-chip" style="color:' + rcColor + ';border-color:' + rcColor + '44">' + esc(rc) + '</span>' +
                ' <span class="conn-edge-desc">w&nbsp;' + br.weight.toFixed(1) + '</span>' +
                '</div>';
      });
      html += '</div>';
    }

    // 3. Top contributors per side.
    var renderContribList = function(title, list, color) {
      if (!list.length) return '';
      var out = '<div class="connections"><h4>' + esc(title) + '</h4>';
      list.forEach(function(c) {
        var typeChip = c.entityType ? ' <span class="cat-hub-type">' + esc(c.entityType) + '</span>' : '';
        out += '<div class="conn-item" data-node-id="' + esc(c.id) + '"' +
               ' title="Drill into this entity">' +
               '<span class="conn-dot" style="background:' + color + '"></span>' +
               esc(c.label) + typeChip +
               ' <span class="conn-edge-desc">&mdash; ' + c.count + ' edge' + (c.count === 1 ? '' : 's') + '</span>' +
               '</div>';
      });
      out += '</div>';
      return out;
    };
    if (s.isSelf) {
      html += renderContribList('Top contributors', s.topContributorsA, aColor);
    } else {
      html += renderContribList('From ' + a, s.topContributorsA, aColor);
      html += renderContribList('From ' + b, s.topContributorsB, bColor);
    }

    document.getElementById('detail-content').innerHTML = html;
    document.getElementById('detail-panel').style.display = 'block';
  }
  window.showCategoryEdgeDetail = showCategoryEdgeDetail;

  // Set entity-type filter to exactly catA + catB and switch to Node view.
  function filterToTwoCategories(catA, catB) {
    document.querySelectorAll('.type-cb').forEach(function(cb) {
      cb.checked = (cb.dataset.cat === catA || cb.dataset.cat === catB);
    });
    if (viewMode !== 'node') switchView('node');
    else applyFilters();
  }
  window.filterToTwoCategories = filterToTwoCategories;

  // ===================== Detail panel: cluster summary =====================
  // A Louvain-community summary panel: composition, internal hubs, internal
  // rel-types, and bridge edges out of the cluster. Driven by the
  // `community` field already attached to every node.
  function showClusterDetail(communityId) {
    if (typeof communityId !== 'number' || communityId < 0) return;
    var sizes = (GRAPH_META.communitySizes || []);
    if (communityId >= sizes.length) return;
    clearEdgeSelection();
    clearNodeSelection();
    clearPathsIfActive();
    window.lastDetailNodeId = null;
    window.lastDetailKind = 'cluster';
    window.lastDetailClusterId = communityId;
    var rb = document.getElementById('reopen-detail-btn'); if (rb) rb.disabled = false;
    navPush({kind: 'cluster', payload: communityId});

    // Single pass over members to collect ids + entity-type histogram +
    // articulation count. originalNodes captures the fixed graph (a member
    // doesn't stop being a member because it's filtered out).
    var memberIds = new Set();
    var byCat = Object.create(null);
    var artCount = 0;
    originalNodes.forEach(function(n) {
      if (n.community !== communityId) return;
      memberIds.add(n.id);
      var cat = n.superCategory || 'Other';
      byCat[cat] = (byCat[cat] || 0) + 1;
      if (n.isArticulation) artCount++;
    });

    // Edges: split into internal (both endpoints in cluster) and bridge
    // (exactly one endpoint in). Internal edges feed the hub degree
    // (within-cluster degree, more meaningful than global degree for "who
    // matters here") + the rel-type histogram.
    var internalDeg = Object.create(null);
    memberIds.forEach(function(id) { internalDeg[id] = 0; });
    var byRel = Object.create(null);
    var bridgeEdgeCount = 0;
    var bridgeByCounter = Object.create(null);  // external nodeId -> bridge edge count
    originalEdges.forEach(function(e) {
      var fromIn = memberIds.has(e.from);
      var toIn = memberIds.has(e.to);
      if (fromIn && toIn) {
        internalDeg[e.from]++;
        internalDeg[e.to]++;
        var rc = e.relCategory || 'Other';
        byRel[rc] = (byRel[rc] || 0) + 1;
      } else if (fromIn || toIn) {
        bridgeEdgeCount++;
        var external = fromIn ? e.to : e.from;
        bridgeByCounter[external] = (bridgeByCounter[external] || 0) + 1;
      }
    });

    var hubs = Object.keys(internalDeg).map(function(id) {
      var n = nodeData.get(id) || {};
      return {id: id, label: id || n.label, deg: internalDeg[id], cat: n.superCategory || 'Other'};
    }).sort(function(a, b) { return b.deg - a.deg; }).slice(0, 10);

    var sortByCount = function(o) {
      return Object.keys(o).map(function(k) { return [k, o[k]]; })
        .sort(function(a, b) { return b[1] - a[1]; });
    };
    var topCats = sortByCount(byCat);
    var topRels = sortByCount(byRel).slice(0, 8);
    var topBridges = Object.keys(bridgeByCounter).map(function(id) {
      var n = nodeData.get(id) || {};
      return {id: id, label: id || n.label, count: bridgeByCounter[id], cat: n.superCategory || 'Other'};
    }).sort(function(a, b) { return b.count - a.count; }).slice(0, 5);

    // Communities are sorted by size descending, so rank = id + 1 — no
    // sort needed.
    var memberCount = memberIds.size;
    var rank = communityId + 1;
    var totalCommunities = sizes.length;
    var rankPct = totalCommunities > 0 ? (rank / totalCommunities) * 100 : 100;
    var rankSuffix = '';
    if (rankPct <= 1) rankSuffix = ' · top 1%';
    else if (rankPct <= 5) rankSuffix = ' · top 5%';
    else if (rankPct <= 10) rankSuffix = ' · top 10%';

    var html = '';
    html += '<div class="entity-badge" style="background: rgba(140,180,240,0.18); color: #cfe1ff">CLUSTER</div>';
    html += '<h3 class="detail-title" onclick="recenterOnCluster(' + communityId +
            ')" title="Click to fit the canvas around this cluster">Cluster #' + rank + '</h3>';
    html += '<div class="detail-meta">' + memberCount + ' members';
    html += ' <span class="rank-badge">#' + rank + ' of ' + totalCommunities.toLocaleString() + rankSuffix + '</span>';
    if (artCount > 0) {
      html += ' &bull; <span class="articulation-badge" title="Articulation points: removing any of these would disconnect part of the graph">' +
              artCount + ' cut node' + (artCount === 1 ? '' : 's') + '</span>';
    }
    html += '</div>';

    var isLocked = (communityFilter === communityId);
    html += '<div class="detail-actions">';
    html += '<button class="action-btn' + (isLocked ? ' active' : '') +
            '" onclick="filterToCommunity(' + communityId + ')" title="' +
            (isLocked ? 'Stop filtering the canvas to this cluster' : 'Hide every node not in this cluster') +
            '">' + (isLocked ? 'Unlock canvas' : 'Lock canvas to this cluster') + '</button>';
    html += '<button class="action-btn" onclick="recenterOnCluster(' + communityId +
            ')" title="Animate the canvas to fit the cluster\'s bounding box">Zoom to fit</button>';
    html += renderCopySplit('cluster', communityId, 'copy-cluster-btn-' + communityId);
    html += '</div>';

    // Composition: entity-type histogram. Reuses superCategory palette so
    // colors line up with the entity-type filter rows.
    if (topCats.length) {
      html += '<div class="connections"><h4>Composition</h4>';
      var maxCatCount = topCats[0][1];
      topCats.forEach(function(kv) {
        var cat = kv[0], cnt = kv[1];
        var color = (GRAPH_META.superCategoryColors || {})[cat] || '#888';
        var pct = maxCatCount > 0 ? (cnt / maxCatCount) * 100 : 0;
        var memPct = memberCount > 0 ? Math.round((cnt / memberCount) * 100) : 0;
        var catJs = String(cat).replace(/'/g, "\\'");
        html += '<div class="cluster-bar-row" onclick="drillToSuperCat(\'' + catJs +
                '\')" title="Filter the canvas to ' + esc(cat) + '">' +
                '<span class="cluster-bar-label">' +
                '<span class="conn-dot" style="background:' + color + '"></span>' + esc(cat) + '</span>' +
                '<span class="cluster-bar-track"><span class="cluster-bar-fill" style="width:' + pct.toFixed(1) +
                '%; background:' + color + '"></span></span>' +
                '<span class="cluster-bar-count">' + cnt + ' (' + memPct + '%)</span>' +
                '</div>';
      });
      html += '</div>';
    }

    // Top hubs by within-cluster degree.
    if (hubs.length && hubs[0].deg > 0) {
      html += '<div class="connections"><h4>Top members (within-cluster degree)</h4>';
      hubs.forEach(function(h) {
        if (h.deg === 0) return;
        var color = (GRAPH_META.superCategoryColors || {})[h.cat] || '#888';
        var nidJs = String(h.id).replace(/'/g, "\\'");
        html += '<div class="conn-item" data-node-id="' + esc(h.id) + '"' +
                ' onclick="showAndFocusNode(\'' + nidJs + '\')"' +
                ' title="Click to highlight + open this entity\'s detail">' +
                '<span class="conn-dot" style="background:' + color + '"></span>' +
                esc(h.label) +
                ' <span class="conn-edge-desc">&mdash; ' + h.deg + ' within-cluster</span>' +
                '</div>';
      });
      html += '</div>';
    }

    // Top relationship types among internal edges.
    if (topRels.length) {
      html += '<div class="connections"><h4>Top relationships (within cluster)</h4>';
      var maxRelCount = topRels[0][1];
      topRels.forEach(function(kv) {
        var rel = kv[0], cnt = kv[1];
        var color = (GRAPH_META.relationshipColors || {})[rel] || '#666';
        var pct = maxRelCount > 0 ? (cnt / maxRelCount) * 100 : 0;
        var relJs = String(rel).replace(/'/g, "\\'");
        html += '<div class="cluster-bar-row" onclick="filterToRelType(\'' + relJs +
                '\')" title="Filter to ' + esc(rel) + ' edges">' +
                '<span class="cluster-bar-label">' +
                '<span class="conn-dot" style="background:' + color + '"></span>' + esc(rel) + '</span>' +
                '<span class="cluster-bar-track"><span class="cluster-bar-fill" style="width:' + pct.toFixed(1) +
                '%; background:' + color + '"></span></span>' +
                '<span class="cluster-bar-count">' + cnt + '</span>' +
                '</div>';
      });
      html += '</div>';
    }

    // Bridge edges out of the cluster.
    if (bridgeEdgeCount > 0) {
      html += '<div class="connections"><h4>Bridges to other clusters &mdash; ' +
              bridgeEdgeCount.toLocaleString() + ' edges</h4>';
      topBridges.forEach(function(b) {
        var color = (GRAPH_META.superCategoryColors || {})[b.cat] || '#888';
        var nidJs = String(b.id).replace(/'/g, "\\'");
        html += '<div class="conn-item" data-node-id="' + esc(b.id) + '"' +
                ' onclick="showAndFocusNode(\'' + nidJs + '\')"' +
                ' title="External node connected by ' + b.count + ' bridge edge' + (b.count === 1 ? '' : 's') + '">' +
                '<span class="conn-dot" style="background:' + color + '"></span>' +
                esc(b.label) +
                ' <span class="conn-edge-desc">&mdash; ' + b.count + ' edge' + (b.count === 1 ? '' : 's') + '</span>' +
                '</div>';
      });
      html += '</div>';
    }

    document.getElementById('detail-content').innerHTML = html;
    document.getElementById('detail-panel').style.display = 'block';
  }
  window.showClusterDetail = showClusterDetail;

  function recenterOnCluster(communityId) {
    var ids = [];
    originalNodes.forEach(function(n) { if (n.community === communityId) ids.push(n.id); });
    if (ids.length) network.fit({nodes: ids, animation: {duration: 400, easingFunction: 'easeInOutQuad'}});
  }
  window.recenterOnCluster = recenterOnCluster;

  // ===================== Edge selection highlight =====================
  // Visual feedback for an edge currently shown in the detail panel: bump
  // its width + opacity so it pops on the canvas. Backups let us restore
  // the previous look on deselect / new selection / panel close. We don't
  // rely on vis.js's built-in selection styling because the Python-side
  // edge color object uses `highlight = base color`, so vis.js's selected
  // rendering is visually identical to non-selected.
  var selectedEdgeBackup = {};  // id -> {width, color}

  function applyEdgeSelection(edgeIds) {
    clearEdgeSelection();
    edgeIds.forEach(function(eid) {
      var e = edgeData.get(eid);
      if (!e) return;
      selectedEdgeBackup[eid] = {width: e.width, color: e.color};
      var origColor = e.color || {};
      edgeData.update({
        id: eid,
        width: (e.width || 1) * 2.5,
        color: Object.assign({}, origColor, {opacity: 1.0}),
      });
    });
  }

  function clearEdgeSelection() {
    var ids = Object.keys(selectedEdgeBackup);
    if (!ids.length) return;
    var updates = ids.map(function(eid) {
      var saved = selectedEdgeBackup[eid];
      return {id: eid, width: saved.width, color: saved.color};
    });
    edgeData.update(updates);
    selectedEdgeBackup = {};
  }

  // ===================== Node selection halo =====================
  // Visual feedback for the node currently shown in the detail panel:
  // bump borderWidth + override color.border to white so the node is
  // findable in a dense graph (the user's primary complaint was "I
  // selected a node and can't see which one in the canvas"). Snapshot
  // + restore mirrors applyEdgeSelection. Doesn't rely on vis.js's
  // built-in selection styling because it's too subtle on dark themes
  // with already-saturated category colors.
  var selectedNodeBackup = {};  // id -> {borderWidth, color}

  function applyNodeSelection(nodeIds) {
    clearNodeSelection();
    nodeIds.forEach(function(nid) {
      var n = nodeData.get(nid);
      if (!n) return;
      var origBorderWidth = n.borderWidth;
      var origColor = n.color || {};
      var origSize = n.size;
      var origShadow = n.shadow;
      selectedNodeBackup[nid] = {
        borderWidth: origBorderWidth,
        color: origColor,
        size: origSize,
        shadow: origShadow,
      };
      var bumpedBorder = (origBorderWidth || 1) + 6;
      // Three stacked treatments make the selection unmistakable even on
      // small / dense graphs (where a thin white border alone disappeared
      // visually): (1) borderWidth +6 (white), (2) size × 1.6, (3) a
      // white drop-shadow as a glow halo. Object.assign preserves
      // origColor's nested .highlight / .hover sub-objects.
      nodeData.update({
        id: nid,
        borderWidth: bumpedBorder,
        borderWidthSelected: bumpedBorder,
        color: Object.assign({}, origColor, {border: '#ffffff'}),
        size: (origSize || 15) * 1.6,
        shadow: {enabled: true, color: '#ffffff', size: 25, x: 0, y: 0},
      });
    });
  }

  function clearNodeSelection() {
    var ids = Object.keys(selectedNodeBackup);
    if (!ids.length) return;
    var updates = ids.map(function(nid) {
      var saved = selectedNodeBackup[nid];
      // Explicit non-undefined reset values: if the original node had no
      // `shadow` (typical — the build doesn't set one), saving undefined
      // and writing it back leaves the apply-time bumped shadow in place
      // (vis.js drops undefined keys during the update merge). Same risk
      // for borderWidth. Fallbacks restore vis.js defaults.
      return {
        id: nid,
        borderWidth: saved.borderWidth != null ? saved.borderWidth : 1,
        borderWidthSelected: saved.borderWidth != null ? saved.borderWidth : 1,
        color: saved.color,
        size: saved.size,
        shadow: saved.shadow != null ? saved.shadow : {enabled: false},
      };
    });
    nodeData.update(updates);
    selectedNodeBackup = {};
  }

  // Single helper invoked by the panel's close button (inline onclick) and
  // by the empty-canvas click branch — both paths must restore any active
  // edge / node highlight + path-mode styling before hiding the panel.
  function closeDetailPanel() {
    clearEdgeSelection();
    clearNodeSelection();
    clearPathsIfActive();
    document.getElementById('detail-panel').style.display = 'none';
    document.getElementById('toolbar').style.display = 'flex';
  }
  window.closeDetailPanel = closeDetailPanel;

  // Path-mode teardown: defer to applyFilters() whose end-of-pass branch
  // is the canonical path-clear logic (restores edge color/width from
  // originalEdgeById, resets node borderWidth, clears pathsHighlighted +
  // _pathNodeIds). Called from closeDetailPanel and from every show*Detail
  // entry point so any way of dismissing the path panel — X button, click
  // empty canvas, click a different node, click a different edge,
  // switchView, drillToSuperCat — exits path mode. Otherwise the path
  // styling sticks until the user finds the (now-removed) Clear paths
  // button.
  function clearPathsIfActive() {
    if (pathsHighlighted) applyFilters();
  }

  // ===================== Detail panel: show edge(s) =====================
  // vis.js's click event returns an *array* of edge IDs (any edges that
  // geometrically intersect the click point). Today's corpus is 1:1
  // (one edge per node-pair) so .length is almost always 1, but rendering
  // a list when N>1 keeps the UI honest at intersection points and for
  // future corpora that produce parallel edges.
  function showEdgeDetail(edgeIds) {
    if (!edgeIds || !edgeIds.length) return;
    var realEdges = edgeIds
      .map(function(eid) { return edgeData.get(eid); })
      .filter(function(e) {
        // Skip aggregate Category-view super-edges — they have no
        // LLM-extracted description payload.
        return e && !String(e.from || '').startsWith('__');
      });
    if (!realEdges.length) return;

    window.lastDetailNodeId = null;
    window.lastDetailEdgeIds = edgeIds.slice();
    window.lastDetailKind = 'edge';
    var rb = document.getElementById('reopen-detail-btn');
    if (rb) rb.disabled = false;

    // Push to nav stack and highlight the edge(s) on the canvas.
    var realIds = realEdges.map(function(e) { return e.id; });
    navPush({kind: 'edge', payload: realIds});
    clearNodeSelection();
    clearPathsIfActive();
    applyEdgeSelection(realIds);
    network.setSelection({nodes: [], edges: realIds}, {unselectAll: true});

    var multi = realEdges.length > 1;
    var firstEdgeIdJs = String(realEdges[0].id).replace(/'/g, "\\'");
    var html = multi
      ? ('<h3 class="detail-title" onclick="recenterOnEdge(\'' + firstEdgeIdJs +
         '\')" title="Click to recenter the canvas on these edges (first endpoint pair)">' +
         realEdges.length + ' edges at this point</h3>')
      : '';

    realEdges.forEach(function(e) {
      var fromNode = nodeData.get(e.from);
      var toNode = nodeData.get(e.to);
      var fromCat = fromNode ? (fromNode.superCategory || 'Other') : 'Other';
      var toCat = toNode ? (toNode.superCategory || 'Other') : 'Other';
      var fromColor = GRAPH_META.superCategoryColors[fromCat] || '#888';
      var toColor = GRAPH_META.superCategoryColors[toCat] || '#888';
      var relCat = e.relCategory || 'Other';
      var relColor = GRAPH_META.relationshipColors[relCat] || '#666';
      var weight = e.edgeWeight || 1;
      var fromLabel = e.from;
      var toLabel = e.to;
      var relCatJs = String(relCat).replace(/'/g, "\\'");

      html += '<div class="edge-detail-card">';
      if (!multi) {
        var edgeIdJs = String(e.id).replace(/'/g, "\\'");
        html += '<h3 class="detail-title" onclick="recenterOnEdge(\'' + edgeIdJs +
                '\')" title="Click to recenter the canvas on this edge">Edge</h3>';
      }
      // Endpoint badges reuse the existing .conn-item[data-node-id] hook —
      // the delegated click handler on #detail-content will navigate into
      // the node's detail when clicked.
      html += '<div class="edge-endpoints">';
      html += '<span class="conn-item edge-endpoint" data-node-id="' + esc(e.from) +
              '" style="color:' + fromColor + '" title="Click to inspect this entity">' + esc(fromLabel) + '</span>';
      html += '<span class="edge-arrow"> &rarr; </span>';
      html += '<span class="conn-item edge-endpoint" data-node-id="' + esc(e.to) +
              '" style="color:' + toColor + '" title="Click to inspect this entity">' + esc(toLabel) + '</span>';
      html += '</div>';
      html += '<div class="detail-meta">';
      html += '<span class="edge-rel-chip" style="background:' + relColor +
              '22;color:' + relColor + ';border-color:' + relColor + '">' +
              esc(relCat) + '</span>';
      html += ' &bull; weight ' + weight.toFixed(1);
      html += '</div>';

      // Multi-phrase descriptions are joined with \x1f; split per phrase
      // and render as a bulleted list. Single-phrase edges show one <li>.
      var phrases = String(e.fullDescription || '')
        .split('\x1f')
        .map(function(s) { return s.trim(); })
        .filter(function(s) { return s; });
      if (phrases.length) {
        html += '<ul class="edge-phrase-list">';
        phrases.forEach(function(p) { html += '<li>' + esc(p) + '</li>'; });
        html += '</ul>';
      }

      var copyBtnId = 'copy-edge-btn-' + e.id;

      html += '<div class="detail-actions">';
      html += '<button class="action-btn" onclick="filterToRelType(\'' + relCatJs +
              '\')" title="Hide all edges except those of this relationship type">Filter to ' + esc(relCat) + '</button>';
      html += _renderStatsQuerySplit({
        stats: _buildStatsCommandForEdge(e),
        query: _buildQueryCommandForEdge(e),
      }, 'sq-edge-' + String(e.id).replace(/[^a-zA-Z0-9-]/g, '_'));
      html += renderCopySplit('edge', e.id, copyBtnId);
      html += '</div>';

      html += _renderSourceTopics(e.topicIds);

      html += '</div>';
    });

    document.getElementById('detail-content').innerHTML = html;
    document.getElementById('detail-panel').style.display = 'block';
  }
  window.showEdgeDetail = showEdgeDetail;

  function filterToRelType(relCat) {
    document.querySelectorAll('.rel-cb').forEach(function(cb) {
      cb.checked = (cb.dataset.rel === relCat);
    });
    // The cat-edge panel's rel-type histogram calls this from Category
    // view; auto-switch so applyFilters has real edges to scope.
    // switchView('node') runs applyFilters internally inside its
    // deferred block, so we don't need to call it again here.
    if (viewMode !== 'node') {
      switchView('node');
    } else {
      applyFilters();
    }
  }
  window.filterToRelType = filterToRelType;

  // Lock the canvas to a single Louvain community. Stays active across
  // other filter changes until cleared. Clicking the same cluster's badge
  // (or the breadcrumb chip) toggles the lock off; passing null also clears.
  // Re-renders the open node detail panel so the badge active state and the
  // breadcrumb chip stay in sync with the new lock state.
  function filterToCommunity(communityId) {
    var isNum = (typeof communityId === 'number');
    if (isNum && communityFilter === communityId) {
      communityFilter = null;
    } else {
      communityFilter = isNum ? communityId : null;
    }
    var refresh = function() {
      // applyFilters already ran inside switchView's deferred block
      // when we switched. Skip the redundant call in that case.
      if (window.lastDetailKind === 'node' && window.lastDetailNodeId) {
        showNodeDetail(window.lastDetailNodeId);
      } else if (window.lastDetailKind === 'cluster' && typeof window.lastDetailClusterId === 'number') {
        showClusterDetail(window.lastDetailClusterId);
      }
      network.fit({animation: {duration: 400}});
    };
    if (viewMode === 'category') {
      switchView('node');
      runAfterPaint(refresh);
    } else {
      applyFilters();
      refresh();
    }
  }
  window.filterToCommunity = filterToCommunity;

  // From Category View, drill straight into Node View filtered to this
  // super-category. (Pre-scope the type-cb so the breadcrumb + filter
  // panel reflect the drill target before switchView calls applyFilters.)
  function drillIntoCategory(cat) {
    document.querySelectorAll('.type-cb').forEach(function(cb) { cb.checked = (cb.dataset.cat === cat); });
    degSlider.value = 0; degVal.textContent = '0';
    switchView('node');
  }
  window.drillIntoCategory = drillIntoCategory;

  // From Entity Type View, drill into filtered Node View scoped to (cat,
  // entityType). Category checkbox is already set by drillIntoCategory; we
  // reuse the search box to narrow by entity_type, matching the existing
  // drillToEntityType behaviour.
  function drillIntoEntityType(cat, etype) {
    document.querySelectorAll('.type-cb').forEach(function(cb) { cb.checked = (cb.dataset.cat === cat); });
    degSlider.value = 0; degVal.textContent = '0';
    document.getElementById('search-input').value = '';
    switchView('node');
    // Narrow further: use the same pattern as drillToEntityType — search by
    // the raw entity type name so the node-view filter hits only those.
    // This works because `.fullDescription` and `.entityType` are searched
    // via the label-only match by default; we piggyback on label search to
    // keep the filter logic untouched. If strict entity-type-only scoping
    // is needed later, extend applyFilters to honour an entityType filter.
    applyFilters();
    network.fit({animation: {duration: 400}});
  }
  window.drillIntoEntityType = drillIntoEntityType;

  // ===================== Click handler (main) =====================
  // Navigate to connected node from detail panel
  document.getElementById('detail-content').addEventListener('click', function(evt) {
    // Stats / Query split-button primary: copy the prefilled command
    // stashed on data-qs-cmd. Match before the data-node-id branch so
    // the button (which sits in .detail-actions, no data-node-id)
    // doesn't accidentally fall through. The ▾ toggle has its own
    // inline onclick="openQsMenu(this)" — handled separately, doesn't
    // need a route here.
    var qsPrimary = evt.target.closest('.statsq-primary');
    if (qsPrimary) {
      var qsCmd = qsPrimary.dataset.qsCmd;
      if (qsCmd) {
        copyToClipboard(qsCmd, qsPrimary.id);
        evt.stopPropagation();
        return;
      }
    }
    // Entity rows / endpoint spans (data-node-id). Match BEFORE the
    // edge-row branch so a click on an endpoint label inside an edge
    // row navigates to that node, not to the edge.
    var nodeSpan = evt.target.closest('[data-node-id]');
    if (nodeSpan) {
      var nid = nodeSpan.dataset.nodeId;
      if (nid) {
        if (viewMode === 'category') {
          var t = nodeData.get(nid);
          if (t && t.isSuperNode) {
            network.selectNodes([nid]);
            network.focus(nid, {scale: 1.2, animation: {duration: 400, easingFunction: 'easeInOutQuad'}});
            showCategoryDetail(nid);
          } else {
            // Real-entity row inside a Category-view panel — drill to
            // Node view + open the entity's detail.
            drillToNode(nid);
          }
        } else {
          network.selectNodes([nid]);
          network.focus(nid, {scale: 1.2, animation: {duration: 400, easingFunction: 'easeInOutQuad'}});
          showNodeDetail(nid);
        }
        evt.stopPropagation();
        return;
      }
    }
    // Bridge-edge row (data-edge-id) → switch to Node view + show
    // edge detail. Used by the Category-edge panel's "Top concrete
    // bridges" rows. switchView is async (runAfterPaint), so queue
    // showEdgeDetail to run after the DataSet hydrates — otherwise it
    // fires against an empty edgeData and silently no-ops.
    var edgeRow = evt.target.closest('[data-edge-id]');
    if (edgeRow) {
      var edgeId = edgeRow.dataset.edgeId;
      if (edgeId) {
        if (viewMode !== 'node') {
          switchView('node');
          runAfterPaint(function() { showEdgeDetail([edgeId]); });
        } else {
          showEdgeDetail([edgeId]);
        }
      }
    }
  });

  network.on('click', function(params) {
    if (params.nodes.length === 0) {
      // Empty-node + non-empty-edge in `node` view ⇒ edge panel.
      // Aggregate super-edge in Category view ⇒ category-edge panel.
      if (params.edges.length > 0 && viewMode === 'node') {
        showEdgeDetail(params.edges);
        return;
      }
      if (params.edges.length > 0 && viewMode === 'category') {
        var aggE = edgeData.get(params.edges[0]);
        if (aggE && typeof aggE.from === 'string' && typeof aggE.to === 'string'
            && aggE.from.indexOf('__cat__') === 0 && aggE.to.indexOf('__cat__') === 0) {
          showCategoryEdgeDetail(aggE.from.substring(7), aggE.to.substring(7));
          return;
        }
      }
      closeDetailPanel();
      return;
    }
    var nodeId = params.nodes[0];

    // Pathfinding: second click selects destination. Top-3 shortest
    // simple paths via Yen's algorithm — usually 1 or 2 will share most
    // of their length with the canonical shortest, the others reveal
    // genuinely different routes through the graph.
    if (pathfindState) {
      var fromId = pathfindState.fromId;
      cancelPathfind();
      var paths = findKShortestPaths(fromId, nodeId, 3);
      if (paths.length > 0) highlightPaths(paths);
      else { alert('No path found between these nodes.'); applyFilters(); }
      return;
    }

    // Category super-node click opens the rich detail panel
    // (composition by entity type, top hubs, inter-category bridges).
    // Drilling into Node view is now an explicit "Drill into X" button
    // inside that panel, so users see what's in a category before
    // committing to a 16k-node drill. EntityType-mode super-nodes
    // (orphaned view) keep the legacy click-to-drill since their panel
    // wasn't enriched.
    var clicked = nodeData.get(nodeId);
    if (viewMode === 'category' && clicked && clicked.isSuperNode && !clicked.isEntityTypeNode) {
      showCategoryDetail(nodeId);
      return;
    }
    if (viewMode === 'entityType' && clicked && clicked.isEntityTypeNode) {
      drillIntoEntityType(clicked.superCategory, clicked.entityType);
      return;
    }

    if (viewMode === 'category') showCategoryDetail(nodeId);
    else showNodeDetail(nodeId);
  });

  // ===================== Drill-down from detail panel =====================
  function drillToSuperCat(cat) {
    if (viewMode === 'category') switchView('node');
    document.querySelectorAll('.type-cb').forEach(function(cb) { cb.checked = (cb.dataset.cat === cat); });
    degSlider.value = 0; degVal.textContent = '0';
    applyFilters();
    network.fit({animation: {duration: 400}});
    closeDetailPanel();
  }
  window.drillToSuperCat = drillToSuperCat;

  function drillToEntityType(rawType) {
    // Find which super-category this raw type belongs to, filter to that, then search for the raw type
    if (viewMode === 'category') switchView('node');
    // Enable all types, set degree to 0, and search for the raw type
    document.querySelectorAll('.type-cb').forEach(function(cb) { cb.checked = true; });
    degSlider.value = 0; degVal.textContent = '0';
    document.getElementById('search-input').value = '';
    // Find nodes with this exact entityType
    var matchCat = null;
    originalNodes.forEach(function(n) {
      if ((n.entityType || '').toLowerCase() === rawType.toLowerCase()) matchCat = n.superCategory;
    });
    if (matchCat) {
      document.querySelectorAll('.type-cb').forEach(function(cb) { cb.checked = (cb.dataset.cat === matchCat); });
    }
    applyFilters();
    network.fit({animation: {duration: 400}});
    closeDetailPanel();
  }
  window.drillToEntityType = drillToEntityType;

  // ===================== Table View =====================
  var tableBuilt = false;
  var tableSortCol = 'degree';
  var tableSortAsc = false;
  var tableFilter = '';

  function buildTable() {
    if (tableBuilt) return;
    tableBuilt = true;
  }

  function renderTable() {
    var tbody = document.getElementById('table-body');
    var q = tableFilter.toLowerCase();
    var rows = originalNodes.filter(function(n) {
      if (!q) return true;
      return (n.id || '').toLowerCase().indexOf(q) >= 0 ||
             (n.label || '').toLowerCase().indexOf(q) >= 0 ||
             (n.superCategory || '').toLowerCase().indexOf(q) >= 0 ||
             (n.entityType || '').toLowerCase().indexOf(q) >= 0 ||
             (n.fullDescription || '').toLowerCase().indexOf(q) >= 0;
    });
    // Sort
    rows.sort(function(a, b) {
      var av = a[tableSortCol], bv = b[tableSortCol];
      if (tableSortCol === 'degree') { av = av || 0; bv = bv || 0; }
      else if (tableSortCol === 'desc') { av = (a.fullDescription || ''); bv = (b.fullDescription || ''); }
      else { av = (av || '').toLowerCase(); bv = (bv || '').toLowerCase(); }
      if (av < bv) return tableSortAsc ? -1 : 1;
      if (av > bv) return tableSortAsc ? 1 : -1;
      return 0;
    });
    // Update count
    document.getElementById('table-count').textContent = rows.length + ' of ' + originalNodes.length + ' entities';
    // Update sort arrows
    document.querySelectorAll('#data-table th').forEach(function(th) {
      var arrow = th.querySelector('.sort-arrow');
      if (th.dataset.col === tableSortCol) {
        arrow.textContent = tableSortAsc ? '\u25B2' : '\u25BC';
      } else {
        arrow.textContent = '';
      }
    });
    // Build set of currently hidden node IDs
    var hiddenIds = new Set();
    if (viewMode === 'node') {
      nodeData.get().forEach(function(nd) { if (nd.hidden) hiddenIds.add(nd.id); });
    }
    // Render rows (virtualize: max 500 visible at once for perf)
    var maxRows = 500;
    var html = '';
    var limit = Math.min(rows.length, maxRows);
    for (var i = 0; i < limit; i++) {
      var n = rows[i];
      var cat = n.superCategory || 'Other';
      var color = GRAPH_META.superCategoryColors[cat] || '#888';
      var desc = (n.fullDescription || '').replace(/\n/g, ' ');
      if (desc.length > 120) desc = desc.substring(0, 120) + '...';
      var rowCls = hiddenIds.has(n.id) ? ' class="hidden-node"' : '';
      html += '<tr data-node-id="' + esc(n.id) + '"' + rowCls + '>';
      html += '<td><strong>' + esc(n.id || n.label) + '</strong></td>';
      html += '<td><span class="cat-dot" style="background:' + color + '"></span>' + esc(cat) + '</td>';
      html += '<td>' + esc(n.entityType || '') + '</td>';
      html += '<td>' + (n.degree || 0) + '</td>';
      html += '<td>' + esc(desc) + '</td>';
      html += '</tr>';
    }
    if (rows.length > maxRows) {
      html += '<tr><td colspan="5" style="color:#888;text-align:center;padding:12px">Showing ' + maxRows + ' of ' + rows.length + ' — use search to narrow down</td></tr>';
    }
    tbody.innerHTML = html;
  }

  function openTable(prefilter) {
    buildTable();
    if (prefilter !== undefined) {
      tableFilter = prefilter;
      document.getElementById('table-search').value = prefilter;
    }
    renderTable();
    document.getElementById('table-view').classList.add('active');
  }
  window.openTable = openTable;

  function closeTable() {
    document.getElementById('table-view').classList.remove('active');
  }

  document.getElementById('table-toggle').addEventListener('click', function() { openTable(''); });
  document.getElementById('table-close').addEventListener('click', closeTable);
  document.getElementById('table-search').addEventListener('input', function() {
    tableFilter = this.value;
    debounce(renderTable, 150)();
  });
  // Sort on header click
  document.querySelectorAll('#data-table th').forEach(function(th) {
    th.addEventListener('click', function() {
      var col = this.dataset.col;
      if (tableSortCol === col) tableSortAsc = !tableSortAsc;
      else { tableSortCol = col; tableSortAsc = (col !== 'degree'); }
      renderTable();
    });
  });
  // Click row to navigate to node on graph
  document.getElementById('table-body').addEventListener('click', function(evt) {
    var tr = evt.target.closest('tr[data-node-id]');
    if (!tr) return;
    var nid = tr.dataset.nodeId;
    closeTable();
    // Ensure node is visible
    var n = nodeData.get(nid);
    if (n && n.hidden) {
      // Temporarily show it
      nodeData.update([{id: nid, hidden: false, opacity: 1}]);
    }
    network.selectNodes([nid]);
    network.focus(nid, {scale: 1.5, animation: {duration: 500, easingFunction: 'easeInOutQuad'}});
    showNodeDetail(nid);
  });
  // Escape key closes table
  document.addEventListener('keydown', function(evt) {
    if (evt.key === 'Escape') {
      if (document.getElementById('conn-table-view').classList.contains('active')) closeConnTable();
      else if (document.getElementById('table-view').classList.contains('active')) closeTable();
    }
  });

  // ===================== Connections Table =====================
  var connTableData = [];     // array of {id, label, superCategory, entityType, degree, relCat, edgeDesc, hidden}
  var connTableSortCol = 'degree';
  var connTableSortAsc = false;
  var connTableFilter = '';
  var connTableSourceId = null;

  function buildConnData(nodeId) {
    connTableSourceId = nodeId;
    connTableData = [];
    var connected = network.getConnectedNodes(nodeId);
    var connEdges = network.getConnectedEdges(nodeId);
    connected.forEach(function(cid) {
      var cn = nodeData.get(cid);
      var edgeDesc = '', relCat = '';
      for (var i = 0; i < connEdges.length; i++) {
        var e = edgeData.get(connEdges[i]);
        if (e && ((e.from === nodeId && e.to === cid) || (e.from === cid && e.to === nodeId))) {
          if (e.title) edgeDesc = e.title.split('|')[0].trim();
          relCat = e.relCategory || '';
          break;
        }
      }
      connTableData.push({
        id: cid,
        label: cn ? (cid || cn.label) : cid,
        superCategory: cn ? (cn.superCategory || 'Other') : 'Other',
        entityType: cn ? (cn.entityType || '') : '',
        degree: cn ? (cn.degree || 0) : 0,
        relCat: relCat,
        edgeDesc: edgeDesc,
        hidden: cn && cn.hidden,
      });
    });
  }

  function renderConnTable() {
    var tbody = document.getElementById('conn-table-body');
    var q = connTableFilter.toLowerCase();
    var rows = connTableData.filter(function(r) {
      if (!q) return true;
      return (r.label || '').toLowerCase().indexOf(q) >= 0 ||
             (r.superCategory || '').toLowerCase().indexOf(q) >= 0 ||
             (r.entityType || '').toLowerCase().indexOf(q) >= 0 ||
             (r.relCat || '').toLowerCase().indexOf(q) >= 0 ||
             (r.edgeDesc || '').toLowerCase().indexOf(q) >= 0;
    });
    rows.sort(function(a, b) {
      var av = a[connTableSortCol], bv = b[connTableSortCol];
      if (connTableSortCol === 'degree') { av = av || 0; bv = bv || 0; }
      else { av = (av || '').toLowerCase(); bv = (bv || '').toLowerCase(); }
      if (av < bv) return connTableSortAsc ? -1 : 1;
      if (av > bv) return connTableSortAsc ? 1 : -1;
      return 0;
    });
    document.getElementById('conn-table-count').textContent = rows.length + ' of ' + connTableData.length + ' connections';
    document.querySelectorAll('#conn-data-table th').forEach(function(th) {
      var arrow = th.querySelector('.sort-arrow');
      if (th.dataset.col === connTableSortCol) arrow.textContent = connTableSortAsc ? '\u25B2' : '\u25BC';
      else arrow.textContent = '';
    });
    var html = '';
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var cat = r.superCategory;
      var color = GRAPH_META.superCategoryColors[cat] || '#888';
      var relColor = GRAPH_META.relationshipColors[r.relCat] || '#666';
      var desc = (r.edgeDesc || '');
      if (desc.length > 100) desc = desc.substring(0, 100) + '...';
      var rowCls = r.hidden ? ' class="hidden-node"' : '';
      html += '<tr data-node-id="' + esc(r.id) + '"' + rowCls + '>';
      html += '<td><strong>' + esc(r.label) + '</strong></td>';
      html += '<td><span class="cat-dot" style="background:' + color + '"></span>' + esc(cat) + '</td>';
      html += '<td>' + esc(r.entityType) + '</td>';
      html += '<td><span class="cat-dot" style="background:' + relColor + '"></span>' + esc(r.relCat || 'Other') + '</td>';
      html += '<td>' + r.degree + '</td>';
      html += '<td>' + esc(desc) + '</td>';
      html += '</tr>';
    }
    tbody.innerHTML = html;
  }

  function openConnTable(nodeId) {
    var n = nodeData.get(nodeId);
    var label = n ? (n.label || nodeId) : nodeId;
    document.getElementById('conn-table-title').textContent = 'Connections of ' + label;
    connTableFilter = '';
    document.getElementById('conn-table-search').value = '';
    connTableSortCol = 'degree';
    connTableSortAsc = false;
    buildConnData(nodeId);
    renderConnTable();
    document.getElementById('conn-table-view').classList.add('active');
  }
  window.openConnTable = openConnTable;

  function closeConnTable() {
    document.getElementById('conn-table-view').classList.remove('active');
  }

  document.getElementById('conn-table-close').addEventListener('click', closeConnTable);
  document.getElementById('conn-table-search').addEventListener('input', function() {
    connTableFilter = this.value;
    debounce(renderConnTable, 150)();
  });
  document.querySelectorAll('#conn-data-table th').forEach(function(th) {
    th.addEventListener('click', function() {
      var col = this.dataset.col;
      if (connTableSortCol === col) connTableSortAsc = !connTableSortAsc;
      else { connTableSortCol = col; connTableSortAsc = (col !== 'degree'); }
      renderConnTable();
    });
  });
  document.getElementById('conn-table-body').addEventListener('click', function(evt) {
    var tr = evt.target.closest('tr[data-node-id]');
    if (!tr) return;
    var nid = tr.dataset.nodeId;
    closeConnTable();
    var n = nodeData.get(nid);
    if (n && n.hidden) nodeData.update([{id: nid, hidden: false, opacity: 1}]);
    network.selectNodes([nid]);
    network.focus(nid, {scale: 1.5, animation: {duration: 500, easingFunction: 'easeInOutQuad'}});
    showNodeDetail(nid);
  });

  // ===================== Unselect =====================
  document.getElementById('unselect-btn').addEventListener('click', function() {
    network.unselectAll();
    closeDetailPanel();
    applyFilters();
  });

  // ===================== Reopen last details =====================
  document.getElementById('reopen-detail-btn').addEventListener('click', function() {
    if (window.lastDetailKind === 'edge' && window.lastDetailEdgeIds) {
      showEdgeDetail(window.lastDetailEdgeIds);
      return;
    }
    if (window.lastDetailKind === 'cluster' && typeof window.lastDetailClusterId === 'number') {
      showClusterDetail(window.lastDetailClusterId);
      return;
    }
    if (window.lastDetailKind === 'cat-edge' && window.lastDetailCatPair) {
      showCategoryEdgeDetail(window.lastDetailCatPair[0], window.lastDetailCatPair[1]);
      return;
    }
    if (!window.lastDetailNodeId) return;
    if (window.lastDetailKind === 'category') {
      showCategoryDetail(window.lastDetailNodeId);
    } else {
      showNodeDetail(window.lastDetailNodeId);
    }
  });

  // ===================== Init =====================
  // Default-open in Node View — full graph, all filters wide. The
  // initial nodeData.add at the top of this handler already populated
  // the network with the real graph; we just need to apply filters,
  // settle the camera (synchronous fit so getViewPosition / getScale
  // are correct on the next line), then run the viewport-aware label
  // LOD. Calling switchView('node') here would re-clear and re-add
  // the data, which on first paint races with the network's internal
  // initialization — see the toggle path for the fully-warmed flow.
  // Prior commit 74f7687 had switched the landing to Category view
  // with switchView('category'); reverting to Node-on-start matches
  // the older pattern.
  applyFilters();
  network.fit({animation: false});
  applyLabelMode();
  renderBreadcrumb();
  syncViewToggleGroup();
  // Initial paint complete — drop the boot-time loading overlay.
  hideLoading();
});
