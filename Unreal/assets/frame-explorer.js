/* Frame explorer. Plain browser JavaScript; no CDN or framework.
 *
 * Read this file with the package contract in README.md:
 *   index.json, resources.json, passes.json, coverage.json, schema.json,
 *   structures.json, capture.json, records/{resource,pass}/<id>.json
 *   chunks/<raw-sha256>.gz
 *
 * Raw access is lazy. A numeric probe fetches only the chunks covering the
 * requested bytes; a preview fetches only its declared bounded row/element
 * window and states that window in the UI.
 */
(function () {
  'use strict';

  var Codec = (typeof window !== 'undefined' && window.FrameCodec)
    || (typeof module === 'object' && module.exports ? require('./frame-codec.js') : null);
  var MAX_PREVIEW_BYTES = 2 * 1024 * 1024;
  var MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024;
  var MAX_PARAMETER_READ = 8 * 1024 * 1024;
  var LIST_PAGE = 100;
  var GAP_PAGE = 200;

  var state = {
    packageBase: null,
    index: null,
    resources: null,
    passes: null,
    coverage: null,
    schema: null,
    capture: null,
    structures: null,
    nativeInputs: null,
    sourceRefMap: new Map(),
    resourceById: new Map(),
    passById: new Map(),
    recordCache: new Map(),
    search: '',
    kind: 'all',
    status: 'all',
    passLimit: LIST_PAGE,
    resourceLimit: LIST_PAGE,
    gapLimit: GAP_PAGE,
    gapSearch: '',
    gapSeverity: 'all',
    activeTab: 'explore',
    selected: null,
    detailToken: 0,
    schemaRendered: false,
    coverageRendered: false,
    captureRendered: false,
    theme: null
  };

  function $(id) { return document.getElementById(id); }

  function h(tag, attrs) {
    var node = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key === 'on') Object.keys(value).forEach(function (evt) { node.addEventListener(evt, value[evt]); });
      else if (key === 'colspan') node.colSpan = value;
      else if (key === 'rowspan') node.rowSpan = value;
      else if (key === 'for') node.htmlFor = value;
      else if (key in node) {
        try { node[key] = value; } catch (e) { node.setAttribute(key, String(value)); }
      } else node.setAttribute(key, String(value));
    });
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  function append(parent, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(parent, c); }); return; }
    if (child instanceof Node) parent.appendChild(child);
    else parent.appendChild(document.createTextNode(String(child)));
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function text(value) {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  }

  function fmtBytes(value) {
    var n = Number(value);
    if (!Number.isFinite(n)) return '—';
    if (n < 1024) return n + ' B';
    var units = ['KiB', 'MiB', 'GiB', 'TiB'];
    var i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2) + ' ' + units[i];
  }

  function fmtNumber(value) {
    if (typeof value === 'number') {
      if (Number.isNaN(value)) return 'NaN';
      if (value === Infinity) return 'Infinity';
      if (value === -Infinity) return '-Infinity';
      return String(value);
    }
    return String(value);
  }

  function shortSha(sha) {
    if (!sha) return '—';
    return sha.slice(0, 12) + '…' + sha.slice(-6);
  }

  function badge(label, tone) {
    return h('span', { class: 'badge' + (tone ? ' ' + tone : ''), text: label });
  }

  function notice(title, body, tone) {
    return h('div', { class: 'notice' + (tone ? ' ' + tone : ''), role: tone === 'danger' ? 'alert' : 'note' },
      title ? h('strong', { text: title }) : null,
      body ? h('p', { text: body }) : null);
  }

  function dataTable(headers, rows, options) {
    options = options || {};
    var table = h('table', { class: options.class || '' });
    var thead = h('thead');
    var tr = h('tr');
    headers.forEach(function (head) {
      var spec = typeof head === 'string' ? { text: head } : head;
      tr.appendChild(h('th', { scope: 'col', class: spec.class || null, text: spec.text }));
    });
    thead.appendChild(tr);
    table.appendChild(thead);
    var tbody = h('tbody');
    rows.forEach(function (row) {
      var rowEl = h('tr');
      row.forEach(function (cell) {
        var spec = cell && typeof cell === 'object' && !(cell instanceof Node) ? cell : { text: cell };
        var td = h('td', { class: spec.class || null });
        append(td, spec.node || spec.text);
        rowEl.appendChild(td);
      });
      tbody.appendChild(rowEl);
    });
    table.appendChild(tbody);
    return h('div', { class: 'data-table-wrap' }, table);
  }

  function details(summary, body, open) {
    var d = h('details');
    if (open) d.open = true;
    d.appendChild(h('summary', { text: summary }));
    d.appendChild(h('div', { class: 'details-body' }, body));
    return d;
  }

  function jsonDetails(label, value, open) {
    return details(label, h('pre', { class: 'json', text: JSON.stringify(value, null, 2) }), open);
  }

  function kvGrid(pairs) {
    var dl = h('dl', { class: 'kv-grid' });
    pairs.forEach(function (pair) {
      if (!pair) return;
      dl.appendChild(h('dt', { text: pair[0] }));
      var dd = h('dd');
      append(dd, pair[1]);
      dl.appendChild(dd);
    });
    return dl;
  }

  function passLink(id, label) {
    if (!id) return document.createTextNode('—');
    var p = state.passById.get(id);
    return h('a', { href: '#pass/' + encodeURIComponent(id), text: label || (p ? (p.displayLabel || p.name) : id) });
  }

  function resourceLink(id, label) {
    if (!id) return document.createTextNode('—');
    var r = state.resourceById.get(id);
    return h('a', { href: '#resource/' + encodeURIComponent(id), text: label || (r ? (r.displayLabel || r.name) : id) });
  }

  function packageUrl(rel) {
    return new URL(rel.replace(/^\/+/, ''), state.packageBase).toString();
  }

  function fetchJson(rel) {
    var url = packageUrl(rel);
    return fetch(url, { cache: 'no-store' }).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status + ' while fetching ' + url);
      return response.json();
    }).catch(function (error) {
      throw new Error(error.message + ' (' + url + ')');
    });
  }

  function tryFetchJson(rel, fallback) {
    return fetchJson(rel).catch(function () { return fallback; });
  }

  // -------------------------------------------------------------- package
  function PackageStore(options) {
    options = options || {};
    this.baseUrl = options.baseUrl || null;
    // Never store the native fetch function unbound: Window.fetch is a WebIDL
    // operation that requires its global receiver.  A wrapper calls the current
    // global fetch with that receiver and keeps injected fetchFn overrides intact.
    this.fetchFn = options.fetchFn || (typeof fetch === 'function'
      ? function (url, init) {
        var receiver = typeof globalThis !== 'undefined' ? globalThis : null;
        if (!receiver || typeof receiver.fetch !== 'function') {
          throw new Error('No fetch implementation is available.');
        }
        return receiver.fetch(url, init);
      }
      : null);
    this.gzipCtor = options.DecompressionStream
      || (typeof DecompressionStream !== 'undefined' ? DecompressionStream : null);
    var cryptoObj = options.crypto
      || (typeof window !== 'undefined' && window.crypto)
      || (typeof globalThis !== 'undefined' && globalThis.crypto)
      || null;
    this.cryptoObj = cryptoObj;
    this.chunks = new Map();
    this.chunkCache = new Map();
    this.inflight = new Map();
    this.cryptoAvailable = !!(cryptoObj && cryptoObj.subtle && cryptoObj.subtle.digest);
    this.warnedCrypto = false;
  }

  PackageStore.prototype.urlFor = function (rel) {
    var base = this.baseUrl || state.packageBase;
    return new URL(rel.replace(/^\/+/, ''), base).toString();
  };

  PackageStore.prototype.load = async function () {
    var params = new URLSearchParams(window.location.search);
    var override = params.get('package');
    var base = override ? new URL(override, document.baseURI) : new URL('assets/lumen-frame/', document.baseURI);
    if (!/\/$/.test(base.pathname)) base = new URL(base.pathname + '/', base);
    state.packageBase = base;
    this.baseUrl = base.toString();
    var index = await fetchJson('index.json');
    if (!index || index.format !== 'unreal-frame-explorer-package') {
      throw new Error('index.json is not an unreal-frame-explorer-package manifest.');
    }
    state.index = index;
    var results = await Promise.all([
      tryFetchJson('resources.json', { items: [], total: 0 }),
      tryFetchJson('passes.json', { items: [], total: 0 }),
      tryFetchJson('coverage.json', { gaps: [], counts: {}, complete: false }),
      tryFetchJson('schema.json', { status: 'missing', resourceSchemas: [], parameterSchemas: [], sourceRefs: {} }),
      tryFetchJson('capture.json', { infos: {}, consoleVariables: [], nativeFiles: [] }),
      tryFetchJson('structures.json', { byPointer: {} }),
      tryFetchJson('native-inputs.json', { items: [], count: 0 })
    ]);
    state.resources = results[0] || { items: [] };
    state.passes = results[1] || { items: [] };
    state.coverage = results[2] || { gaps: [] };
    state.schema = results[3] || { status: 'missing' };
    state.capture = results[4] || {};
    state.structures = results[5] || { byPointer: {} };
    state.nativeInputs = results[6] || { items: [], count: 0 };
    (state.resources.items || []).forEach(function (r) { state.resourceById.set(r.id, r); });
    (state.passes.items || []).forEach(function (p) { state.passById.set(p.id, p); });
    (state.coverage.gaps || []).forEach(function (gap, index) {
      if (!gap.id) gap.id = 'gap-' + (index + 1);
    });
    this.chunks = new Map();
    (index.chunks || []).forEach(function (chunk) { this.chunks.set(chunk.sha, chunk); }, this);
    state.sourceRefMap = buildSourceRefMap(state.schema);
    return index;
  };

  PackageStore.prototype.record = async function (rel) {
    if (state.recordCache.has(rel)) return state.recordCache.get(rel);
    var data = await fetchJson(rel);
    state.recordCache.set(rel, data);
    return data;
  };

  PackageStore.prototype.getChunk = async function (sha, declaredChunkLength) {
    var meta = this.chunks.get(sha);
    if (!meta) throw new Error('Chunk ' + sha + ' is not listed in index.json.');
    // index.json is the authority for the decompressed chunk length. A byte
    // range read only covers a slice of that chunk; it must never be passed
    // here as the expected chunk length.
    var expectedLength = Number(meta.rawLength);
    if (!Number.isInteger(expectedLength) || expectedLength <= 0) {
      throw new Error('Chunk ' + sha + ' has no valid uncompressed length in index.json.');
    }
    if (declaredChunkLength !== undefined && declaredChunkLength !== null
        && Number(declaredChunkLength) !== expectedLength) {
      throw new Error('Chunk ' + sha + ' is ' + expectedLength + ' bytes; ' + declaredChunkLength
        + ' is a requested slice length, not the whole chunk length.');
    }
    if (this.chunkCache.has(sha)) return this.chunkCache.get(sha);
    if (this.inflight.has(sha)) return this.inflight.get(sha);
    var self = this;
    var promise = (async function () {
      if (!self.fetchFn) throw new Error('No fetch implementation is available.');
      var url = self.urlFor('chunks/' + sha + '.gz');
      var response = await self.fetchFn(url, { cache: 'no-store' });
      if (!response.ok) throw new Error('HTTP ' + response.status + ' while fetching ' + url);
      if (!self.gzipCtor) {
        throw new Error('This browser has no DecompressionStream. Serve the package over HTTP in a current browser; raw bytes were not substituted.');
      }
      var decompressed;
      try {
        decompressed = await new Response(response.body.pipeThrough(new self.gzipCtor('gzip'))).arrayBuffer();
      } catch (error) {
        throw new Error('Could not decompress ' + url + ': ' + error.message);
      }
      var bytes = new Uint8Array(decompressed);
      if (bytes.length !== expectedLength) {
        throw new Error('Chunk ' + sha + ' length mismatch: got ' + bytes.length + ', expected ' + expectedLength + '.');
      }
      if (self.cryptoAvailable) {
        var digest = await self.cryptoObj.subtle.digest('SHA-256', bytes);
        var actual = Array.prototype.map.call(new Uint8Array(digest), function (b) {
          return b.toString(16).padStart(2, '0');
        }).join('');
        if (actual !== sha) throw new Error('Chunk SHA-256 mismatch: got ' + actual + ', expected ' + sha + '.');
      } else if (!self.warnedCrypto) {
        self.warnedCrypto = true;
        setLiveStatus('Chunk lengths are checked, but SHA-256 verification is unavailable because crypto.subtle is not exposed in this context. Serve over http(s) for hash checks.');
      }
      self.chunkCache.set(sha, bytes);
      if (self.chunkCache.size > 64) {
        var first = self.chunkCache.keys().next().value;
        self.chunkCache.delete(first);
      }
      return bytes;
    })();
    this.inflight.set(sha, promise);
    try { return await promise; } finally { this.inflight.delete(sha); }
  };

  PackageStore.prototype.readRange = async function (payload, offset, length) {
    if (!payload || !Array.isArray(payload.chunks)) throw new Error('Payload has no ordered chunk references.');
    var pieces = Codec.chunkRange(payload.chunks, offset, length, payload.bytes);
    var self = this;
    var data = await Promise.all(pieces.map(function (piece) {
      // Whole chunk; the requested range is sliced after verification.
      return self.getChunk(piece.sha);
    }));
    return Codec.assembleRange(payload.chunks, data, offset, length, payload.bytes);
  };

  PackageStore.prototype.readAll = async function (payload, cap) {
    if (payload.bytes > cap) throw new Error('Refusing to fetch ' + fmtBytes(payload.bytes) + ' for this action; the cap is ' + fmtBytes(cap) + '. Use the reconstruction command or download individual chunks.');
    return this.readRange(payload, 0, payload.bytes);
  };

  var explorer = new PackageStore();

  function buildSourceRefMap(schema) {
    var map = new Map();
    if (!schema) return map;
    var refs = schema.sourceRefs || schema.source_refs;
    if (Array.isArray(refs)) {
      refs.forEach(function (ref, index) {
        if (ref && typeof ref === 'object') map.set(String(ref.id || ref.name || index), ref);
      });
    } else if (refs && typeof refs === 'object') {
      Object.keys(refs).forEach(function (key) { map.set(key, refs[key]); });
    }
    return map;
  }

  function sourceRefText(ref) {
    if (ref === null || ref === undefined) return '';
    if (typeof ref === 'string') return ref;
    var parts = [];
    if (ref.path) parts.push(ref.path + (ref.line ? ':' + ref.line : ''));
    if (ref.symbol) parts.push(ref.symbol);
    if (ref.claim) parts.push(ref.claim);
    if (!parts.length) parts.push(JSON.stringify(ref));
    return parts.join(' — ');
  }

  function appendSourceRefs(parent, refs) {
    var list = Array.isArray(refs) ? refs : refs ? [refs] : [];
    if (!list.length) { parent.appendChild(document.createTextNode('—')); return; }
    var wrap = h('div');
    list.forEach(function (ref) {
      var value = ref;
      if (typeof ref === 'string' && state.sourceRefMap.has(ref)) value = state.sourceRefMap.get(ref);
      if (value && typeof value === 'object' && value.url && /^https?:\/\//i.test(value.url)) {
        wrap.appendChild(h('a', { class: 'source-anchor', href: value.url, rel: 'noopener noreferrer', target: '_blank', text: sourceRefText(value) }));
      } else {
        wrap.appendChild(h('span', { class: 'source-anchor', text: sourceRefText(value) }));
      }
    });
    parent.appendChild(wrap);
  }

  function appendNamedSourceRefs(parent, label, refs) {
    var row = h('div', { class: 'small' });
    row.appendChild(h('strong', { text: label + ': ' }));
    appendSourceRefs(row, refs);
    parent.appendChild(row);
  }

  function schemaTransforms(schema) {
    var all = state.schema && Array.isArray(state.schema.transforms) ? state.schema.transforms : [];
    var byId = new Map();
    all.forEach(function (t, i) { if (t && typeof t === 'object') byId.set(String(t.id || t.name || i), t); });
    var refs = schema && schema.transforms;
    var list = Array.isArray(refs) ? refs : refs ? [refs] : [];
    return list.map(function (ref) {
      if (typeof ref === 'string' && byId.has(ref)) return byId.get(ref);
      return ref;
    });
  }

  function renderTransforms(parent, schema) {
    var list = schemaTransforms(schema);
    if (!list.length) return;
    parent.appendChild(h('h4', { text: 'Source-linked transformations (not applied automatically)' }));
    list.forEach(function (t) {
      var wrap = h('div', { class: 'field-card' });
      if (t && typeof t === 'object') {
        wrap.appendChild(h('strong', { text: t.name || t.id || 'transform' }));
        wrap.appendChild(h('div', { class: 'equation', text: t.equation || t.expression || '(no equation text)' }));
        if (t.units) wrap.appendChild(h('p', { class: 'small muted', text: 'Units: ' + t.units }));
        if (t.note) wrap.appendChild(h('p', { class: 'small muted', text: t.note }));
        var anchors = h('div');
        appendSourceRefs(anchors, t.sourceRefs || t.sourceRefs);
        wrap.appendChild(anchors);
      } else {
        wrap.appendChild(h('div', { class: 'equation', text: String(t) }));
      }
      parent.appendChild(wrap);
    });
  }

  function renderTerms(parent, terms) {
    var list = Array.isArray(terms) ? terms : terms ? [terms] : [];
    if (!list.length) return;
    parent.appendChild(h('h4', { text: 'Math terms' }));
    var ul = h('ul', { class: 'term-list' });
    list.forEach(function (t) {
      if (t && typeof t === 'object') {
        var li = h('li', { class: 'term', title: t.definition || '' }, t.term || t.name || t.id || '(term)');
        ul.appendChild(li);
      } else {
        ul.appendChild(h('li', { class: 'term', text: String(t) }));
      }
    });
    parent.appendChild(ul);
    list.forEach(function (t) {
      if (t && typeof t === 'object' && t.definition) {
        parent.appendChild(h('p', { class: 'small muted', text: (t.term || t.id) + ': ' + t.definition }));
      }
    });
  }

  // --------------------------------------------------------------- banner
  function setLiveStatus(message) {
    var el = $('result-count');
    if (el) el.textContent = message;
  }

  function renderPackageBanner() {
    var banner = $('package-banner');
    clear(banner);
    var tone = '';
    var message = '';
    var detail = '';
    if (state.index.fixture) {
      tone = 'is-fixture';
      message = 'Labelled synthetic fixture — not runtime evidence.';
      detail = state.index.evidence && state.index.evidence.notice ? state.index.evidence.notice : '';
    } else if (state.index.evidence && state.index.evidence.kind === 'unavailable') {
      tone = 'is-unavailable';
      message = 'No capture package is present.';
      detail = (state.index.evidence && state.index.evidence.notice) || '';
    } else if (state.coverage && state.coverage.complete) {
      tone = 'is-ok';
      message = 'Package loaded; no explicit gaps are recorded.';
    } else {
      tone = 'is-incomplete';
      message = 'Package loaded; coverage is explicitly incomplete.';
      detail = (state.coverage && state.coverage.statement) || '';
    }
    banner.className = 'package-banner ' + tone;
    banner.appendChild(h('span', { class: 'status-dot', 'aria-hidden': 'true' }));
    banner.appendChild(h('strong', { text: message }));
    if (detail) banner.appendChild(h('span', { text: detail }));
    var counts = state.index.counts || {};
    banner.appendChild(h('span', {
      class: 'mono small',
      text: (counts.resources || 0) + ' resources · ' + (counts.passes || 0) + ' passes · ' + (counts.payloadFiles || 0) + ' payload files · ' + (counts.gapCount || 0) + ' gaps'
    }));
    $('footer-package').textContent = 'Package: ' + state.index.adapter + ', generated ' + (state.index.generatedUtc || 'unknown') + '.';
  }

  // ---------------------------------------------------------------- errors
  function showFatal(error) {
    var panel = $('fatal-error');
    panel.hidden = false;
    $('fatal-message').textContent = error && error.message ? error.message : String(error);
    $('package-banner').className = 'package-banner is-unavailable';
    clear($('package-banner'));
    $('package-banner').appendChild(h('span', { class: 'status-dot', 'aria-hidden': 'true' }));
    $('package-banner').appendChild(h('strong', { text: 'Package load failed. No records or bytes are fabricated.' }));
    setLiveStatus('Package load failed.');
  }

  // ------------------------------------------------------------- filtering
  function queryTerms() {
    return state.search.trim().toLowerCase().split(/\s+/).filter(Boolean);
  }

  function textMatches(haystack) {
    var terms = queryTerms();
    if (!terms.length) return true;
    var low = String(haystack || '').toLowerCase();
    return terms.every(function (term) { return low.indexOf(term) !== -1; });
  }

  function resourceMatches(item) {
    if (state.kind !== 'all' && item.kind !== state.kind) return false;
    if (!textMatches(item.searchText || (item.name + ' ' + item.uniqueName + ' ' + (item.format || '')))) return false;
    var status = state.status;
    if (status === 'all') return true;
    if (status === 'with-payload') return (item.payloadCount || 0) > 0;
    if (status === 'missing-payload') return (item.payloadCount || 0) === 0;
    if (status === 'gaps') return (item.gapCount || 0) > 0;
    if (status === 'schema-matched') return item.schemaStatus === 'matched';
    if (status === 'schema-ambiguous') return item.schemaStatus === 'ambiguous';
    if (status === 'schema-unmatched') return item.schemaStatus === 'unmatched';
    if (status === 'decoding-unsupported') return item.decodeSupported === false && item.kind === 'texture';
    return true;
  }

  function passMatches(item) {
    if (!textMatches(item.searchText || (item.name + ' ' + (item.parametersName || '')))) return false;
    var status = state.status;
    if (status === 'all') return true;
    if (status === 'with-payload') return !!item.hasParameterBlock;
    if (status === 'missing-payload') return !item.hasParameterBlock;
    if (status === 'gaps') return (item.gapCount || 0) > 0;
    if (status === 'schema-matched') return item.schemaStatus === 'matched';
    if (status === 'schema-ambiguous') return item.schemaStatus === 'ambiguous';
    if (status === 'schema-unmatched') return item.schemaStatus === 'unmatched' || item.schemaMatchCount === 0;
    if (status === 'params-only') return !!item.hasParameterBlock;
    if (status === 'decoding-unsupported') return !!item.hasUnsupportedDecode;
    return true;
  }

  function renderLists() {
    var resources = (state.resources.items || []).filter(resourceMatches);
    var passes = (state.passes.items || []).filter(passMatches);
    $('resource-count').textContent = resources.length + ' / ' + (state.resources.total || 0);
    $('pass-count').textContent = passes.length + ' / ' + (state.passes.total || 0);
    var totalMatches = resources.length + passes.length;
    setLiveStatus(totalMatches + ' matching record' + (totalMatches === 1 ? '' : 's') + ' of ' + ((state.resources.total || 0) + (state.passes.total || 0)) + '. Search covers names, parameter members, schema IDs/terms and gap messages.');

    renderRecordList($('resource-list'), resources.slice(0, state.resourceLimit), 'resource');
    renderRecordList($('pass-list'), passes.slice(0, state.passLimit), 'pass');

    var resourceMore = $('resource-more');
    resourceMore.hidden = resources.length <= state.resourceLimit;
    resourceMore.textContent = 'Show more resources (' + Math.max(0, resources.length - state.resourceLimit) + ' remaining)';
    resourceMore.onclick = function () { state.resourceLimit += LIST_PAGE; renderLists(); };
    var passMore = $('pass-more');
    passMore.hidden = passes.length <= state.passLimit;
    passMore.textContent = 'Show more passes (' + Math.max(0, passes.length - state.passLimit) + ' remaining)';
    passMore.onclick = function () { state.passLimit += LIST_PAGE; renderLists(); };

    if (!state.resources.total && !state.passes.total) {
      var empty = h('p', { class: 'muted', text: 'No passes or resources are present in this package. Coverage and gaps record why.' });
      $('resource-list').appendChild(h('li', {}, empty));
    }
  }

  function renderRecordList(list, items, kind) {
    clear(list);
    items.forEach(function (item) {
      var selected = state.selected && state.selected.kind === kind && state.selected.id === item.id;
      var button = h('button', {
        type: 'button',
        class: 'record-button',
        'aria-current': selected ? 'true' : null,
        on: { click: function () { if (kind === 'resource') selectResource(item.id); else selectPass(item.id); } }
      });
      button.appendChild(h('span', { class: 'record-title', text: item.displayLabel || item.name }));
      var meta;
      if (kind === 'resource') {
        meta = (item.kind || '?') + ' · ' + (item.format || 'no format') + ' · ' + fmtBytes(item.bytes) + ' · ' + (item.payloadCount || 0) + ' payload(s) · ' + (item.schemaStatus || 'unmatched');
        if (item.sceneInvocation) meta += ' · scene ' + item.sceneInvocation;
        else if (item.instancePrefix) meta += ' · instance ' + item.instancePrefix;
        if (item.frameNumber !== null && item.frameNumber !== undefined) meta += ' · frame ' + item.frameNumber;
        if (item.viewState && item.viewState.frameIndexMod8 !== null && item.viewState.frameIndexMod8 !== undefined) meta += ' · StateFrameIndexMod8 ' + item.viewState.frameIndexMod8;
      } else {
        meta = (item.inputCount || 0) + ' in · ' + (item.outputCount || 0) + ' out · ' + (item.hasParameterBlock ? 'params ' + fmtBytes(item.parameterBlockBytes) : 'no parameter bytes') + ' · ' + (item.schemaMatchCount || 0) + ' schema';
        if (item.frameNumber !== null && item.frameNumber !== undefined) meta += ' · frame ' + item.frameNumber;
        if (item.viewState && item.viewState.frameIndexMod8 !== null && item.viewState.frameIndexMod8 !== undefined) meta += ' · StateFrameIndexMod8 ' + item.viewState.frameIndexMod8;
        meta += item.drawCount === null || item.drawCount === undefined ? ' · draw count not captured' : ' · ' + item.drawCount + ' draw(s)';
      }
      button.appendChild(h('span', { class: 'record-meta', text: meta }));
      list.appendChild(h('li', {}, button));
    });
  }

  // ------------------------------------------------------------ navigation
  function setActiveTab(name) {
    state.activeTab = name;
    ['explore', 'coverage', 'schema', 'capture'].forEach(function (tab) {
      var button = $('tab-' + tab);
      var panel = $('panel-' + tab);
      var active = tab === name;
      button.setAttribute('aria-selected', active ? 'true' : 'false');
      button.tabIndex = active ? 0 : -1;
      panel.hidden = !active;
    });
    if (name === 'coverage' && !state.coverageRendered) { renderCoverage(); state.coverageRendered = true; }
    if (name === 'schema' && !state.schemaRendered) { renderSchema(); state.schemaRendered = true; }
    if (name === 'capture' && !state.captureRendered) { renderCapture(); state.captureRendered = true; }
  }

  function navigate(hash, push) {
    var next = hash || '#explore';
    if (push && window.location.hash !== next) {
      history.replaceState(null, '', next);
    }
    var route = next.replace(/^#/, '');
    if (route === 'coverage' || route === 'schema' || route === 'capture') {
      setActiveTab(route);
      return;
    }
    var match = /^(resource|pass)\/(.+)$/.exec(route);
    if (match) {
      setActiveTab('explore');
      var id = decodeURIComponent(match[2]);
      if (match[1] === 'resource') selectResource(id, true);
      else selectPass(id, true);
      return;
    }
    setActiveTab('explore');
  }

  function selectResource(id, fromRoute) {
    var item = state.resourceById.get(id);
    if (!item) { showDetailError('Unknown resource id ' + id); return; }
    state.selected = { kind: 'resource', id: id };
    renderLists();
    if (!fromRoute) history.replaceState(null, '', '#resource/' + encodeURIComponent(id));
    var token = ++state.detailToken;
    renderDetailLoading(item.name);
    explorer.record(item.record).then(function (record) {
      if (token !== state.detailToken) return;
      renderResourceDetail(record);
      $('detail').scrollTop = 0;
    }).catch(function (error) {
      if (token !== state.detailToken) return;
      showDetailError('Could not load resource record: ' + error.message);
    });
  }

  function selectPass(id, fromRoute) {
    var item = state.passById.get(id);
    if (!item) { showDetailError('Unknown pass id ' + id); return; }
    state.selected = { kind: 'pass', id: id };
    renderLists();
    if (!fromRoute) history.replaceState(null, '', '#pass/' + encodeURIComponent(id));
    var token = ++state.detailToken;
    renderDetailLoading(item.name);
    explorer.record(item.record).then(function (record) {
      if (token !== state.detailToken) return;
      renderPassDetail(record);
      $('detail').scrollTop = 0;
    }).catch(function (error) {
      if (token !== state.detailToken) return;
      showDetailError('Could not load pass record: ' + error.message);
    });
  }

  function renderDetailLoading(name) {
    var detail = $('detail');
    clear(detail);
    detail.appendChild(h('p', { class: 'loading', text: 'Loading ' + name + '…' }));
  }

  function showDetailError(message) {
    var detail = $('detail');
    clear(detail);
    detail.appendChild(notice('Record unavailable', message, 'danger'));
  }

  function detailHeader(kicker, title, subtitle, badges) {
    var header = h('header', { class: 'detail-header' });
    header.appendChild(h('p', { class: 'detail-kicker', text: kicker }));
    header.appendChild(h('h2', { text: title }));
    if (subtitle) header.appendChild(h('p', { class: 'detail-sub', text: subtitle }));
    if (badges && badges.length) header.appendChild(h('div', { class: 'badge-row' }, badges));
    return header;
  }

  function renderGapsList(gaps, parent) {
    if (!gaps || !gaps.length) {
      parent.appendChild(h('p', { class: 'muted', text: 'No explicit gaps are recorded for this record.' }));
      return;
    }
    var ul = h('ul', { class: 'gap-list' });
    gaps.forEach(function (gap) {
      var li = h('li', { class: 'gap-item severity-' + (gap.severity || 'info') });
      li.appendChild(h('div', { class: 'badge-row' }, badge(gap.kind || 'gap', gap.severity === 'error' ? 'danger' : gap.severity === 'warning' ? 'warn' : 'accent'), gap.source ? badge(gap.source, '') : null));
      li.appendChild(h('p', { text: gap.message || '' }));
      if (gap.source) {
        var path = String(gap.source).replace(/:\d+$/, '');
        var link = h('a', { href: packageUrl('native/' + path), text: 'Native source: ' + path });
        li.appendChild(h('p', { class: 'gap-meta' }, link));
      }
      ul.appendChild(li);
    });
    parent.appendChild(ul);
  }

  // -------------------------------------------------------------- resource
  function viewportInfo(resource) {
    var vp = resource && resource.viewport;
    if (vp && typeof vp === 'object') return vp;
    var padding = resource && resource.padding;
    if (padding && padding.padded) {
      return {
        applicability: 'legacy-unverified', screenPadding: false, cropAllowed: false,
        domain: 'unknown', classification: null, passViewRect: resource.viewState && resource.viewState.viewRect || null,
        passId: null, passName: null,
        reason: 'This package predates explicit viewport applicability records; the legacy padding is retained as context, but the crop is disabled until the package is rebuilt.',
        source: 'legacy-package'
      };
    }
    return {
      applicability: 'no-view-context', screenPadding: false, cropAllowed: false,
      domain: 'unknown', classification: null, passViewRect: null, passId: null, passName: null,
      reason: 'This package has no explicit viewport applicability record; screen padding is not inferred.',
      source: 'legacy-package'
    };
  }

  function rectText(rect) {
    if (!rect || !Array.isArray(rect.min) || !Array.isArray(rect.size)) return 'not recorded';
    return rect.min.join(',') + ' ' + rect.size.join(' × ');
  }

  function renderResourceDetail(record) {
    var detail = $('detail');
    clear(detail);
    var decode = record.decode || {};
    var r = record;
    var badges = [
      badge(r.kind),
      badge(r.format || 'no pixel format'),
      badge(fmtBytes(r.bytes)),
      badge((r.payloads || []).length + ' payload(s)', (r.payloads || []).length ? 'ok' : 'warn')
    ];
    if (decode.dumpFormat && decode.dumpFormat !== r.format) badges.push(badge('dumped as ' + decode.dumpFormat, 'warn'));
    if (r.schemaMatches && r.schemaMatches.length) {
      badges.push(badge(r.schemaMatches.length + ' schema match' + (r.schemaMatches.length === 1 ? '' : 'es'), r.schemaMatches.length > 1 ? 'warn' : 'accent'));
    } else {
      badges.push(badge('schema unmatched', 'warn'));
    }
    if (r.kind === 'texture' && decode.supported === false) badges.push(badge('decode unsupported', 'danger'));
    detail.appendChild(detailHeader('Resource', r.displayLabel || r.name, r.uniqueName + ' · native metadata and captured payloads', badges));

    if (state.index.fixture) detail.appendChild(notice('Synthetic fixture', 'These bytes are a labelled test fixture. They are not runtime evidence.', 'warn'));
    var viewport = viewportInfo(r);
    if (viewport.applicability === 'screen-aligned' && r.padding && r.padding.padded) {
      detail.appendChild(notice('Camera ViewRect touches this allocation',
        'Allocation ' + (r.allocation || r.extent || []).slice(0, 2).join(' × ') + '; camera pass ' + (viewport.passName || '—')
        + ' recorded ViewRect ' + r.padding.validWidth + ' × ' + r.padding.validHeight + ' at (' + r.padding.left + ', ' + r.padding.top + '). '
        + r.padding.bottom + ' padded row(s) below and ' + r.padding.right + ' padded column(s) right are outside the camera view. '
        + 'The overview can crop to the valid view for display only; the numeric inspector still reads allocation coordinates exactly and no captured byte is removed.',
        'warn'));
    } else if (viewport.passViewRect) {
      detail.appendChild(notice('ViewRect retained as context only',
        'This resource is classified ' + viewport.applicability + (viewport.domain ? ' (' + viewport.domain + ')' : '')
        + '. ' + (viewport.reason || 'Screen padding is not inferred.')
        + ' Pass ViewRect ' + rectText(viewport.passViewRect) + ' is displayed as context, not used to crop or remove bytes.',
        viewport.applicability === 'unknown-mapping' ? 'warn' : ''));
    }
    if (r.kind === 'texture' && decode.supported === false) {
      detail.appendChild(notice('Semantic decode unavailable', decode.reason || 'No explicit decoder for this format. Raw bytes remain available below; no numeric values are inferred.', 'danger'));
    }
    if (decode.convertedByDumpGpu) {
      detail.appendChild(notice('DumpGPU postprocessed this texture', 'The descriptor retains the native format ' + r.format + ', while the saved payload uses ' + decode.dumpFormat + ' after DumpGPU decompression/preprocessing. Numeric reads use the dumped layout; the original descriptor bytes are not reinterpreted.', 'warn'));
    }
    if (!(r.payloads || []).length) {
      detail.appendChild(notice('No captured payload', 'The native resource descriptor exists, but no raw binary payload was captured for this resource. Nothing is zero-filled or substituted.', 'danger'));
    }

    var extent = Array.isArray(r.extent) ? r.extent : [];
    var facts = [
      ['Native name', r.name],
      ['Unique resource name', r.uniqueName],
      ['Display label', r.displayLabel || '—'],
      ['Scene invocation', r.sceneInvocation || (r.instancePrefix ? 'not recorded (native prefix ' + r.instancePrefix + ')' : '—')],
      ['Frame', r.frameNumber === null || r.frameNumber === undefined ? '—' : String(r.frameNumber)],
      ['Producer pass interval', r.passInterval ? String(r.passInterval.first) + (r.passInterval.last !== r.passInterval.first ? '..' + r.passInterval.last : '') : '—'],
      ['View.StateFrameIndexMod8', r.viewState && r.viewState.frameIndexMod8 !== null && r.viewState.frameIndexMod8 !== undefined ? String(r.viewState.frameIndexMod8) : 'not recorded'],
      ['Allocation', (r.allocation || extent).join(' × ') || '—'],
      ['Viewport mapping', viewport.applicability + (viewport.domain ? ' · ' + viewport.domain : '')],
      ['Viewport basis', viewport.basis || '—'],
      ['ViewRect (mapping / context)', viewport.passViewRect ? rectText(viewport.passViewRect) + (viewport.passName ? ' · ' + viewport.passName : '') : 'not recorded'],
      ['Padding', viewport.applicability === 'screen-aligned' && r.padding ? (r.padding.padded ? 'left ' + r.padding.left + ' · top ' + r.padding.top + ' · right ' + r.padding.right + ' · bottom ' + r.padding.bottom : 'none (allocation equals the view rect)') : 'not applicable: ' + (viewport.reason || 'no screen mapping')],
      ['Kind', r.kind],
      ['Descriptor format', r.format || '—'],
      ['Dumped payload layout', decode.dumpFormat || r.format || '—'],
      ['Declared bytes', fmtBytes(r.bytes) + ' (' + (r.bytes || 0) + ' B)'],
      ['Extent', extent.join(' × ') || '—'],
      ['Array size / mips / samples', (r.arraySize || 1) + ' / ' + (r.numMips || 1) + ' / ' + (r.numSamples || 1)],
      ['Buffer element layout', r.bytesPerElement ? r.bytesPerElement + ' B × ' + (r.numElements || '?') + ' elements' : '—'],
      ['Metadata pointer', r.metadataPointer || '—'],
      ['Native record', r.nativeRecordId ? r.nativeRecordId + ' · ' + r.nativePath : '—'],
      ['Native source', r.nativePath ? h('a', { href: packageUrl('native/' + r.nativePath), text: r.nativePath }) : '—']
    ];
    detail.appendChild(kvGrid(facts));

    if (r.bufferMetadata) {
      detail.appendChild(details('Captured buffer structure metadata', jsonDetails('FSynthetic / native member list', r.bufferMetadata)));
    }

    if ((r.subresourceLayouts || []).length) {
      detail.appendChild(h('h3', { text: 'Captured subresource layouts' }));
      var layoutRows = r.subresourceLayouts.map(function (item) {
        var layout = item.layout || {};
        var completion = item.completion || {};
        return [
          item.file,
          (layout.Dimension || '—') + ' · mip ' + layout.MipLevel + (layout.CubeFace !== null && layout.CubeFace !== undefined ? ' · face ' + layout.CubeFace : (layout.NumArraySlices >= 1 ? ' · slice ' + layout.FirstArraySlice : '')) + (layout.ZSliceCount > 1 ? ' · z ' + layout.ZSliceCount : ''),
          [layout.ExtentX, layout.ExtentY, layout.Depth].join(' × '),
          layout.RowPitchBytes + ' / ' + layout.ZSliceStrideBytes,
          layout.NativeFormat || '—', layout.StoredFormat || '—',
          String(layout.BitExact), String(layout.ValuePreserving),
          completion.dumped ? 'Dumped · ' + (completion.timing || '') : 'unobserved',
          { text: String(layout.ByteSize), class: 'mono' }
        ];
      });
      detail.appendChild(dataTable(['File', 'Subresource', 'Extent', 'Row pitch / z stride', 'Native format', 'Stored format', 'Bit exact', 'Value preserving', 'Completion', 'ByteSize'], layoutRows));
    }

    var payloads = (r.payloads || []).slice().sort(function (a, b) {
      return String(a.version || '').localeCompare(String(b.version || '')) || (a.mip || 0) - (b.mip || 0) || (a.slice || 0) - (b.slice || 0);
    });
    var probeHost = h('div');
    var schemaHost = h('div');
    var downloadHost = h('div');
    var probeContext = null;
    var schemaSequence = 0;
    var showPayload = null;
    if (payloads.length) {
      detail.appendChild(h('h3', { text: 'Subresource and numeric inspection' }));
      var select = h('select', { id: 'payload-select', 'aria-label': 'Select captured payload / subresource' });
      payloads.forEach(function (payload) {
        var option = h('option', { value: payload.id });
        option.textContent = payloadLabel(payload) + ' · ' + fmtBytes(payload.bytes);
        select.appendChild(option);
      });
      var selectorCard = h('div', { class: 'field-card' });
      selectorCard.appendChild(h('h4', { text: 'Independent subresource controls' }));
      selectorCard.appendChild(h('p', { class: 'small muted', text: 'Version, mip and array-slice/cube-face are separate recorded dimensions. A combination that was not captured is reported as unavailable; no payload is synthesized.' }));
      function uniqueValues(list) {
        var seen = new Set();
        var out = [];
        list.forEach(function (value) {
          var key = String(value);
          if (!seen.has(key)) { seen.add(key); out.push(value); }
        });
        return out;
      }
      var versions = uniqueValues(payloads.map(function (p) { return p.version; }));
      var mips = uniqueValues(payloads.map(function (p) { return p.mip; }));
      var faceMode = payloads.some(function (p) { return p.face !== null && p.face !== undefined; });
      var units = uniqueValues(payloads.map(function (p) { return faceMode ? p.face : p.slice; }));
      function dimensionSelect(label, values, labeler) {
        var control = h('select', { 'aria-label': label });
        values.forEach(function (value) {
          control.appendChild(h('option', { value: value === null || value === undefined ? '' : String(value), text: labeler(value) }));
        });
        return control;
      }
      var versionSelect = dimensionSelect('Version', versions, function (v) { return v ? 'v' + v : 'no version recorded'; });
      var mipSelect = dimensionSelect('Mip', mips, function (m) { return m === null || m === undefined ? 'mip not recorded' : 'mip ' + m; });
      var unitSelect = dimensionSelect(faceMode ? 'Cube face' : 'Array slice', units, function (u) {
        if (u === null || u === undefined) return faceMode ? 'face not recorded' : 'slice not recorded';
        return (faceMode ? 'cube face ' : 'array slice ') + u;
      });
      var hasAspect = payloads.some(function (p) { return p.stencil; }) && payloads.some(function (p) { return !p.stencil; });
      var aspectSelect = hasAspect ? dimensionSelect('Aspect', [false, true], function (value) {
        return value ? 'stencil' : 'colour/depth';
      }) : null;
      var controlGrid = h('div', { class: 'tool-grid' });
      var controlPairs = [['Version', versionSelect], ['Mip', mipSelect], [faceMode ? 'Cube face (view unit)' : 'Array slice', unitSelect]];
      if (aspectSelect) controlPairs.push(['Aspect', aspectSelect]);
      controlPairs.forEach(function (pair) {
        var card = h('div');
        card.appendChild(h('label', { text: pair[0] }));
        card.appendChild(pair[1]);
        controlGrid.appendChild(card);
      });
      selectorCard.appendChild(controlGrid);
      selectorCard.appendChild(h('label', { for: 'payload-select', text: 'Resolved captured payload (all dimensions shown)' }));
      selectorCard.appendChild(select);
      var controlsNote = h('div', { class: 'small muted' });
      selectorCard.appendChild(controlsNote);
      var payloadInfo = h('div', { class: 'small muted' });
      selectorCard.appendChild(payloadInfo);
      detail.appendChild(selectorCard);
      detail.appendChild(probeHost);
      detail.appendChild(schemaHost);
      detail.appendChild(downloadHost);

      function onProbeContext(context) {
        probeContext = context;
        var sequence = ++schemaSequence;
        // renderSchemaFields is async; ignore its result if the selection moved on.
        renderSchemaFields(schemaHost, r, record, context).then(function () {
          if (sequence !== schemaSequence) return;
        });
      }

      showPayload = function (payload) {
        probeContext = null;
        clear(payloadInfo);
        payloadInfo.appendChild(h('p', { text: payloadLabel(payload) + ' · ' + (payload.originalPath || '') }));
        payloadInfo.appendChild(h('p', { text: 'Raw length ' + payload.bytes + ' B · SHA-256 ' + (payload.sha256 || '—') + ' · ' + (payload.chunks || []).length + ' chunks' }));
        renderProbe(probeHost, r, record, payload, onProbeContext);
        clear(schemaHost);
        renderSchemaFields(schemaHost, r, record, null);
        clear(downloadHost);
        downloadHost.appendChild(renderDownloadCard(r, payload));
      };
      function controlsMatch(payload) {
        var v = versionSelect.value;
        var m = mipSelect.value;
        var u = unitSelect.value;
        var payloadVersion = payload.version === null || payload.version === undefined ? '' : String(payload.version);
        var payloadMip = payload.mip === null || payload.mip === undefined ? '' : String(payload.mip);
        var payloadUnit = (faceMode ? payload.face : payload.slice);
        var payloadUnitText = payloadUnit === null || payloadUnit === undefined ? '' : String(payloadUnit);
        if (aspectSelect && String(!!payload.stencil) !== aspectSelect.value) return false;
        return payloadVersion === v && payloadMip === m && payloadUnitText === u;
      }
      function syncControls(payload) {
        versionSelect.value = payload.version === null || payload.version === undefined ? '' : String(payload.version);
        mipSelect.value = payload.mip === null || payload.mip === undefined ? '' : String(payload.mip);
        var unit = faceMode ? payload.face : payload.slice;
        unitSelect.value = unit === null || unit === undefined ? '' : String(unit);
        if (aspectSelect) aspectSelect.value = String(!!payload.stencil);
      }
      function fromControls() {
        var payload = payloads.find(controlsMatch);
        if (!payload) {
          clear(probeHost); clear(schemaHost); clear(downloadHost); clear(payloadInfo);
          controlsNote.textContent = 'This version/mip/' + (faceMode ? 'face' : 'slice') + (aspectSelect ? '/aspect' : '') + ' combination was not captured. Nothing is inferred.';
          probeHost.appendChild(notice('Subresource not captured', 'The selected combination has no payload in this resource record. Choose another captured combination; no missing bytes are fabricated.', 'warn'));
          return;
        }
        controlsNote.textContent = 'Resolved to ' + payload.id + ' (' + (payload.originalPath || '') + ').';
        select.value = payload.id;
        showPayload(payload);
      }
      var controlList = [versionSelect, mipSelect, unitSelect];
      if (aspectSelect) controlList.push(aspectSelect);
      controlList.forEach(function (control) { control.addEventListener('change', fromControls); });
      select.addEventListener('change', function () {
        var payload = payloads.find(function (p) { return p.id === select.value; });
        if (payload) { syncControls(payload); fromControls(); }
      });
      syncControls(payloads[0]);
      fromControls();
    } else {
      detail.appendChild(h('h3', { text: 'Numeric inspection' }));
      detail.appendChild(notice('No raw payload to probe', 'Only native metadata is available for this resource. Hex and numeric reads are disabled rather than filled with zeroes.', 'warn'));
    }

    if ((r.schemaMatches || []).length) {
      var schemaSection = h('section');
      schemaSection.appendChild(h('h3', { text: 'Source schema joins' }));
      schemaSection.appendChild(h('p', { class: 'muted small', text: 'Joins use every explicit selector (resource name regex and, when present, pass name regex). Multiple matches are shown as ambiguous and none is selected silently.' }));
      r.schemaMatches.forEach(function (match) { schemaSection.appendChild(renderResourceSchemaMatch(match)); });
      detail.appendChild(schemaSection);
    } else {
      detail.appendChild(h('h3', { text: 'Source schema joins' }));
      detail.appendChild(notice('No schema match', 'No resource schema selector matched this resource. This is a coverage gap, not evidence that the resource is uninteresting. Native metadata remains authoritative.', 'warn'));
    }

    if ((r.versions || []).length) {
      detail.appendChild(h('h3', { text: 'Version and history relationship' }));
      var versionRows = r.versions.map(function (v) {
        var payloadCount = (v.payloadIds || []).length;
        return [
          v.version,
          v.producerPassId ? passLink(v.producerPassId, v.producerPassName || v.producerPassId) : (/^0{20}$/.test(v.version) ? 'initial/external version (no captured producer pass)' : 'no captured producer pass'),
          String(payloadCount),
          v.previousVersion || '—',
          v.nextVersion ? h('button', { type: 'button', text: 'Next version', on: { click: function () { var p = payloads.find(function (q) { return q.version === v.nextVersion; }); if (p && showPayload) { select.value = p.id; showPayload(p); } } } }) : '—'
        ];
      });
      detail.appendChild(dataTable(['Version', 'Producing pass', 'Payloads', 'Previous', 'Next'], versionRows));
      detail.appendChild(h('p', { class: 'table-note', text: 'Version suffixes come from native DumpGPU payload filenames. They are not assumed to be chronological; the producing pass pointer is shown when it can be resolved.' }));
    }

    if (r.bindings) {
      detail.appendChild(h('h3', { text: 'Bindings: producers and consumers' }));
      detail.appendChild(renderBindingTable('Producer pass', r.bindings.producers, r));
      detail.appendChild(renderBindingTable('Consumer pass', r.bindings.consumers, r));
    }

    if ((r.nativeUniformBindings || []).length) {
      detail.appendChild(h('h3', { text: 'Native uniform bindings that reference this resource' }));
      detail.appendChild(renderNativeBindingTable(r.nativeUniformBindings, 'resource', payloads));
    }

    detail.appendChild(h('h3', { text: 'All captured payloads' }));
    detail.appendChild(renderPayloadTable(record, payloads));

    detail.appendChild(h('h3', { text: 'Native metadata and gaps' }));
    detail.appendChild(jsonDetails('Complete native resource descriptor', r.native, false));
    detail.appendChild(details('Resource gaps (' + (r.gaps || []).length + ')', (function () { var d = h('div'); renderGapsList(r.gaps || [], d); return d; })(), (r.gaps || []).length > 0));
  }

  function formatForResource(resource, payload) {
    if (payload && payload.subresource && payload.subresource.storageFormat) return payload.subresource.storageFormat;
    if (payload && payload.layout && payload.layout.StoredFormat) return payload.layout.StoredFormat;
    if (payload && payload.decode && payload.decode.dumpFormat) return payload.decode.dumpFormat;
    if (payload && payload.stencil) return 'PF_R8_UINT';
    return (resource.decode && resource.decode.dumpFormat) || resource.format;
  }

  function payloadLayout(payload) {
    if (!payload) return null;
    return payload.subresource || (payload.layout ? {
      dimension: payload.layout.Dimension,
      mipLevel: payload.layout.MipLevel,
      firstArraySlice: payload.layout.FirstArraySlice,
      numArraySlices: payload.layout.NumArraySlices,
      cubeFace: payload.layout.CubeFace,
      extentX: payload.layout.ExtentX,
      extentY: payload.layout.ExtentY,
      depth: payload.layout.Depth,
      zSliceCount: payload.layout.ZSliceCount,
      zSliceStrideBytes: payload.layout.ZSliceStrideBytes,
      rowPitchBytes: payload.layout.RowPitchBytes,
      storageFormat: payload.layout.StoredFormat,
      stencil: payload.layout.StencilSubresource
    } : null);
  }

  function subresourceInfo(resource, payload) {
    var layout = payloadLayout(payload);
    var format = formatForResource(resource, payload);
    var info = Codec.describeFormat(format);
    if (layout) {
      return {
        fromSidecar: true, dimension: layout.dimension || (resource.native || {}).Type || 'Texture2D',
        mip: layout.mipLevel === null || layout.mipLevel === undefined ? 0 : Number(layout.mipLevel),
        width: Number(layout.extentX || 1), height: Number(layout.extentY || 1),
        depth: Math.max(1, Number(layout.depth || 1)), zSliceCount: Math.max(1, Number(layout.zSliceCount || 1)),
        rowPitch: Number(layout.rowPitchBytes || (Number(layout.extentX || 1) * (info.bytesPerTexel || 1))),
        zSliceStride: Number(layout.zSliceStrideBytes || (Number(layout.rowPitchBytes || 0) * Number(layout.extentY || 1))),
        slice: layout.firstArraySlice, numArraySlices: layout.numArraySlices,
        face: layout.cubeFace === null || layout.cubeFace === undefined ? null : Number(layout.cubeFace),
        storageFormat: format, info: info
      };
    }
    var extent = Array.isArray(resource.extent) ? resource.extent : [1, 1, 1];
    var mip = payload && payload.mip !== null && payload.mip !== undefined ? payload.mip : 0;
    var width = Math.max(1, Math.floor((extent[0] || 1) / Math.pow(2, mip)));
    var height = Math.max(1, Math.floor((extent[1] || 1) / Math.pow(2, mip)));
    return {
      fromSidecar: false, dimension: (resource.native || {}).Type || (extent[2] > 1 ? 'Texture3D' : 'Texture2D'),
      mip: mip, width: width, height: height,
      depth: Math.max(1, Number(extent[2] || 1)), zSliceCount: 1,
      rowPitch: width * (info.bytesPerTexel || 1), zSliceStride: width * height * (info.bytesPerTexel || 1),
      slice: payload ? payload.slice : null, numArraySlices: null,
      face: payload ? payload.face : null, storageFormat: format, info: info
    };
  }

  function storageLabel(storage) {
    if (storage === null || storage === undefined) return '—';
    if (typeof storage === 'string') return storage;
    var parts = [];
    ['kind', 'format', 'strideBytes', 'pageStrideBytes', 'pageEntryStrideBytes', 'mrtSlot'].forEach(function (key) {
      if (storage[key] !== undefined && storage[key] !== null) parts.push(key + '=' + storage[key]);
    });
    if (Array.isArray(storage.formatAlternatives) && storage.formatAlternatives.length) {
      parts.push('alternatives=' + storage.formatAlternatives.join(' | '));
    }
    if (Array.isArray(storage.formatVariants) && storage.formatVariants.length) {
      parts.push('variants=' + storage.formatVariants.map(function (variant) { return variant && variant.format ? variant.format : '?'; }).join(' | '));
    }
    return parts.length ? parts.join('; ') : JSON.stringify(storage);
  }

  function payloadLabel(payload) {
    var bits = [payload.version ? 'v' + payload.version : 'no version'];
    if (payload.mip !== null && payload.mip !== undefined) bits.push('mip ' + payload.mip);
    if (payload.face !== null && payload.face !== undefined) bits.push('face ' + payload.face);
    else if (payload.slice !== null && payload.slice !== undefined) bits.push('slice ' + payload.slice);
    if (payload.subresource && payload.subresource.zSliceCount > 1) bits.push('z-slices ' + payload.subresource.zSliceCount);
    if (payload.stencil) bits.push('stencil');
    if (payload.role !== 'resource') bits.push(payload.role);
    return bits.join(' · ');
  }

  function renderBindingTable(label, refs, resource) {
    var rows = (refs || []).map(function (ref) {
      return [ref ? passLink(ref.id, ref.displayLabel || ref.name) : '—', resource.kind, ref ? ref.id : '—'];
    });
    return h('div', {}, h('h4', { text: label + ' (' + rows.length + ')' }), rows.length ? dataTable(['Pass', 'Resource kind', 'Pass id'], rows) : h('p', { class: 'muted small', text: 'No captured ' + label.toLowerCase() + ' bindings.' }));
  }

  function renderPayloadTable(record, payloads) {
    var rows = payloads.map(function (payload) {
      return [
        payload.originalPath,
        payloadLabel(payload),
        fmtBytes(payload.bytes),
        { text: shortSha(payload.sha256), class: 'mono' },
        String((payload.chunks || []).length),
        h('button', {
          type: 'button', text: 'Download raw',
          on: { click: function (event) { downloadPayload(payload, event.currentTarget); } }
        })
      ];
    });
    if (!rows.length) return h('p', { class: 'muted', text: 'No payloads.' });
    return dataTable(['Original path', 'Version / subresource', 'Length', 'SHA-256', 'Chunks', 'Raw'], rows);
  }

  function renderDownloadCard(resource, payload) {
    var card = h('div', { class: 'field-card' });
    card.appendChild(h('h4', { text: 'Raw download and lossless reconstruction' }));
    card.appendChild(h('p', { class: 'small', text: 'Payload length ' + payload.bytes + ' B. SHA-256 ' + payload.sha256 + '. Ordered chunk references are listed in the resource record.' }));
    card.appendChild(h('p', { class: 'small muted', text: 'Browser download reconstructs from chunks and verifies the payload hash when crypto.subtle is available. For payloads above ' + fmtBytes(MAX_DOWNLOAD_BYTES) + ', use the Python command; the browser will not fetch the whole payload on its own.' }));
    card.appendChild(h('pre', { class: 'small', text: 'python tools/package_frame.py reconstruct --package <package-dir> --dest <output-dir>' }));
    var row = h('div', { class: 'button-row' });
    row.appendChild(h('button', { type: 'button', text: 'Download raw payload', on: { click: function (event) { downloadPayload(payload, event.currentTarget); } } }));
    row.appendChild(h('a', { href: packageUrl('index.json'), text: 'Package index' }));
    card.appendChild(row);
    return card;
  }

  async function downloadPayload(payload, button) {
    var original = button ? button.textContent : null;
    try {
      if (payload.bytes > MAX_DOWNLOAD_BYTES) {
        throw new Error('This payload is ' + fmtBytes(payload.bytes) + '; browser download is capped at ' + fmtBytes(MAX_DOWNLOAD_BYTES) + '. Use the reconstruct command or fetch individual chunks.');
      }
      if (button) { button.disabled = true; button.textContent = 'Fetching ' + fmtBytes(payload.bytes) + '…'; }
      var bytes = await explorer.readRange(payload, 0, payload.bytes);
      if (window.crypto && window.crypto.subtle) {
        var digest = await window.crypto.subtle.digest('SHA-256', bytes);
        var actual = Array.prototype.map.call(new Uint8Array(digest), function (b) { return b.toString(16).padStart(2, '0'); }).join('');
        if (actual !== payload.sha256) throw new Error('Reconstructed SHA-256 does not match the manifest.');
      }
      var blob = new Blob([bytes], { type: 'application/octet-stream' });
      var url = URL.createObjectURL(blob);
      var a = h('a', { href: url, download: (payload.originalPath || 'payload.bin').split('/').pop() });
      document.body.appendChild(a);
      a.click();
      setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 30000);
    } catch (error) {
      alert(error.message);
    } finally {
      if (button) { button.disabled = false; button.textContent = original; }
    }
  }

  // --------------------------------------------------------------- probing
  function renderProbe(host, resource, record, payload, onContext) {
    clear(host);
    var kind = resource.kind;
    var sub = subresourceInfo(resource, payload);
    var info = sub.info;
    var layout = textureLayout(resource, payload);
    if (kind === 'texture' && sub.fromSidecar) {
      host.appendChild(renderLayoutCard(payload, sub));
    }
    if (kind === 'texture' && !sub.fromSidecar && ((resource.extent || [1, 1, 1])[2] || 1) > 1) {
      var sidecarCard = h('div', { class: 'field-card' });
      sidecarCard.appendChild(h('h4', { text: 'Volume texture layout not asserted' }));
      sidecarCard.appendChild(notice('Texture3D requires an exact sidecar', 'This capture has no SubresourceLayouts record for the selected payload. The explorer does not assume a 2D slice/row layout. Raw byte pages remain available below; no texel values are inferred.', 'warn'));
      if (payload && payload.bytes) renderHexViewer(host, payload, 0);
      host.appendChild(sidecarCard);
      return;
    }
    if (kind === 'texture' && !layout.available) {
      var unavailableCard = h('div', { class: 'field-card' });
      unavailableCard.appendChild(h('h4', { text: layout.kind + ' subresource selection (2D overview unavailable)' }));
      unavailableCard.appendChild(notice(layout.kind + ' layout requires an exact sidecar', layout.reason + ' Captured subresource payloads remain selectable; no missing bytes are claimed or inferred.', 'warn'));
      if (record.payloads && record.payloads.length) {
        var sidecarSelect = h('select', { 'aria-label': 'Captured subresource' });
        record.payloads.forEach(function (item) {
          sidecarSelect.appendChild(h('option', { value: item.id, text: payloadLabel(item) + ' · ' + fmtBytes(item.bytes) }));
        });
        sidecarSelect.value = payload ? payload.id : record.payloads[0].id;
        sidecarSelect.addEventListener('change', function () {
          var next = record.payloads.find(function (item) { return item.id === sidecarSelect.value; });
          if (next) renderProbe(host, resource, record, next, onContext);
        });
        unavailableCard.appendChild(h('label', { text: 'Captured subresource payload' })); unavailableCard.appendChild(sidecarSelect);
      } else {
        unavailableCard.appendChild(h('p', { class: 'muted small', text: 'No payload is captured for this descriptor-only resource.' }));
      }
      host.appendChild(unavailableCard);
      if (payload && payload.bytes) renderHexViewer(host, payload, 0);
      return;
    }

    var width = sub.width;
    var height = sub.height;
    var zCount = sub.zSliceCount;
    var rowPitch = sub.rowPitch;

    var resultHost = h('div');
    var hexHost = h('div');
    var previewHost = h('div');
    var bitfieldHost = h('div');
    var overviewHost = h('div');
    var card = h('div', { class: 'field-card' });
    var row = h('div', { class: 'tool-grid' });

    if (kind === 'texture' || kind === 'unknown') {
      var format = formatForResource(resource, payload);
      var channels = channelList(format);
      var xInput = h('input', { id: 'probe-x', type: 'number', min: '0', max: String(width - 1), step: '1', value: '0', 'aria-label': 'Texel x' });
      var yInput = h('input', { id: 'probe-y', type: 'number', min: '0', max: String(height - 1), step: '1', value: '0', 'aria-label': 'Texel y' });
      var zInput = h('input', { type: 'number', min: '0', max: String(Math.max(0, zCount - 1)), step: '1', value: '0', 'aria-label': 'Texel z (volume slice)' });
      if (zCount <= 1) zInput.disabled = true;
      var texelCard = h('div', { class: 'field-card' });
      texelCard.appendChild(h('h4', { text: 'Per-texel numeric read (exact)' }));
      texelCard.appendChild(h('p', { class: 'small muted', text: sub.dimension + ' · mip ' + sub.mip + ' · ' + width + ' × ' + height + (zCount > 1 ? ' × ' + zCount + ' z-slices' : '') + (sub.face !== null && sub.face !== undefined ? ' · cube face ' + sub.face : (sub.slice !== null && sub.slice !== undefined ? ' · array slice ' + sub.slice : '')) + '. Layout ' + (info.layout || 'unknown') + ', ' + (info.bytesPerTexel || '?') + ' B/texel. Row pitch ' + rowPitch + ' B, z-slice stride ' + sub.zSliceStride + ' B (recorded sidecar). Stored components: ' + storedChannelNames(format).join(', ') + '.' }));
      texelCard.appendChild(h('label', { for: 'probe-x', text: 'x (column)' })); texelCard.appendChild(xInput);
      texelCard.appendChild(h('label', { for: 'probe-y', text: 'y (row)' })); texelCard.appendChild(yInput);
      texelCard.appendChild(h('label', { text: 'z (volume slice)' })); texelCard.appendChild(zInput);
      texelCard.appendChild(h('div', { class: 'button-row' },
        h('button', { type: 'button', text: 'Read texel', on: { click: function () { readTexel(resource, payload, width, height, xInput.value, yInput.value, resultHost, hexHost, onContext, zInput.value); } } }),
        h('button', { type: 'button', text: 'Hex at texel', on: { click: function () { readHexAtTexel(resource, payload, width, height, xInput.value, yInput.value, hexHost, zInput.value); } } })
      ));
      row.appendChild(texelCard);

      var previewCard = h('div', { class: 'field-card' });
      previewCard.appendChild(h('h4', { text: 'Bounded row-strip preview' }));
      previewCard.appendChild(h('p', { class: 'small muted', text: 'Fetches only the selected rows of the selected subresource; never the whole texture. Channel selection affects this display only. Max ' + fmtBytes(MAX_PREVIEW_BYTES) + ' per request.' }));
      var startRow = h('input', { type: 'number', min: '0', max: String(Math.max(0, height - 1)), value: '0', 'aria-label': 'First preview row' });
      var rowCount = h('input', { type: 'number', min: '1', max: String(Math.min(64, height)), value: String(Math.min(16, height)), 'aria-label': 'Preview row count' });
      var previewZ = h('input', { type: 'number', min: '0', max: String(Math.max(0, zCount - 1)), value: '0', 'aria-label': 'Preview z slice' });
      if (zCount <= 1) previewZ.disabled = true;
      var previewChannel = h('select', { 'aria-label': 'Preview channel (every stored component)' });
      channels.forEach(function (item) { previewChannel.appendChild(h('option', { value: item.id, text: item.label })); });
      var transform = h('select', { 'aria-label': 'Preview display transform' });
      [['srgb', 'Clamp 0..1, then sRGB encode'], ['linear', 'Clamp 0..1, linear byte values'], ['reinhard', 'Reinhard x/(1+x), then sRGB'], ['minmax', 'Per-window min/max stretch']].forEach(function (pair) {
        transform.appendChild(h('option', { value: pair[0], text: pair[1] }));
      });
      previewCard.appendChild(h('label', { text: 'First row' })); previewCard.appendChild(startRow);
      previewCard.appendChild(h('label', { text: 'Rows (max 64)' })); previewCard.appendChild(rowCount);
      previewCard.appendChild(h('label', { text: 'z slice' })); previewCard.appendChild(previewZ);
      previewCard.appendChild(h('label', { text: 'Channel' })); previewCard.appendChild(previewChannel);
      previewCard.appendChild(h('label', { text: 'Display transform' })); previewCard.appendChild(transform);
      previewCard.appendChild(h('div', { class: 'button-row' },
        h('button', { type: 'button', text: 'Preview selected rows', on: { click: function () { previewTextureRows(resource, payload, width, height, startRow.value, rowCount.value, previewChannel.value, transform.value, previewHost, previewZ.value, sub); } } })
      ));
      row.appendChild(previewCard);

      var overviewCard = h('div', { class: 'field-card' });
      overviewCard.appendChild(h('h4', { text: 'Whole-texture overview (subresource + channel selectable)' }));
      var padding = resource.padding;
      var viewport = viewportInfo(resource);
      var paddingText;
      if (viewport.applicability === 'screen-aligned' && padding) {
        paddingText = padding.padded
          ? 'Allocation ' + (resource.allocation || [width, height]).slice(0, 2).join(' × ') + '; camera ViewRect ' + padding.validWidth + ' × ' + padding.validHeight + ' at (' + padding.left + ', ' + padding.top + '); ' + padding.bottom + ' padded row(s) below and ' + padding.right + ' padded column(s) right. Padding is labelled, not counted as scene; the crop is display-only.'
          : 'Allocation equals the recorded camera ViewRect; there is no padding to crop.';
      } else {
        paddingText = 'Viewport crop not applicable (' + viewport.applicability + (viewport.domain ? ' · ' + viewport.domain : '') + '): '
          + (viewport.reason || 'no positively identified camera screen mapping.')
          + (viewport.passViewRect ? ' Pass ViewRect ' + rectText(viewport.passViewRect) + ' is context only.' : '');
      }
      overviewCard.appendChild(h('p', { class: 'small muted', text: 'Samples the full allocation or the recorded camera view rect on an explicit lattice. Each sampled row is fetched whole and then point-sampled; unread texels are not interpolated. Cap ' + fmtBytes(MAX_PREVIEW_BYTES) + '. ' + paddingText }));
      var overviewSubresource = h('select', { 'aria-label': 'Overview subresource (version, mip, slice)' });
      (record.payloads || []).forEach(function (item) {
        overviewSubresource.appendChild(h('option', { value: item.id, text: payloadLabel(item) + ' · ' + (item.subresource ? item.subresource.dimension : 'descriptor layout') }));
      });
      overviewSubresource.value = payload.id;
      var overviewChannel = h('select', { 'aria-label': 'Overview channel (every stored component)' });
      channels.forEach(function (item) { overviewChannel.appendChild(h('option', { value: item.id, text: item.label })); });
      var defaults = defaultOverviewSteps(width, height, info.bytesPerTexel || 1, MAX_PREVIEW_BYTES);
      var overviewStepX = h('input', { type: 'number', min: '1', max: String(Math.max(1, width)), value: String(defaults.stepX), 'aria-label': 'Overview column stride' });
      var overviewStepY = h('input', { type: 'number', min: '1', max: String(Math.max(1, height)), value: String(defaults.stepY), 'aria-label': 'Overview row stride' });
      var overviewZ = h('input', { type: 'number', min: '0', max: String(Math.max(0, zCount - 1)), value: '0', 'aria-label': 'Overview z slice' });
      if (zCount <= 1) overviewZ.disabled = true;
      var overviewCrop = h('input', { type: 'checkbox', 'aria-label': 'Crop overview to recorded camera ViewRect' });
      var cropAvailable = !!(viewport.cropAllowed && padding && padding.padded);
      if (!cropAvailable) overviewCrop.disabled = true;
      overviewCrop.title = cropAvailable
        ? 'Display-only crop to the recorded camera ViewRect; no captured byte is removed.'
        : 'Crop disabled: ' + (viewport.applicability === 'screen-aligned' ? 'allocation has no padding beyond the ViewRect' : (viewport.reason || 'no positively identified camera screen mapping.'));
      var overviewTransform = h('select', { 'aria-label': 'Overview display transform' });
      [['srgb', 'sRGB encode'], ['linear', 'Linear bytes'], ['reinhard', 'Reinhard + sRGB'], ['minmax', 'Sampled min/max stretch']].forEach(function (pair) {
        overviewTransform.appendChild(h('option', { value: pair[0], text: pair[1] }));
      });
      overviewCard.appendChild(h('label', { text: 'Subresource (version / mip / slice / face)' })); overviewCard.appendChild(overviewSubresource);
      overviewCard.appendChild(h('label', { text: 'Selected z slice' })); overviewCard.appendChild(overviewZ);
      overviewCard.appendChild(h('label', { text: 'Channel' })); overviewCard.appendChild(overviewChannel);
      overviewCard.appendChild(h('label', { text: 'Column stride (every N columns)' })); overviewCard.appendChild(overviewStepX);
      overviewCard.appendChild(h('label', { text: 'Row stride (every N rows)' })); overviewCard.appendChild(overviewStepY);
      var cropLabel = h('label', { text: cropAvailable ? 'Crop to recorded camera ViewRect (valid view only)' : 'Crop to ViewRect disabled for this resource' });
      overviewCard.appendChild(cropLabel); overviewCard.appendChild(overviewCrop);
      overviewCard.appendChild(h('label', { text: 'Display transform' })); overviewCard.appendChild(overviewTransform);
      overviewCard.appendChild(h('div', { class: 'button-row' },
        h('button', { type: 'button', text: 'Render sampled overview', on: { click: function () {
          var target = (record.payloads || []).find(function (item) { return item.id === overviewSubresource.value; });
          if (!target) { clear(overviewHost); overviewHost.appendChild(notice('Overview unavailable', 'The selected subresource is not present in this record.', 'warn')); return; }
          previewTextureOverview(resource, target, overviewChannel.value, overviewStepX.value, overviewStepY.value, overviewCrop.checked, overviewTransform.value, overviewHost, overviewZ.value);
        } } })
      ));
      row.appendChild(overviewCard);

      var bitfieldCard = h('div', { class: 'field-card' });
      bitfieldCard.appendChild(h('h4', { text: 'Packed bitfield / decode view (distinct from channel preview)' }));
      bitfieldCard.appendChild(h('p', { class: 'small muted', text: 'Reads the exact texel at x/y and shows each stored component bit range, raw bits and decoded value. No display transform is applied and this view never substitutes for the pixel numeric inspector.' }));
      bitfieldCard.appendChild(h('div', { class: 'button-row' },
        h('button', { type: 'button', text: 'Show exact bitfield / decode', on: { click: function () { readTexel(resource, payload, width, height, xInput.value, yInput.value, bitfieldHost, null, onContext); } } })
      ));
      row.appendChild(bitfieldCard);
    } else {
      var stride = Number(resource.bytesPerElement || 0);
      var count = Number(resource.numElements || 0);
      var elementCard = h('div', { class: 'field-card' });
      elementCard.appendChild(h('h4', { text: 'Per-element numeric read' }));
      elementCard.appendChild(h('p', { class: 'small muted', text: stride ? stride + ' B/element × ' + count + ' element(s). Typed fields require captured structure metadata or a schema field with an explicit element offset basis.' : 'Bytes-per-element is not declared; typed indexing is unavailable.' }));
      var indexInput = h('input', { type: 'number', min: '0', max: String(Math.max(0, count - 1)), value: '0', 'aria-label': 'Buffer element index' });
      elementCard.appendChild(h('label', { text: 'Element index' })); elementCard.appendChild(indexInput);
      elementCard.appendChild(h('div', { class: 'button-row' },
        h('button', { type: 'button', text: 'Read element', on: { click: function () { readBufferElement(resource, payload, stride, count, indexInput.value, resultHost, hexHost, onContext); } } }),
        h('button', { type: 'button', text: 'Hex at element', on: { click: function () { readHexAtBufferElement(resource, payload, stride, count, indexInput.value, hexHost); } } })
      ));
      row.appendChild(elementCard);

      var previewBuffer = h('div', { class: 'field-card' });
      previewBuffer.appendChild(h('h4', { text: 'Bounded element preview' }));
      previewBuffer.appendChild(h('p', { class: 'small muted', text: 'Reads one contiguous bounded element window and shows decoded fields when metadata permits. Max ' + fmtBytes(MAX_PREVIEW_BYTES) + '.' }));
      var bufTransform = h('select', { 'aria-label': 'Buffer preview display transform' });
      [['minmax', 'Per-column min/max stretch (display only)'], ['raw', 'Raw numeric values in table']].forEach(function (pair) {
        bufTransform.appendChild(h('option', { value: pair[0], text: pair[1] }));
      });
      var bufStart = h('input', { type: 'number', min: '0', max: String(Math.max(0, count - 1)), value: '0', 'aria-label': 'First preview element' });
      var bufCount = h('input', { type: 'number', min: '1', max: '64', value: String(Math.min(16, count || 1)), 'aria-label': 'Preview element count' });
      previewBuffer.appendChild(h('label', { text: 'First element' })); previewBuffer.appendChild(bufStart);
      previewBuffer.appendChild(h('label', { text: 'Elements (max 64)' })); previewBuffer.appendChild(bufCount);
      previewBuffer.appendChild(h('label', { text: 'Display transform' })); previewBuffer.appendChild(bufTransform);
      previewBuffer.appendChild(h('div', { class: 'button-row' },
        h('button', { type: 'button', text: 'Preview elements', on: { click: function () { previewBufferElements(resource, payload, stride, count, bufStart.value, bufCount.value, bufTransform.value, previewHost); } } })
      ));
      row.appendChild(previewBuffer);
    }
    card.appendChild(row);
    host.appendChild(card);
    host.appendChild(resultHost);
    host.appendChild(hexHost);
    host.appendChild(bitfieldHost);
    host.appendChild(overviewHost);
    host.appendChild(previewHost);

  }

  function renderLayoutCard(payload, sub) {
    var layout = payload.layout || {};
    var completion = payload.completion || {};
    var card = h('div', { class: 'field-card' });
    var badges = [badge(sub.dimension || 'texture'), badge('mip ' + sub.mip), badge('stored ' + (layout.StoredFormat || 'unknown'), 'accent')];
    if (layout.NativeFormat !== layout.StoredFormat) badges.push(badge('native ' + (layout.NativeFormat || '?'), 'warn'));
    if (layout.BitExact === true) badges.push(badge('bit exact', 'ok'));
    else if (layout.BitExact === false) badges.push(badge('not bit exact', 'warn'));
    if (layout.ValuePreserving === true) badges.push(badge('value preserving', 'ok'));
    else if (layout.ValuePreserving === false) badges.push(badge('not value preserving', 'danger'));
    if (completion.dumped) badges.push(badge('completion: Dumped / ' + (completion.timing || 'timing not recorded'), 'ok'));
    else badges.push(badge('completion unobserved', 'danger'));
    card.appendChild(h('h4', { text: 'Exact subresource layout (SubresourceLayouts sidecar)' }));
    card.appendChild(h('div', { class: 'badge-row' }, badges));
    card.appendChild(kvGrid([
      ['File', payload.originalPath],
      ['Dimension', layout.Dimension || '—'],
      ['MipLevel', String(layout.MipLevel)],
      ['FirstArraySlice', String(layout.FirstArraySlice)],
      ['NumArraySlices', String(layout.NumArraySlices)],
      ['CubeFace', layout.CubeFace === null || layout.CubeFace === undefined ? 'not a cube face / not recorded' : String(layout.CubeFace)],
      ['Extent', (layout.ExtentX || 0) + ' × ' + (layout.ExtentY || 0) + ' × ' + (layout.Depth || 1)],
      ['Depth / ZSliceCount', String(layout.Depth) + ' / ' + String(layout.ZSliceCount)],
      ['ZSliceOrder', layout.ZSliceOrder || '—'],
      ['ZSliceStrideBytes', String(layout.ZSliceStrideBytes)],
      ['RowPitchBytes', String(layout.RowPitchBytes)],
      ['NativeFormat', layout.NativeFormat || '—'],
      ['ViewFormat', layout.ViewFormat || '—'],
      ['StagingFormat', layout.StagingFormat || '—'],
      ['StoredFormat (authoritative)', layout.StoredFormat || '—'],
      ['NativeFormatExpanded', String(layout.NativeFormatExpanded)],
      ['NativeSRGB / ViewSRGB / override', String(layout.NativeSRGB) + ' / ' + String(layout.ViewSRGB) + ' / ' + (layout.SRGBOverride || '—')],
      ['SRGBViewUsedForReadback', String(layout.SRGBViewUsedForReadback)],
      ['StoredBytesAreNativeStorage', String(layout.StoredBytesAreNativeStorage)],
      ['NativeValueInterpretation', layout.NativeValueInterpretation || '—'],
      ['BitExact / ValuePreserving', String(layout.BitExact) + ' / ' + String(layout.ValuePreserving)],
      ['BitExactCondition', layout.BitExactCondition || '—'],
      ['StencilSubresource', String(layout.StencilSubresource)],
      ['ByteSize', fmtBytes(layout.ByteSize) + ' (' + layout.ByteSize + ' B)'],
      ['StagingByteSize', String(layout.StagingByteSize)],
      ['MipExtentClampedToMinimum', String(layout.MipExtentClampedToMinimum)],
      ['Readback', layout.Readback || '—'],
      ['Scheduled status', layout.DumpStatus || '—'],
      ['FileVerifiedAtSchedule', String(layout.FileVerifiedAtSchedule)],
      ['Completion Status / Timing', (completion.observed ? String(completion.dumped) + ' / ' + (completion.timing || '—') : 'unobserved')],
      ['Expected / Actual bytes', String(completion.expectedBytes) + ' / ' + String(completion.actualBytes)],
      ['FileExists / FileSizeMatches', String(completion.fileExists) + ' / ' + String(completion.fileSizeMatches)],
      ['Completion note', completion.reason || 'Dumped with GPUReadbackCompleted; scheduling alone is not treated as success.']
    ]));
    if (layout.NativeFormat !== layout.StoredFormat) {
      card.appendChild(notice('Native vs decoded storage', 'Native format ' + (layout.NativeFormat || '?') + ' was exported as stored format ' + (layout.StoredFormat || '?') + '. Numeric reads use ' + (layout.StoredFormat || 'the stored format') + '. ' + (layout.BitExactCondition || ''), 'warn'));
    }
    if (layout.ValuePreserving === false) {
      card.appendChild(notice('Values are not preserved', layout.BitExactCondition || 'The exporter records ValuePreserving=false; decoded numbers are not the original native values.', 'danger'));
    }
    card.appendChild(details('Complete SubresourceLayouts record (all fields, including unknown)', h('pre', { class: 'json', text: JSON.stringify(layout, null, 2) })));
    card.appendChild(details('Completion record (DumpReadbackStatus join)', h('pre', { class: 'json', text: JSON.stringify(completion, null, 2) })));
    return card;
  }

  function parseIntegerInput(value, label, min, max) {
    var n = Number(value);
    if (!Number.isInteger(n)) throw new Error(label + ' must be an integer.');
    if (n < min || n > max) throw new Error(label + ' ' + n + ' is outside ' + min + '..' + max + '.');
    return n;
  }

  async function readTexel(resource, payload, width, height, xRaw, yRaw, resultHost, hexHost, onContext, zRaw) {
    clear(resultHost);
    try {
      var sub = subresourceInfo(resource, payload);
      var info = sub.info;
      var z = parseIntegerInput(zRaw === undefined ? 0 : zRaw, 'z', 0, Math.max(0, sub.zSliceCount - 1));
      if (!info.supported || !info.bytesPerTexel) {
        resultHost.appendChild(notice('No semantic texel decoder', info.gap || 'Format does not identify a supported layout.', 'danger'));
        return;
      }
      var offset;
      if (payload && payload.layout) {
        var calc = Codec.texelOffset(payload.layout, Number(xRaw), Number(yRaw), z, info.bytesPerTexel, payload.bytes);
        if (!calc.ok) throw new Error(calc.error);
        offset = calc.offset;
      } else {
        var x = parseIntegerInput(xRaw, 'x', 0, width - 1);
        var y = parseIntegerInput(yRaw, 'y', 0, height - 1);
        offset = (y * width + x) * info.bytesPerTexel;
        if (offset + info.bytesPerTexel > payload.bytes) throw new Error('Texel range exceeds the captured payload.');
      }
      var bytes = await explorer.readRange(payload, offset, info.bytesPerTexel);
      var decoded = Codec.decodeTexel(bytes, formatForResource(resource, payload));
      var context = { kind: 'texture', payload: payload, x: Number(xRaw), y: Number(yRaw), z: z, width: width, height: height, texelOffset: offset, stride: info.bytesPerTexel, subresource: sub };
      if (onContext) onContext(context);
      resultHost.appendChild(h('h4', { text: 'Texel (' + xRaw + ', ' + yRaw + (sub.zSliceCount > 1 ? ', z=' + z : '') + ') at byte ' + offset }));
      resultHost.appendChild(h('p', { class: 'small muted', text: sub.dimension + ' subresource mip ' + sub.mip + ' · ' + width + ' × ' + height + (sub.zSliceCount > 1 ? ' × ' + sub.zSliceCount : '') + '. Read ' + bytes.length + ' B from ' + (payload.chunks || []).length + ' ordered chunk reference(s). Stored as ' + (sub.storageFormat || 'unknown') + '; no display transfer is applied to the table.' }));
      resultHost.appendChild(renderDecodedTable(decoded));
      if (decoded.gap) resultHost.appendChild(notice('Decode gap', decoded.gap, 'warn'));
      resultHost.appendChild(h('details', { open: true }, h('summary', { text: 'Raw bytes at this texel' }), h('pre', { class: 'hex', text: Codec.hexDump(bytes, offset, 16).map(function (line) { return line.address.toString(16).padStart(8, '0') + '  ' + line.hex + '  ' + line.ascii; }).join('\n') })));
      if (hexHost) renderHexViewer(hexHost, payload, offset);
    } catch (error) {
      resultHost.appendChild(notice('Texel read failed', error.message, 'danger'));
    }
  }

  async function readBufferElement(resource, payload, stride, count, indexRaw, resultHost, hexHost, onContext) {
    clear(resultHost);
    try {
      if (!stride || !count) throw new Error('Buffer element stride/count is not declared; typed indexing would require a guess.');
      var index = parseIntegerInput(indexRaw, 'element index', 0, Math.max(0, count - 1));
      var offset = index * stride;
      if (offset + stride > payload.bytes) throw new Error('Element range exceeds the captured payload.');
      var bytes = await explorer.readRange(payload, offset, stride);
      var context = { kind: 'buffer', payload: payload, index: index, elementOffset: offset, stride: stride };
      if (onContext) onContext(context);
      resultHost.appendChild(h('h4', { text: 'Buffer element ' + index + ' at byte ' + offset }));
      var entries = flattenStructure(resource.bufferMetadata ? String(resource.metadataPointer || '') : '', 0, '', 0, new Set());
      if (entries.length) {
        resultHost.appendChild(renderMetadataValues(entries, bytes, 'Element'));
      } else {
        resultHost.appendChild(notice('No typed buffer fields', 'The buffer has no captured structure metadata and no source schema field with an element offset basis. Raw bytes are shown; no float/integer interpretation is guessed.', 'warn'));
      }
      resultHost.appendChild(h('p', { class: 'small muted', text: 'Read ' + bytes.length + ' B at offset ' + offset + ' from the selected payload version.' }));
      resultHost.appendChild(h('details', { open: true }, h('summary', { text: 'Raw element bytes' }), h('pre', { class: 'hex', text: Codec.hexDump(bytes, offset, 16).map(function (line) { return line.address.toString(16).padStart(8, '0') + '  ' + line.hex + '  ' + line.ascii; }).join('\n') })));
      if (hexHost) renderHexViewer(hexHost, payload, offset);
    } catch (error) {
      resultHost.appendChild(notice('Element read failed', error.message, 'danger'));
    }
  }

  function renderDecodedTable(decoded) {
    if (!decoded.supported) {
      return notice('Decode unavailable', decoded.gap || 'No decoder.', 'danger');
    }
    var rows = decoded.channels.map(function (ch) {
      return [ch.name, ch.type, String(ch.bits), String(ch.raw), ch.rawHex || '', fmtNumber(ch.value)];
    });
    var wrap = h('div');
    wrap.appendChild(dataTable(['Channel', 'Type', 'Bits', 'Raw', 'Raw hex', 'Value (unmodified)'], rows, { class: 'value-table' }));
    if (decoded.wordHex) wrap.appendChild(h('p', { class: 'small muted', text: 'Packed word: ' + decoded.wordHex }));
    return wrap;
  }

  async function readHexAtTexel(resource, payload, width, height, xRaw, yRaw, hexHost, zRaw) {
    clear(hexHost);
    try {
      var sub = subresourceInfo(resource, payload);
      var info = sub.info;
      var offset;
      if (payload && payload.layout) {
        var calc = Codec.texelOffset(payload.layout, Number(xRaw), Number(yRaw), Number(zRaw || 0), info.bytesPerTexel, payload.bytes);
        if (!calc.ok) throw new Error(calc.error);
        offset = calc.offset;
      } else {
        var x = parseIntegerInput(xRaw, 'x', 0, width - 1);
        var y = parseIntegerInput(yRaw, 'y', 0, height - 1);
        if (!info.bytesPerTexel) throw new Error('No explicit bytes-per-texel for this format.');
        offset = (y * width + x) * info.bytesPerTexel;
      }
      renderHexViewer(hexHost, payload, offset);
    } catch (error) {
      hexHost.appendChild(notice('Hex read failed', error.message, 'danger'));
    }
  }

  async function readHexAtBufferElement(resource, payload, stride, count, indexRaw, hexHost) {
    clear(hexHost);
    try {
      if (!stride || !count) throw new Error('Buffer stride/count is not declared.');
      var index = parseIntegerInput(indexRaw, 'element index', 0, Math.max(0, count - 1));
      renderHexViewer(hexHost, payload, index * stride);
    } catch (error) {
      hexHost.appendChild(notice('Hex read failed', error.message, 'danger'));
    }
  }

  // ---------------------------------------------------------------- hex view
  function renderHexViewer(host, payload, initialOffset) {
    clear(host);
    if (!payload || !payload.bytes) { host.appendChild(h('p', { class: 'muted', text: 'No payload bytes.' })); return; }
    var pageSize = 256;
    var offset = Math.max(0, Math.min(Number(initialOffset) || 0, Math.max(0, payload.bytes - 1)));
    var output = h('div', { class: 'hex-view', tabindex: '0', role: 'region', 'aria-label': 'Raw byte hex view' });
    var status = h('p', { class: 'small muted' });
    var offsetInput = h('input', { type: 'number', min: '0', max: String(Math.max(0, payload.bytes - 1)), value: String(offset), 'aria-label': 'Hex byte offset' });
    var pageSizeSelect = h('select', { 'aria-label': 'Hex page size' });
    [64, 128, 256, 512, 1024].forEach(function (n) { pageSizeSelect.appendChild(h('option', { value: String(n), text: n + ' B' })); });
    pageSizeSelect.value = '256';
    function read() {
      clear(output);
      output.appendChild(h('p', { class: 'loading', text: 'Fetching required gzip chunk(s)…' }));
      var start = Math.max(0, Math.min(Number(offsetInput.value) || 0, payload.bytes));
      var size = Number(pageSizeSelect.value) || pageSize;
      var length = Math.min(size, payload.bytes - start);
      explorer.readRange(payload, start, length).then(function (bytes) {
        clear(output);
        var pre = h('pre', { class: 'hex' });
        Codec.hexDump(bytes, start, 16).forEach(function (line) {
          pre.appendChild(h('span', { class: 'hex-line' },
            h('span', { class: 'hex-address', text: line.address.toString(16).padStart(8, '0') }),
            '  ' + line.hex + '  ',
            h('span', { class: 'hex-ascii', text: line.ascii })
          ));
        });
        output.appendChild(pre);
        status.textContent = 'Showing bytes ' + start + '..' + (start + length) + ' of ' + payload.bytes + ' (page ' + length + ' B). Only chunks covering this range were fetched.';
      }).catch(function (error) {
        clear(output);
        output.appendChild(notice('Hex page failed', error.message, 'danger'));
        status.textContent = '';
      });
    }
    var controls = h('div', { class: 'button-row' },
      h('button', { type: 'button', text: 'Previous page', on: { click: function () { offsetInput.value = String(Math.max(0, (Number(offsetInput.value) || 0) - (Number(pageSizeSelect.value) || 256))); read(); } } }),
      h('button', { type: 'button', text: 'Next page', on: { click: function () { offsetInput.value = String(Math.min(Math.max(0, payload.bytes - 1), (Number(offsetInput.value) || 0) + (Number(pageSizeSelect.value) || 256))); read(); } } }),
      h('button', { type: 'button', text: 'Read page', on: { click: read } }),
      offsetInput,
      pageSizeSelect
    );
    host.appendChild(h('h4', { text: 'Byte / hex view' }));
    host.appendChild(controls);
    host.appendChild(output);
    host.appendChild(status);
    read();
  }

  // ------------------------------------------------------------- preview
  function displayTransform(kind, x) {
    if (!Number.isFinite(x)) return 0;
    var v = Math.max(0, Math.min(1, x));
    if (kind === 'linear') return Math.round(v * 255);
    if (kind === 'srgb') {
      var s = v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
      return Math.round(Math.max(0, Math.min(1, s)) * 255);
    }
    if (kind === 'reinhard') {
      var r = v / (1 + v);
      var rs = r <= 0.0031308 ? 12.92 * r : 1.055 * Math.pow(r, 1 / 2.4) - 0.055;
      return Math.round(Math.max(0, Math.min(1, rs)) * 255);
    }
    return Math.round(v * 255);
  }

  function transformDescription(kind) {
    if (kind === 'linear') return 'clamp(value,0,1) × 255 (no gamma encoding)';
    if (kind === 'srgb') return 'sRGBEncode(clamp(value,0,1)) × 255';
    if (kind === 'reinhard') return 'sRGBEncode(x/(1+x)) × 255, where x = max(0, value)';
    return 'per-channel min/max stretch of this bounded preview window (display only)';
  }

  function storedChannelNames(format) {
    return Codec.channelNames(format);
  }

  function channelList(format) {
    var names = storedChannelNames(format);
    var list = [{ id: 'rgb', label: names.length > 1 ? 'RGB composite' : (names[0] || 'scalar') }];
    names.forEach(function (name) {
      list.push({ id: name, label: names.length === 1 ? 'scalar (' + name + ')' : name });
    });
    return list;
  }

  function textureLayout(record, payload) {
    var layout = payloadLayout(payload);
    if (layout && layout.dimension) {
      return {
        kind: layout.dimension, available: true, reason: null,
        sidecar: true, zSlices: Math.max(1, Number(layout.zSliceCount || 1)),
        face: layout.cubeFace, slice: layout.firstArraySlice
      };
    }
    var native = record.native || {};
    var type = String(native.Type || '');
    var extent = record.extent || [1, 1, 1];
    if (/cube/i.test(type)) {
      return {
        kind: 'TextureCube', available: false,
        reason: 'TextureCube face subresources require a SubresourceLayouts record; no 2D face layout is asserted here.'
      };
    }
    if (/3d|volume/i.test(type) || (Number(extent[2]) || 1) > 1) {
      return {
        kind: 'Texture3D', available: false,
        reason: 'Texture3D/volume subresources require a SubresourceLayouts record; no 2D slice layout is asserted here.'
      };
    }
    if (type === 'Texture2DArray' || (Number(record.arraySize) || 1) > 1) {
      return { kind: 'Texture2DArray', available: true, reason: null };
    }
    return { kind: 'Texture2D', available: true, reason: null };
  }

  function planOverview(width, height, bytesPerTexel, stepX, stepY, cropRect, cap, rowPitch) {
    width = Math.max(1, Math.floor(Number(width) || 1));
    height = Math.max(1, Math.floor(Number(height) || 1));
    bytesPerTexel = Math.max(1, Math.floor(Number(bytesPerTexel) || 1));
    rowPitch = Math.max(width * bytesPerTexel, Math.floor(Number(rowPitch) || (width * bytesPerTexel)));
    cap = Math.max(1, Math.floor(Number(cap) || MAX_PREVIEW_BYTES));
    var x0 = 0, y0 = 0, validWidth = width, validHeight = height;
    if (cropRect && Array.isArray(cropRect.min) && Array.isArray(cropRect.size)) {
      x0 = Math.max(0, Math.min(width, Math.floor(Number(cropRect.min[0]) || 0)));
      y0 = Math.max(0, Math.min(height, Math.floor(Number(cropRect.min[1]) || 0)));
      validWidth = Math.max(0, Math.min(width - x0, Math.floor(Number(cropRect.size[0]) || 0)));
      validHeight = Math.max(0, Math.min(height - y0, Math.floor(Number(cropRect.size[1]) || 0)));
    }
    stepX = Math.max(1, Math.floor(Number(stepX) || 1));
    stepY = Math.max(1, Math.floor(Number(stepY) || 1));
    var cols = Math.ceil(validWidth / stepX);
    var rows = Math.ceil(validHeight / stepY);
    var rowBytes = validWidth * bytesPerTexel;
    var fetchBytes = rowBytes * rows;
    var result = {
      available: true, error: null, width: width, height: height, bytesPerTexel: bytesPerTexel,
      x0: x0, y0: y0, validWidth: validWidth, validHeight: validHeight,
      stepX: stepX, stepY: stepY, cols: cols, rows: rows,
      rowBytes: rowBytes, rowPitch: rowPitch, fetchBytes: fetchBytes, cap: cap, cropped: x0 !== 0 || y0 !== 0 || validWidth !== width || validHeight !== height
    };
    if (!validWidth || !validHeight) {
      result.available = false;
      result.error = 'The selected view rect is empty inside the allocation; no bytes are invented.';
    } else if (rowBytes > cap) {
      result.available = false;
      result.error = 'One sampled row is ' + fmtBytes(rowBytes) + '; the cap is ' + fmtBytes(cap)
        + '. Increase the column stride or select a smaller mip; the overview is unavailable rather than fabricated.';
    } else if (fetchBytes > cap) {
      result.available = false;
      result.error = 'This sampling lattice needs ' + fmtBytes(fetchBytes) + '; the cap is ' + fmtBytes(cap)
        + '. Increase the row stride (currently every ' + stepY + ' row(s)).';
    }
    return result;
  }

  function defaultOverviewSteps(width, height, bytesPerTexel, cap) {
    var rowBytes = Math.max(1, width * bytesPerTexel);
    var maxRows = Math.max(1, Math.floor(cap / rowBytes));
    return {
      stepX: Math.max(1, Math.ceil(width / 512)),
      stepY: Math.max(1, Math.ceil(height / maxRows))
    };
  }

  async function previewTextureRows(resource, payload, width, height, startRaw, countRaw, channel, transform, host, zRaw, subOverride) {
    clear(host);
    try {
      var sub = subOverride || subresourceInfo(resource, payload);
      var info = sub.info;
      if (!info.supported || !info.bytesPerTexel) throw new Error(info.gap || 'No explicit texel layout.');
      var start = parseIntegerInput(startRaw, 'first row', 0, Math.max(0, height - 1));
      var count = parseIntegerInput(countRaw, 'row count', 1, Math.min(64, height - start));
      var z = parseIntegerInput(zRaw === undefined ? 0 : zRaw, 'z slice', 0, Math.max(0, sub.zSliceCount - 1));
      var rowBytes = width * info.bytesPerTexel;
      if (rowBytes * count > MAX_PREVIEW_BYTES) throw new Error('Selected row window is ' + fmtBytes(rowBytes * count) + '; cap is ' + fmtBytes(MAX_PREVIEW_BYTES) + '. Reduce rows or read individual texels.');
      var bytes = new Uint8Array(rowBytes * count);
      var fetched = 0;
      for (var r = 0; r < count; r++) {
        var rowOffset = z * sub.zSliceStride + (start + r) * sub.rowPitch;
        if (!Number.isSafeInteger(rowOffset) || rowOffset + rowBytes > payload.bytes) {
          throw new Error('Row ' + (start + r) + ' range is outside the captured payload; the recorded pitch/stride is authoritative and is not repaired.');
        }
        var rowData = await explorer.readRange(payload, rowOffset, rowBytes);
        bytes.set(rowData, r * rowBytes);
        fetched += rowData.length;
      }
      var channelName = channel || 'rgb';
      var channelIndex = storedChannelNames(formatForResource(resource, payload)).indexOf(channelName);
      if (channelName !== 'rgb' && channelIndex === -1) throw new Error('Channel ' + channelName + ' is not stored by this format.');
      var canvas = h('canvas', { class: 'preview-canvas', width: width, height: count, role: 'img', 'aria-label': 'Preview of rows ' + start + ' through ' + (start + count - 1) + ' of ' + height + (sub.zSliceCount > 1 ? ', z slice ' + z : '') + '; channel ' + channelName + '; display transform ' + transformDescription(transform) });
      var ctx = canvas.getContext('2d');
      if (!ctx) throw new Error('Canvas 2D context unavailable.');
      var image = ctx.createImageData(width, count);
      var minmax = [[Infinity, -Infinity], [Infinity, -Infinity], [Infinity, -Infinity]];
      var values = new Array(width * count);
      for (var p = 0; p < width * count; p++) {
        var decoded = Codec.decodeTexel(bytes.subarray(p * info.bytesPerTexel, (p + 1) * info.bytesPerTexel), formatForResource(resource, payload));
        values[p] = decoded.values;
        if (decoded.supported) {
          var chans = channelName === 'rgb' ? [0, 1, 2] : [channelIndex];
          for (var ci = 0; ci < chans.length; ci++) {
            var v = decoded.values[Math.min(chans[ci], decoded.values.length - 1)];
            if (!Number.isFinite(v)) continue;
            minmax[ci][0] = Math.min(minmax[ci][0], v);
            minmax[ci][1] = Math.max(minmax[ci][1], v);
          }
        }
      }
      var tf = transform;
      for (var i = 0; i < width * count; i++) {
        var dec = values[i] || [];
        var rgb = [0, 0, 0];
        if (channelName === 'rgb') {
          for (var c = 0; c < 3; c++) {
            var value = Number(dec[Math.min(c, Math.max(0, dec.length - 1))]);
            if (!Number.isFinite(value)) value = 0;
            if (tf === 'minmax') {
              var lo = minmax[c][0], hi = minmax[c][1];
              var t = hi > lo ? (value - lo) / (hi - lo) : 0;
              rgb[c] = Math.round(Math.max(0, Math.min(1, t)) * 255);
            } else {
              rgb[c] = displayTransform(tf, value);
            }
          }
        } else {
          var scalar = Number(dec[channelIndex]);
          if (!Number.isFinite(scalar)) scalar = 0;
          var gray;
          if (tf === 'minmax') {
            var slo = minmax[0][0], shi = minmax[0][1];
            gray = Math.round(Math.max(0, Math.min(1, shi > slo ? (scalar - slo) / (shi - slo) : 0)) * 255);
          } else {
            gray = displayTransform(tf, scalar);
          }
          rgb = [gray, gray, gray];
        }
        var idx = i * 4;
        image.data[idx] = rgb[0]; image.data[idx + 1] = rgb[1]; image.data[idx + 2] = rgb[2]; image.data[idx + 3] = 255;
      }
      ctx.putImageData(image, 0, 0);
      host.appendChild(h('h4', { text: 'Texture preview (bounded window, channel-selected)' }));
      host.appendChild(canvas);
      host.appendChild(h('p', { class: 'preview-caption', text: sub.dimension + ' · ' + (sub.face !== null && sub.face !== undefined ? 'cube face ' + sub.face : (sub.slice !== null && sub.slice !== undefined ? 'slice ' + sub.slice : 'single slice')) + (sub.zSliceCount > 1 ? ' · z ' + z + ' of ' + sub.zSliceCount : '') + '. Channel: ' + channelName + ' (stored components: ' + storedChannelNames(formatForResource(resource, payload)).join(', ') + '). Rows ' + start + '..' + (start + count - 1) + ' of ' + height + ' (' + count + ' of ' + height + ' rows, ' + (width * count) + ' of ' + (width * height) + ' texels). Fetched ' + fmtBytes(fetched) + ' at recorded row pitch ' + sub.rowPitch + ' B. Display transform: ' + transformDescription(tf) + '. This preview is separate from the exact per-texel numeric inspector and its packed bitfield table.' }));
    } catch (error) {
      host.appendChild(notice('Preview unavailable', error.message, 'danger'));
    }
  }

  async function previewTextureOverview(resource, payload, channel, stepXRaw, stepYRaw, crop, transform, host, zRaw) {
    clear(host);
    try {
      var sub = subresourceInfo(resource, payload);
      var layout = textureLayout(resource, payload);
      if (!layout.available) throw new Error(layout.reason || 'Texture layout is unavailable; no overview is inferred.');
      var info = sub.info;
      if (!info.supported || !info.bytesPerTexel) throw new Error(info.gap || 'No explicit texel layout.');
      var width = sub.width;
      var height = sub.height;
      var z = parseIntegerInput(zRaw === undefined ? 0 : zRaw, 'z slice', 0, Math.max(0, sub.zSliceCount - 1));
      var viewport = viewportInfo(resource);
      var cropRect = null;
      if (crop) {
        if (!viewport.cropAllowed || !resource.padding || !resource.padding.padded) {
          throw new Error('Viewport crop is not applicable for this resource: '
            + (viewport.reason || (viewport.applicability === 'screen-aligned'
              ? 'the allocation has no padding beyond the recorded ViewRect.'
              : 'no positively identified camera screen mapping.')));
        }
        if (!viewport.passViewRect || !Array.isArray(viewport.passViewRect.min) || !Array.isArray(viewport.passViewRect.size)) {
          throw new Error('The camera pass has no recorded ViewRect; padding cannot be cropped without inventing a rect.');
        }
        cropRect = viewport.passViewRect;
      }
      var defaults = defaultOverviewSteps(width, height, info.bytesPerTexel, MAX_PREVIEW_BYTES);
      var stepX = stepXRaw === '' || stepXRaw === null || stepXRaw === undefined ? defaults.stepX : parseIntegerInput(stepXRaw, 'column stride', 1, width);
      var stepY = stepYRaw === '' || stepYRaw === null || stepYRaw === undefined ? defaults.stepY : parseIntegerInput(stepYRaw, 'row stride', 1, height);
      var plan = planOverview(width, height, info.bytesPerTexel, stepX, stepY, cropRect, MAX_PREVIEW_BYTES, sub.rowPitch);
      if (!plan.available) throw new Error(plan.error);
      var channelName = channel || 'rgb';
      var names = storedChannelNames(formatForResource(resource, payload));
      var channelIndex = names.indexOf(channelName);
      if (channelName !== 'rgb' && channelIndex === -1) throw new Error('Channel ' + channelName + ' is not stored by this format.');
      var rowBytes = plan.validWidth * info.bytesPerTexel;
      var canvas = h('canvas', { class: 'preview-canvas', width: plan.cols, height: plan.rows, role: 'img', 'aria-label': 'Whole-subresource overview sampled every ' + plan.stepX + ' column and ' + plan.stepY + ' row; channel ' + channelName + (sub.zSliceCount > 1 ? '; z slice ' + z : '') });
      var ctx = canvas.getContext('2d');
      if (!ctx) throw new Error('Canvas 2D context unavailable.');
      var image = ctx.createImageData(plan.cols, plan.rows);
      var minmax = [[Infinity, -Infinity], [Infinity, -Infinity], [Infinity, -Infinity]];
      var decodedRows = [];
      var fetchedBytes = 0;
      for (var row = 0; row < plan.rows; row++) {
        var y = plan.y0 + row * plan.stepY;
        var rowOffset = z * sub.zSliceStride + y * sub.rowPitch + plan.x0 * info.bytesPerTexel;
        if (!Number.isSafeInteger(rowOffset) || rowOffset + rowBytes > payload.bytes) {
          throw new Error('Sampled row ' + y + ' range is outside the captured payload; the recorded pitch/stride is authoritative.');
        }
        var rowData = await explorer.readRange(payload, rowOffset, rowBytes);
        fetchedBytes += rowData.length;
        var rowValues = [];
        for (var col = 0; col < plan.cols; col++) {
          var x = col * plan.stepX;
          var texelOffset = x * info.bytesPerTexel;
          var decoded = Codec.decodeTexel(rowData.subarray(texelOffset, texelOffset + info.bytesPerTexel), formatForResource(resource, payload));
          rowValues.push(decoded.values);
          if (decoded.supported) {
            var chans = channelName === 'rgb' ? [0, 1, 2] : [channelIndex];
            for (var ci = 0; ci < chans.length; ci++) {
              var v = decoded.values[Math.min(chans[ci], decoded.values.length - 1)];
              if (!Number.isFinite(v)) continue;
              minmax[ci][0] = Math.min(minmax[ci][0], v);
              minmax[ci][1] = Math.max(minmax[ci][1], v);
            }
          }
        }
        decodedRows.push(rowValues);
      }
      for (var r = 0; r < plan.rows; r++) {
        for (var c2 = 0; c2 < plan.cols; c2++) {
          var dec = decodedRows[r][c2] || [];
          var rgb = [0, 0, 0];
          if (channelName === 'rgb') {
            for (var cc = 0; cc < 3; cc++) {
              var value = Number(dec[Math.min(cc, Math.max(0, dec.length - 1))]);
              if (!Number.isFinite(value)) value = 0;
              if (transform === 'minmax') {
                var lo = minmax[cc][0], hi = minmax[cc][1];
                rgb[cc] = Math.round(Math.max(0, Math.min(1, hi > lo ? (value - lo) / (hi - lo) : 0)) * 255);
              } else {
                rgb[cc] = displayTransform(transform, value);
              }
            }
          } else {
            var scalar = Number(dec[channelIndex]);
            if (!Number.isFinite(scalar)) scalar = 0;
            var gray;
            if (transform === 'minmax') {
              var slo = minmax[0][0], shi = minmax[0][1];
              gray = Math.round(Math.max(0, Math.min(1, shi > slo ? (scalar - slo) / (shi - slo) : 0)) * 255);
            } else {
              gray = displayTransform(transform, scalar);
            }
            rgb = [gray, gray, gray];
          }
          var idx = (r * plan.cols + c2) * 4;
          image.data[idx] = rgb[0]; image.data[idx + 1] = rgb[1]; image.data[idx + 2] = rgb[2]; image.data[idx + 3] = 255;
        }
      }
      ctx.putImageData(image, 0, 0);
      host.appendChild(h('h4', { text: 'Whole-subresource overview (bounded, sampled)' }));
      host.appendChild(canvas);
      host.appendChild(h('p', { class: 'preview-caption', text: sub.dimension + (sub.face !== null && sub.face !== undefined ? ' cube face ' + sub.face : (sub.slice !== null && sub.slice !== undefined ? ' slice ' + sub.slice : '')) + (sub.zSliceCount > 1 ? ' · z ' + z + ' of ' + sub.zSliceCount : '') + '. Sampled every ' + plan.stepX + ' column and every ' + plan.stepY + ' row: ' + plan.cols + ' of ' + plan.validWidth + ' view columns, ' + plan.rows + ' of ' + plan.validHeight + ' view rows. Channel: ' + channelName + '. Extent ' + width + ' × ' + height + (cropRect ? '; display-cropped to the camera ViewRect ' + plan.validWidth + ' × ' + plan.validHeight : '; full subresource (no bytes removed' + (viewport.passViewRect ? '; pass ViewRect retained as context only' : '') + ')') + '. Fetched ' + fmtBytes(fetchedBytes) + ' in ' + plan.rows + ' row read(s) at recorded row pitch ' + sub.rowPitch + ' B; cap ' + fmtBytes(MAX_PREVIEW_BYTES) + '. Points are not interpolated; unread texels are simply not shown. Display transform: ' + transformDescription(transform) + '.' }));
    } catch (error) {
      host.appendChild(notice('Whole-subresource overview unavailable', error.message, 'warn'));
    }
  }

  async function previewBufferElements(resource, payload, stride, count, startRaw, countRaw, transform, host) {
    clear(host);
    try {
      if (!stride || !count) throw new Error('Buffer stride/count is not declared.');
      var start = parseIntegerInput(startRaw, 'first element', 0, Math.max(0, count - 1));
      var n = parseIntegerInput(countRaw, 'element count', 1, Math.min(64, count - start));
      var byteLength = n * stride;
      if (byteLength > MAX_PREVIEW_BYTES) throw new Error('Selected element window is ' + fmtBytes(byteLength) + '; cap is ' + fmtBytes(MAX_PREVIEW_BYTES) + '. Reduce the count.');
      var bytes = await explorer.readRange(payload, start * stride, byteLength);
      host.appendChild(h('h4', { text: 'Buffer element preview (bounded window)' }));
      var entries = flattenStructure(resource.bufferMetadata ? String(resource.metadataPointer || '') : '', 0, '', 0, new Set());
      if (entries.length) {
        var rows = [];
        for (var i = 0; i < n; i++) {
          var element = bytes.subarray(i * stride, (i + 1) * stride);
          var values = entries.map(function (entry) { return readMetadataValue(entry, element); });
          rows.push([String(start + i), values.join(' · '), Codec.hexDump(element, 0, 16)[0] ? Codec.hexDump(element, 0, 16)[0].hex : '']);
        }
        host.appendChild(dataTable(['Element', 'Captured fields', 'First 16 bytes'], rows, { class: 'value-table' }));
      } else {
        host.appendChild(notice('No typed buffer fields', 'No captured structure metadata or explicit element-basis schema fields. Only raw bytes are shown.', 'warn'));
        var rawRows = [];
        for (var j = 0; j < n; j++) {
          rawRows.push([String(start + j), Codec.hexDump(bytes.subarray(j * stride, (j + 1) * stride), 0, 16).map(function (l) { return l.hex; }).join(' ')]);
        }
        host.appendChild(dataTable(['Element', 'Raw bytes'], rawRows, { class: 'value-table' }));
      }
      host.appendChild(h('p', { class: 'preview-caption', text: 'Elements ' + start + '..' + (start + n - 1) + ' of ' + count + ' (' + n + ' of ' + count + ' elements). Fetched ' + fmtBytes(bytes.length) + '. Display: ' + (transform === 'minmax' ? 'display-only min/max mapping is not applied to the numeric table' : 'raw numeric table') + '.' }));
    } catch (error) {
      host.appendChild(notice('Preview unavailable', error.message, 'danger'));
    }
  }

  // --------------------------------------------------------- schema fields
  function matchRuntimeFormat(match) {
    if (!match) return null;
    return match.payloadFormat || match.dumpFormat || match.actualFormat || null;
  }

  function matchGateFormat(match) {
    if (!match) return null;
    if (match.gateFormat) return match.gateFormat;
    var declared = match.declaredFormat;
    return declared && /^PF_/i.test(String(declared)) ? declared : null;
  }

  function activeVariantFields(match) {
    var runtime = matchRuntimeFormat(match);
    if (Array.isArray(match.formatVariantFields) && match.formatVariantFields.length) {
      return match.formatVariantFields;
    }
    var variant = Codec.formatVariantFor(match.storage, runtime);
    return variant ? Codec.formatVariantFields(variant) : [];
  }

  function normalizeBasis(value) {
    if (value === null || value === undefined) return null;
    var s = String(value).toLowerCase().replace(/_/g, '-');
    if (['texel', 'pixel', 'texture-texel', 'per-texel'].indexOf(s) !== -1) return 'texel';
    if (['element', 'buffer-element', 'index', 'per-element'].indexOf(s) !== -1) return 'element';
    if (['block', 'uniform', 'constant', 'parameter-block', 'absolute'].indexOf(s) !== -1) return 'block';
    return null;
  }

  function fieldBasis(schema, field, context) {
    var explicit = field.offsetBasis || field.offsetSpace || field.offset_space || schema.offsetBasis || schema.offsetSpace || schema.offset_space;
    var basis = normalizeBasis(explicit);
    if (basis) return basis;
    var storage = typeof schema.storage === 'object' && schema.storage !== null
      ? [schema.storage.kind, schema.storage.format, schema.storage.mrtSlot].join(' ')
      : String(schema.storage || '');
    storage = storage.toLowerCase();
    if (/texel|texture|pixel/.test(storage)) return 'texel';
    if (/element|buffer|structured/.test(storage)) return 'element';
    if (/uniform|constant|parameter|block/.test(storage)) return 'block';
    // A parameterSchemas entry describes a pass parameter block by contract.  The
    // UI labels this basis explicitly when extracting; resource schemas never get
    // this default.
    if (context === 'parameter') return 'block';
    return null;
  }

  function fieldEndLength(field) {
    var info = Codec.normalizeFieldType(field.type);
    if (!info) return null;
    var bits = field.bitWidth === null || field.bitWidth === undefined ? info.bits : Number(field.bitWidth);
    var bitOffset = field.bitOffset === null || field.bitOffset === undefined ? 0 : Number(field.bitOffset);
    var count = field.count === null || field.count === undefined ? 1 : Number(field.count);
    var maxBits = info.vectorLength ? info.vectorLength * 32 : 64;
    if (!Number.isInteger(bits) || bits <= 0 || bits > maxBits || !Number.isInteger(bitOffset) || bitOffset < 0 || !Number.isInteger(count) || count < 1 || count > 65536) return null;
    return { byteLength: Math.ceil((bitOffset + bits * count) / 8), info: info, bits: bits, bitOffset: bitOffset, count: count };
  }

  function renderResourceSchemaMatch(match) {
    var card = h('div', { class: 'field-card' });
    card.appendChild(h('h4', { text: match.schemaId || '(schema without id)' }));
    var runtimeFormat = matchRuntimeFormat(match);
    var badges = [badge(match.method || 'selector')];
    if (match.quantity) badges.push(badge('quantity: ' + match.quantity));
    if (match.units) badges.push(badge('units: ' + match.units));
    if (match.coordinateSpace) badges.push(badge('space: ' + match.coordinateSpace));
    if (match.storage) badges.push(badge('storage: ' + storageLabel(match.storage)));
    if (match.declaredFormat) badges.push(badge('schema format: ' + match.declaredFormat, match.formatMatch === false ? 'warn' : ''));
    else if (match.declaredFormatText) badges.push(badge('schema format prose: ' + match.declaredFormatText, 'warn'));
    if (match.formatProof) badges.push(badge('format proof: ' + match.formatProof, match.formatProof === 'explicit-pf' ? 'ok' : 'warn'));
    if (runtimeFormat) badges.push(badge('runtime payload format: ' + runtimeFormat, match.payloadFormatMatch === false ? 'warn' : ''));
    if (match.dumpFormat) badges.push(badge('dump layout: ' + match.dumpFormat, match.dumpFormat !== match.declaredFormat ? 'warn' : ''));
    if (match.decodingStatus) badges.push(badge('decoding: ' + match.decodingStatus, /partial|unsupported|unknown/i.test(String(match.decodingStatus)) ? 'warn' : ''));
    card.appendChild(h('div', { class: 'badge-row' }, badges));
    if (match.formatProof === 'descriptive-only') {
      card.appendChild(notice('No machine-readable packed-layout proof', 'Schema storage.format is source prose (' + (match.declaredFormatText || '') + ') with no PF_* tag. All fields remain visible, but numeric extraction is disabled because a descriptive format and any formatAlternatives cannot establish a packed layout. The raw runtime-format texel inspector below remains usable for the captured stored format.', 'warn'));
    }
    if (match.formatMatch === false && match.formatProof === 'explicit-pf') {
      card.appendChild(notice('Schema format differs from the captured descriptor', 'Schema declares ' + (match.declaredFormat || 'an unspecified format') + ' but the descriptor says ' + (match.actualFormat || 'unknown') + '. Field offsets are shown, but extraction is flagged unverified.', 'warn'));
    }
    if (match.payloadFormatMatch === false && match.extractionEligible !== false) {
      card.appendChild(notice('Captured bytes use a different storage format', 'Schema declares ' + (match.declaredFormat || 'an unspecified format') + ' but the captured payload bytes are stored as ' + (runtimeFormat || 'unknown') + '. Only fields or format-variant components that explicitly apply to that runtime format may be extracted.', 'warn'));
    }
    if (match.activeFormatVariant) {
      card.appendChild(notice('Captured format variant is active', 'The runtime payload format ' + runtimeFormat + ' matches the schema format variant ' + match.activeFormatVariant + '. Its explicit component offsets are used for extraction; the primary field rows remain visible and format-gated closed.', 'ok'));
    }
    if (match.dumpFormat && match.declaredFormat && match.dumpFormat !== match.declaredFormat && match.extractionEligible === false) {
      card.appendChild(notice('Dumped layout differs from the schema format', 'DumpGPU saved this resource as ' + match.dumpFormat + '. The schema declares ' + match.declaredFormat + '. Numeric reads use the dumped layout; schema field offsets are not applied unless the dumped and declared formats are equivalent.', 'warn'));
    }
    card.appendChild(h('p', { class: 'small muted', text: 'Selectors: ' + (match.selectors || []).map(function (s) { return [s.resourceNameRegex, s.passNameRegex].filter(Boolean).join(' + '); }).join('; ') }));
    var sourceHost = h('div');
    sourceHost.appendChild(h('h4', { text: 'Field source anchors' }));
    var fieldRows = (match.fields || []).map(function (field) {
      var offset = [];
      if (field.byteOffset !== null && field.byteOffset !== undefined) offset.push('byte ' + field.byteOffset);
      if (field.bitOffset !== null && field.bitOffset !== undefined) offset.push('bit ' + field.bitOffset + (field.bitWidth ? '/' + field.bitWidth : ''));
      if (!offset.length) offset.push('not specified');
      var gate = Codec.fieldFormatGate(field, runtimeFormat, matchGateFormat(match));
      return [
        field.name || '(unnamed)',
        field.type || '—',
        offset.join(' · '),
        field.count || 1,
        (gate.appliesToFormats || []).length ? gate.appliesToFormats.join(', ') : 'declared storage format',
        field.decode || '—',
        field.units || '—',
        { node: (function () { var d = h('div'); appendSourceRefs(d, field.sourceRefs); return d; })() }
      ];
    });
    card.appendChild(dataTable(['Field', 'Type', 'Offset', 'Count', 'Applies to formats', 'Decode / source formula', 'Units', 'Source anchors'], fieldRows));
    renderFormatVariants(card, match);
    if (match.fieldSupport && (match.fieldSupport.unsafeExpressionFields || []).length) {
      card.appendChild(notice('Executable-looking decode strings retained, never evaluated', 'Fields: ' + match.fieldSupport.unsafeExpressionFields.join(', ') + '. These strings are displayed as source text; no schema string is evaluated.', 'danger'));
    }
    if (match.fieldSupport && (match.fieldSupport.sourceEquationFields || []).length) {
      card.appendChild(notice('Source formula (display only), not an executable decoder', 'Fields: ' + match.fieldSupport.sourceEquationFields.join(', ') + '. Each decode is a source-linked equation; the typed raw value is shown and the formula is not applied or evaluated by the explorer.', 'warn'));
    }
    appendNamedSourceRefs(sourceHost, 'Producer sources', match.producerSources || []);
    appendNamedSourceRefs(sourceHost, 'Consumer sources', match.consumerSources || []);
    card.appendChild(sourceHost);
    renderTransforms(card, match);
    renderTerms(card, match.mathTerms);
    if (match.gaps) card.appendChild(details('Schema-declared gaps', h('pre', { text: JSON.stringify(match.gaps, null, 2) })));
    var extractionHost = h('div', { class: 'schema-extraction' });
    card.appendChild(extractionHost);
    card._extractionHost = extractionHost;
    return card;
  }

  function renderFormatVariants(card, match) {
    var storage = match.storage && typeof match.storage === 'object' ? match.storage : null;
    var variants = storage && Array.isArray(storage.formatVariants) ? storage.formatVariants : [];
    if (!variants.length) return;
    var runtimeFormat = matchRuntimeFormat(match);
    card.appendChild(h('h4', { text: 'Format variants (schema alternatives; only the captured runtime format is active)' }));
    var rows = variants.map(function (variant) {
      var active = !!runtimeFormat && Codec.formatsEquivalent(runtimeFormat, variant.format);
      var components = Array.isArray(variant.components) ? variant.components : [];
      return [
        variant.format || '—',
        active ? badge('active for captured payload', 'ok') : badge('not selected by captured payload', 'warn'),
        variant.strideBytes === null || variant.strideBytes === undefined ? '—' : String(variant.strideBytes),
        components.map(function (component) {
          var offset = component.byteOffset !== null && component.byteOffset !== undefined
            ? 'byte ' + component.byteOffset : 'offset not specified';
          return (component.name || '(unnamed)') + ' ' + (component.type || '?') + ' @ ' + offset;
        }).join('; ') || '—',
        { node: (function () { var d = h('div'); appendSourceRefs(d, variant.sourceRefs || []); return d; })() }
      ];
    });
    card.appendChild(dataTable(['Variant format', 'Captured state', 'Stride', 'Components', 'Source anchors'], rows));
    card.appendChild(h('p', { class: 'small muted', text: 'formatAlternatives and formatVariants are source-level possibilities, not evidence of a captured permutation. The runtime stored format and raw metadata decide which components are active.' }));
  }

  async function renderSchemaFields(host, resource, record, view) {
    if (!host) return;
    clear(host);
    if (!(resource.schemaMatches || []).length) return;
    if (!view) {
      host.appendChild(h('p', { class: 'muted small', text: 'Read a texel or buffer element to extract source-schema fields at the current index.' }));
      return;
    }
    host.appendChild(h('h4', { text: 'Source schema fields at the current selection' }));
    for (var mi = 0; mi < resource.schemaMatches.length; mi++) {
      var match = resource.schemaMatches[mi];
      var block = h('div', { class: 'field-card' });
      block.appendChild(h('h5', { text: match.schemaId }));
      if (match.extractionEligible === false) {
        block.appendChild(notice('Schema fields retained but not applied', match.extractionEligibleReason || 'The captured storage layout is not proven by machine-readable format metadata. Field offsets are displayed for reference only; the raw runtime-format texel inspector remains usable.', 'warn'));
      } else if (match.primaryFieldsExtractable === false) {
        block.appendChild(notice('Primary fields not applied; only the captured format variant is extractable', match.extractionEligibleReason || ('The schema primary layout ' + (match.declaredFormat || 'is not machine-readable') + ' does not match the captured stored format. Variant components whose explicit format matches the captured format remain extractable.'), 'warn'));
      }
      var runtimeFormat = matchRuntimeFormat(match);
      var variantFields = activeVariantFields(match);
      block.appendChild(h('p', { class: 'small muted', text: 'Runtime stored format: ' + (runtimeFormat || 'not recorded') + '. Numeric reads use that format; fields gated to another format are shown as inactive and are never extracted.' }));
      if (variantFields.length) {
        block.appendChild(h('p', { class: 'small muted', text: 'Captured payload format matches the schema format variant ' + runtimeFormat + '; its explicit component offsets are added below.' }));
      }
      var storage = match.storage && typeof match.storage === 'object' ? match.storage : {};
      var pageStride = Number(storage.pageStrideBytes || storage.pageEntryStrideBytes || 0);
      var elemStride = Number(storage.strideBytes || view.stride || 0);
      var pageElems = pageStride > 0 && elemStride > 0 ? Math.max(1, Math.round(pageStride / elemStride)) : 1;
      if (pageStride > 0 && view.kind === 'buffer') {
        var pageNumber = Math.floor(view.index / pageElems);
        block.appendChild(h('p', { class: 'table-note', text: 'Schema fields are page-relative: element index ' + view.index + ' maps to page ' + pageNumber + ' (page stride ' + pageStride + ' bytes, ' + pageElems + ' elements per page). The selected element also has its own 4-byte value; the page fields below are not a claim that the selected element starts at byte 0 of the page.' }));
      }
      var rows = [];
      var fieldsToRender = (match.fields || []).slice().concat(variantFields);
      for (var fi = 0; fi < fieldsToRender.length; fi++) {
        var field = fieldsToRender[fi];
        var basis = fieldBasis(match, field);
        var result = { field: field, basis: basis, values: null, error: null, inactive: false };
        var hasExplicitGate = Array.isArray(field.appliesToFormats) && field.appliesToFormats.length > 0;
        if (match.primaryFieldsExtractable === false && !field.variantFormat && !hasExplicitGate) {
          result.inactive = true;
          result.error = match.extractionEligibleReason || 'The schema packed layout has no machine-readable format proof for this field; the offset is displayed but not applied.';
          rows.push(renderFieldValueRow(field, basis, result));
          continue;
        }
        var gate = Codec.fieldFormatGate(field, runtimeFormat, matchGateFormat(match));
        if (!gate.active) {
          result.inactive = true;
          result.error = gate.reason;
          rows.push(renderFieldValueRow(field, basis, result));
          continue;
        }
        try {
          if (!view.payload) throw new Error('No payload selected.');
          if (basis === null) throw new Error('Offset basis is not specified by the schema field or storage; not guessed.');
          var end = fieldEndLength(field);
          if (!end) throw new Error('Field type/offset/width is not supported for extraction.');
          var base;
          if (basis === 'texel') base = view.texelOffset;
          else if (basis === 'element') {
            base = view.elementOffset;
            if (pageStride > 0 && view.index !== null && view.index !== undefined) {
              base = Math.floor(view.index / pageElems) * pageStride;
            }
          } else base = 0;
          if (basis === 'texel' && view.kind !== 'texture') throw new Error('Schema says texel basis but the selection is not a texture.');
          if (basis === 'element' && view.kind !== 'buffer') throw new Error('Schema says element basis but the selection is not a buffer.');
          var byteOffset = field.byteOffset === null || field.byteOffset === undefined ? 0 : Number(field.byteOffset);
          var absolute = base + byteOffset;
          if (!Number.isInteger(absolute) || absolute < 0 || absolute + end.byteLength > view.payload.bytes) throw new Error('Field range is outside the captured payload.');
          var bytes = await explorer.readRange(view.payload, absolute, end.byteLength);
          result.values = Codec.extractField(bytes, 0, {
            type: field.type, bitOffset: end.bitOffset, bitWidth: end.bits, count: end.count, decode: field.decode
          });
        } catch (error) {
          result.error = error.message;
        }
        rows.push(renderFieldValueRow(field, basis, result));
      }
      block.appendChild(dataTable(['Field', 'Basis / offset', 'Raw / decoded values', 'State'], rows));
      host.appendChild(block);
    }
  }

  function renderFieldValueRow(field, basis, result) {
    var valueCell = h('div');
    var stateCell = h('div');
    if (result.inactive) {
      valueCell.textContent = '—';
      stateCell.appendChild(badge('inactive for runtime format', 'warn'));
      stateCell.appendChild(h('div', { class: 'small muted', text: result.error }));
    } else if (result.error) {
      valueCell.textContent = '—';
      stateCell.appendChild(badge('not extracted', 'warn'));
      stateCell.appendChild(h('div', { class: 'small muted', text: result.error }));
    } else if (result.values && result.values.supported === false) {
      valueCell.textContent = '—';
      stateCell.appendChild(badge('unsupported decode', 'danger'));
      stateCell.appendChild(h('div', { class: 'small muted', text: result.values.reason }));
    } else if (result.values) {
      var vals = result.values.values || [];
      var shown = vals.slice(0, 8).map(function (v, i) { return '[' + i + '] ' + fmtNumber(v); }).join(' ');
      valueCell.appendChild(h('div', { class: 'mono' }, shown + (vals.length > 8 ? ' … ' + vals.length + ' values' : '')));
      if (result.values.rawValues) valueCell.appendChild(h('div', { class: 'small muted mono', text: 'raw: ' + result.values.rawValues.slice(0, 8).join(', ') }));
      stateCell.appendChild(badge('extracted', 'ok'));
      if (field.variantFormat) stateCell.appendChild(badge('variant ' + field.variantFormat, 'accent'));
      if (result.values.decodeApplied) stateCell.appendChild(h('div', { class: 'small muted', text: result.values.decodeApplied }));
      if (result.values.decodeIgnored) stateCell.appendChild(h('div', { class: 'small muted', text: 'Source decode equation displayed but not applied.' }));
    } else {
      valueCell.textContent = '—';
      stateCell.appendChild(badge('not requested', ''));
    }
    return [
      field.name || '(unnamed)',
      (basis || 'unspecified') + (field.byteOffset !== null && field.byteOffset !== undefined ? ' + byte ' + field.byteOffset : '') + (field.bitOffset !== null && field.bitOffset !== undefined ? ' + bit ' + field.bitOffset : ''),
      valueCell,
      stateCell
    ];
  }

  // -------------------------------------------------------- structure values
  function flattenStructure(pointer, baseOffset, prefix, depth, seen) {
    var meta = pointer && state.structures && state.structures.byPointer ? state.structures.byPointer[pointer] : null;
    if (!meta) return [];
    if (seen.has(pointer) || depth > 8) return [];
    seen.add(pointer);
    var out = [];
    var members = Array.isArray(meta.Members) ? meta.Members : [];
    members.forEach(function (member) {
      var bt = String(member.BaseType || '');
      var name = prefix + String(member.Name || '(unnamed)');
      var offset = baseOffset + Number(member.Offset || 0);
      if (bt === 'UBMT_NESTED_STRUCT' || bt === 'UBMT_INCLUDED_STRUCT') {
        var nested = flattenStructure(String(member.StructMetadata || ''), offset, name + '.', depth + 1, seen);
        if (nested.length) out.push.apply(out, nested);
        else out.push({ path: name, offset: offset, unsupported: bt, note: 'nested structure metadata ' + (member.StructMetadata || '(missing)') + ' was not captured or is cyclic' });
      } else if (bt === 'UBMT_REFERENCED_STRUCT') {
        out.push({ path: name, offset: offset, referenced: true, metadataPointer: member.StructMetadata, note: 'referenced struct; its content is linked separately' });
      } else if (bt === 'UBMT_FLOAT32' || bt === 'UBMT_UINT32' || bt === 'UBMT_INT32' || bt === 'UBMT_BOOL') {
        var elems = Math.max(Number(member.NumElements || 1), 1);
        var rows = Math.max(Number(member.NumRows || 1), 1);
        var cols = Math.max(Number(member.NumColumns || 1), 1);
        var scalar = 0;
        for (var e = 0; e < elems; e++) {
          for (var r = 0; r < rows; r++) {
            for (var c = 0; c < cols; c++) {
              var suffix = '';
              if (elems > 1) suffix += '[' + e + ']';
              if (rows > 1) suffix += ['.x', '.y', '.z', '.w'][r] || ('.r' + r);
              if (cols > 1) suffix += ['.x', '.y', '.z', '.w'][c] || ('.c' + c);
              out.push({ path: name + suffix, offset: offset + scalar * 4, baseType: bt, shaderType: member.ShaderType, fileLine: member.FileLine, scalar: scalar });
              scalar++;
            }
          }
        }
      } else {
        out.push({ path: name, offset: offset, unsupported: bt || '(missing BaseType)', shaderType: member.ShaderType, fileLine: member.FileLine, note: 'no numeric extractor for this uniform base type' });
      }
    });
    seen.delete(pointer);
    return out;
  }

  function readMetadataValue(entry, bytes) {
    if (!entry || entry.offset + 4 > bytes.length) return '—';
    var view = new DataView(bytes.buffer, bytes.byteOffset + entry.offset, 4);
    if (entry.baseType === 'UBMT_FLOAT32') return fmtNumber(view.getFloat32(0, true));
    if (entry.baseType === 'UBMT_UINT32' || entry.baseType === 'UBMT_BOOL') return String(view.getUint32(0, true));
    if (entry.baseType === 'UBMT_INT32') return String(view.getInt32(0, true));
    return '—';
  }

  function renderMetadataValues(entries, bytes, label) {
    var rows = entries.map(function (entry) {
      var row = [entry.path, entry.baseType || entry.unsupported || '—', String(entry.offset)];
      if (entry.unsupported || entry.referenced) {
        row.push('—'); row.push('—');
        row.push(h('span', { class: 'small muted', text: entry.note || 'No numeric extractor.' }));
        return row;
      }
      if (entry.offset + 4 > bytes.length) {
        row.push('—'); row.push('—');
        row.push(h('span', { class: 'small warn', text: 'offset beyond captured bytes' }));
        return row;
      }
      var raw = Array.prototype.slice.call(bytes.subarray(entry.offset, entry.offset + 4)).map(function (b) { return b.toString(16).padStart(2, '0'); }).join(' ');
      row.push({ text: raw, class: 'mono' });
      row.push({ text: readMetadataValue(entry, bytes), class: 'value' });
      row.push(entry.shaderType || '');
      return row;
    });
    return h('div', {},
      h('p', { class: 'table-note', text: label + ' values are read from captured bytes at native member offsets. Unsupported base types remain raw; no reinterpretation is substituted.' }),
      dataTable(['Member path', 'Base type', 'Byte offset', 'Raw (LE)', 'Value', 'Shader type'], rows, { class: 'value-table' })
    );
  }

  // ------------------------------------------------------------------- pass
  function renderPassDetail(record) {
    var detail = $('detail');
    clear(detail);
    var badges = [
      badge('pass ' + (record.native ? record.native.Id : '?')),
      badge((record.inputs || []).length + ' inputs'),
      badge((record.outputs || []).length + ' outputs'),
      badge(record.parameterBlock ? 'parameter bytes captured' : 'no parameter bytes', record.parameterBlock ? 'ok' : 'warn')
    ];
    if (record.schemaMatches && record.schemaMatches.length) badges.push(badge(record.schemaMatches.length + ' parameter schema', record.schemaMatches.length > 1 ? 'warn' : 'accent'));
    detail.appendChild(detailHeader('Pass', record.displayLabel || record.name, 'Pointer ' + (record.pointer || '—') + ' · ' + (record.parametersName || 'no parameter layout name'), badges));
    if (state.index.fixture) detail.appendChild(notice('Synthetic fixture', 'These records and bytes are a labelled test fixture, not runtime evidence.', 'warn'));

    detail.appendChild(kvGrid([
      ['Native pass id', String(record.native && record.native.Id !== undefined ? record.native.Id : '—')],
      ['Display label', record.displayLabel || '—'],
      ['Frame', record.frameNumber === null || record.frameNumber === undefined ? 'not recorded' : String(record.frameNumber)],
      ['Graph prefix', record.graphPrefix || '—'],
      ['View.StateFrameIndexMod8', record.viewState && record.viewState.frameIndexMod8 !== null && record.viewState.frameIndexMod8 !== undefined ? String(record.viewState.frameIndexMod8) : 'not recorded'],
      ['Pointer', record.pointer || '—'],
      ['Parameters name', record.parametersName || '—'],
      ['Parameters pointer', record.parametersPointer || '—'],
      ['Metadata pointer', record.parametersMetadataPointer || '—'],
      ['Draw count', record.drawCount === null || record.drawCount === undefined ? 'not captured (null; zero is not substituted)' : String(record.drawCount)],
      ['Draw count source', record.drawCountStatus === 'captured' ? (record.drawCountSource || 'captured') : 'Base/PassDrawCounts.json has no record for this pass'],
      ['Parent event scopes', (record.parentScopes || []).join(' › ') || '—'],
      ['Native record', record.nativeRecordId || '—']
    ]));

    detail.appendChild(h('h3', { text: 'Parameter block and every captured constant' }));
    if (!record.parameterBlock) {
      detail.appendChild(notice('No parameter bytes captured', 'The pass metadata is retained below, but no raw parameter block was captured. Member values are not fabricated.', 'danger'));
    } else {
      var pb = record.parameterBlock;
      detail.appendChild(h('p', { class: 'small muted', text: 'Raw block ' + pb.bytes + ' B · SHA-256 ' + pb.sha256 + ' · ' + (pb.chunks || []).length + ' chunks · ' + pb.originalPath }));
      var blockHost = h('div', { class: 'parameter-values' });
      detail.appendChild(blockHost);
      renderParameterBlock(blockHost, record, pb);
      var hexHost = h('div');
      renderHexViewer(hexHost, pb, 0);
      detail.appendChild(hexHost);
      detail.appendChild(details('Raw parameter block details', h('pre', { class: 'json', text: JSON.stringify(pb, null, 2) })));
    }

    if ((record.schemaMatches || []).length) {
      detail.appendChild(h('h3', { text: 'Parameter schema joins' }));
      detail.appendChild(h('p', { class: 'muted small', text: 'Explicit pass-name/resource selectors only. Unknown decode strings are never evaluated; they are shown with source anchors.' }));
      record.schemaMatches.forEach(function (match) { detail.appendChild(renderParameterSchemaMatch(match, record)); });
    } else {
      detail.appendChild(h('h3', { text: 'Parameter schema joins' }));
      detail.appendChild(notice('No parameter schema match', 'No parameter schema selector matched this pass. Parameter metadata and raw bytes are still shown; this is a schema coverage gap.', 'warn'));
    }

    detail.appendChild(h('h3', { text: 'Inputs and outputs' }));
    detail.appendChild(renderBindings(record));

    detail.appendChild(h('h3', { text: 'Native uniform buffer bindings (per-pass, all members)' }));
    if (!(record.nativeUniformBuffers || []).length) {
      detail.appendChild(h('p', { class: 'muted small', text: 'No Base/NativeUniformBufferBindings.json record names this pass. No member list is inferred from the pass parameter metadata.' }));
    } else {
      record.nativeUniformBuffers.forEach(function (native) {
        var card = h('div', { class: 'field-card' });
        card.appendChild(h('h4', { text: (native.layoutName || 'uniform layout') + ' · ' + (native.uniformBuffer || 'no uniform buffer pointer') }));
        card.appendChild(h('div', { class: 'badge-row' },
          badge('OwnerPassExecutionDependencyClaimed=' + String(native.ownerPassExecutionDependencyClaimed), 'warn'),
          badge('SourceStateProvenAtScheduling=' + String(native.sourceStateProvenAtScheduling), 'warn'),
          badge('ScheduledStatus=' + String(native.scheduledStatus || '—')),
          badge(Object.keys(native.bindings || {}).length + ' member(s)')));
        card.appendChild(notice('Temporal limitation (preserved from the source)', 'TimingRisk: ' + (native.timingRisk || '—') + '. MutableBindingPolicy: ' + (native.mutableBindingPolicy || '—') + '. ReadOnlyBindingPolicy: ' + (native.readOnlyBindingPolicy || '—') + '. Collection: ' + (native.collectionTiming || '—') + ' / ' + (native.cpuCollectionTiming || '—') + '. A successful file/hash proves readback completion, not owner-time value identity; this explorer does not upgrade that claim. Static LUT/BlueNoise immutability is a separate replay/source review question.', 'warn'));
        card.appendChild(kvGrid([
          ['Pass pointer', native.pass || '—'],
          ['Uniform buffer pointer', native.uniformBuffer || '—'],
          ['Layout name', native.layoutName || '—'],
          ['Collection timing', native.collectionTiming || '—'],
          ['CPU collection timing', native.cpuCollectionTiming || '—'],
          ['GPU readback ordering', native.gpuReadbackOrdering || '—'],
          ['Readback completion file', native.readbackCompletionFile || '—'],
          ['Dumped status source', native.dumpedStatusSource || '—'],
          ['ReadOnlyBindingPolicy', native.readOnlyBindingPolicy || '—'],
          ['MutableBindingPolicy', native.mutableBindingPolicy || '—'],
          ['Native record', native.nativeRecordId || '—']
        ]));
        if (native.uniformBufferPayload) {
          var ub = native.uniformBufferPayload;
          card.appendChild(h('p', { class: 'small', text: 'Captured RDG uniform bytes: ' + ub.bytes + ' B · SHA-256 ' + ub.sha256 + ' · ' + ub.originalPath }));
          var ubHost = h('div');
          card.appendChild(h('div', { class: 'button-row' }, h('button', { type: 'button', text: 'Show captured uniform bytes', on: { click: function () { renderHexViewer(ubHost, ub, 0); } } })));
          card.appendChild(ubHost);
        } else {
          card.appendChild(notice('Uniform bytes not captured', 'No Structures/<pointer>.bin exists for this uniform buffer. Binding descriptors are retained with their offsets and skips; values are not fabricated.', 'warn'));
        }
        card.appendChild(renderNativeBindingTable(Object.keys(native.bindings || {}).map(function (memberPath) {
          var entry = native.bindings[memberPath];
          return { memberPath: memberPath, entry: entry, passId: record.id, passPointer: native.pass, completion: entry && entry.completion, payloadIds: entry && entry.payloadIds };
        }), 'pass'));
        card.appendChild(details('Complete NativeUniformBufferBindings record (all fields, arrays, nested paths)', h('pre', { class: 'json', text: JSON.stringify(native.raw, null, 2) })));
        detail.appendChild(card);
      });
    }

    detail.appendChild(h('h3', { text: 'Pass parameter pointer bindings (ParameterBindings)' }));
    if (!(record.parameterBindings || []).length) {
      detail.appendChild(h('p', { class: 'muted small', text: 'No Base/ParameterBindings.json record names this pass.' }));
    } else {
      record.parameterBindings.forEach(function (bindingRecord) {
        var rows = Object.keys(bindingRecord.Bindings || {}).map(function (name) {
          var entry = bindingRecord.Bindings[name];
          return [name, entry.BaseType || '—', entry.Offset === undefined ? '—' : String(entry.Offset), entry.NumElements === undefined ? '—' : String(entry.NumElements), entry.ElementStrideBytes === undefined ? '—' : String(entry.ElementStrideBytes), entry.Pointer || '—', String(entry.Null), entry.Source || '—', h('details', {}, h('summary', { text: 'raw' }), h('pre', { class: 'json small', text: JSON.stringify(entry, null, 2) }))];
        });
        detail.appendChild(h('p', { class: 'small muted', text: 'Parameters pointer ' + (bindingRecord.Parameters || '—') + ' · every pointer binding is retained; unknown members are not dropped.' }));
        detail.appendChild(dataTable(['Member', 'Base type', 'Offset', 'NumElements', 'Stride', 'Pointer', 'Null', 'Source', 'Raw'], rows));
        detail.appendChild(details('Complete ParameterBindings record', h('pre', { class: 'json', text: JSON.stringify(bindingRecord, null, 2) })));
      });
    }

    detail.appendChild(h('h3', { text: 'Referenced structs' }));
    if (!(record.referencedStructs || []).length) {
      detail.appendChild(h('p', { class: 'muted small', text: 'No referenced structs were recorded for this pass.' }));
    } else {
      record.referencedStructs.forEach(function (ref) { detail.appendChild(renderReferencedStruct(ref, record)); });
    }

    detail.appendChild(h('h3', { text: 'Native metadata and gaps' }));
    detail.appendChild(jsonDetails('Complete native pass record', record.native, false));
    detail.appendChild(details('Pass gaps (' + (record.gaps || []).length + ')', (function () { var d = h('div'); renderGapsList(record.gaps || [], d); return d; })(), (record.gaps || []).length > 0));
  }

  async function renderParameterBlock(host, record, block) {
    clear(host);
    var meta = record.parameterMetadata;
    if (!meta) {
      host.appendChild(notice('Parameter metadata unavailable', 'A raw parameter block exists, but its structure metadata was not captured. Only hex is available; member names and numeric values are not guessed.', 'warn'));
      return;
    }
    var maxOffset = 0;
    var entries = flattenStructure(String(record.parametersMetadataPointer || ''), 0, '', 0, new Set());
    entries.forEach(function (entry) { maxOffset = Math.max(maxOffset, entry.offset + 4); });
    var readLength = Math.min(block.bytes, Math.min(MAX_PARAMETER_READ, Math.max(maxOffset, Math.min(block.bytes, 4096))));
    if (block.bytes > MAX_PARAMETER_READ) {
      host.appendChild(notice('Large parameter block', 'Only the first ' + fmtBytes(readLength) + ' are read for member values. Hex pages remain available for the rest; no values are invented.', 'warn'));
    }
    try {
      var bytes = await explorer.readRange(block, 0, readLength);
      host.appendChild(h('p', { class: 'small muted', text: 'Structure ' + (meta.StructTypeName || '?') + ' (' + (meta.Size || block.bytes) + ' B declared, ' + block.bytes + ' B captured). ' + entries.length + ' flattened numeric member value(s) resolved from native metadata.' }));
      host.appendChild(renderMetadataValues(entries, bytes, 'Parameter'));
      if (block.bytes > readLength) host.appendChild(h('p', { class: 'small muted', text: 'Member values beyond the read window are not shown; raw bytes are retained.' }));
    } catch (error) {
      host.appendChild(notice('Could not read parameter bytes', error.message, 'danger'));
    }
    host.appendChild(details('Captured structure metadata', h('pre', { class: 'json', text: JSON.stringify(meta, null, 2) })));
  }

  function renderParameterSchemaMatch(match, record) {
    var card = h('div', { class: 'field-card' });
    card.appendChild(h('h4', { text: match.schemaId || '(schema without id)' }));
    card.appendChild(h('div', { class: 'badge-row' }, badge(match.method || 'selector'), match.qualifiedShader ? badge('shader: ' + match.qualifiedShader) : null, match.quantity ? badge('quantity: ' + match.quantity) : null, match.units ? badge('units: ' + match.units) : null, match.decodingStatus ? badge('decoding: ' + match.decodingStatus) : null));
    card.appendChild(h('p', { class: 'small muted', text: 'Match: ' + (match.selectors || []).map(function (s) { return s.passEvent || s.qualifiedShader || [s.resourceNameRegex, s.passNameRegex].filter(Boolean).join(' + '); }).join('; ') + (match.sourcePath ? ' · source ' + match.sourcePath + (match.line ? ':' + match.line : '') : '') }));
    var nativeMembers = new Map();
    if (record.parameterMetadata && Array.isArray(record.parameterMetadata.Members)) {
      record.parameterMetadata.Members.forEach(function (member) { nativeMembers.set(String(member.Name), member); });
    }
    var rows = (match.fields || []).map(function (field) {
      var native = nativeMembers.get(String(field.name));
      return [
        field.name || '(unnamed)', field.type || '—',
        field.byteOffset !== null && field.byteOffset !== undefined ? String(field.byteOffset) : '—',
        field.bitOffset !== null && field.bitOffset !== undefined ? field.bitOffset + (field.bitWidth ? '/' + field.bitWidth : '') : '—',
        field.condition || field.variant || (field.optional ? 'optional' : '—'),
        field.decode || '—', field.units || '—',
        native ? 'offset ' + native.Offset + ' · ' + (native.BaseType || '') : 'no exact native member name',
        { node: (function () { var d = h('div'); appendSourceRefs(d, field.sourceRefs); return d; })() }
      ];
    });
    card.appendChild(dataTable(['Field', 'Type', 'Byte offset', 'Bit offset', 'Condition / variant', 'Decode / source equation', 'Units', 'Native parameter member', 'Source anchors'], rows));
    if (match.fieldSupport && (match.fieldSupport.unsafeExpressionFields || []).length) {
      card.appendChild(notice('Executable-looking decode strings retained, never evaluated', 'Fields: ' + match.fieldSupport.unsafeExpressionFields.join(', ') + '. Only known numeric tags are applied; these strings are shown as source text and are never evaluated.', 'danger'));
    }
    if (match.fieldSupport && (match.fieldSupport.sourceEquationFields || []).length) {
      card.appendChild(notice('Source formula (display only), not an executable decoder', 'Fields: ' + match.fieldSupport.sourceEquationFields.join(', ') + '. Each decode is a source-linked equation; native StructuresMetadata values are shown separately and the formula is not applied or evaluated.', 'warn'));
    }
    renderTransforms(card, match);
    renderTerms(card, match.mathTerms);
    var valueHost = h('div');
    card.appendChild(valueHost);
    var hasExplicitOffset = (match.fields || []).some(function (field) {
      return (field && field.byteOffset !== null && field.byteOffset !== undefined)
        || (field && field.bitOffset !== null && field.bitOffset !== undefined);
    });
    if (record.parameterBlock && hasExplicitOffset) {
      card.appendChild(h('p', { class: 'small muted', text: 'Some parameter schema fields carry explicit byte/bit offsets. The extraction button interprets those as offset from the captured parameter block.' }));
      card.appendChild(h('div', { class: 'button-row' },
        h('button', {
          type: 'button', text: 'Extract schema fields from captured parameter block',
          on: { click: function (event) { extractParameterSchemaFields(record, match, valueHost, event.currentTarget); } }
        })
      ));
    } else {
      card.appendChild(h('p', { class: 'small muted', text: 'This parameter schema has no captured byte offsets; its fields are displayed as source-linked names/equations. Values come from the native StructuresMetadata and raw block above, not from these offsets.' }));
    }
    return card;
  }

  async function extractParameterSchemaFields(record, match, host, button) {
    clear(host);
    var block = record.parameterBlock;
    if (!block) { host.appendChild(notice('No parameter bytes', 'The schema fields cannot be read without a captured block.', 'warn')); return; }
    if (button) button.disabled = true;
    try {
      var maxNeeded = 0;
      var prepared = [];
      (match.fields || []).forEach(function (field) {
        var basis = fieldBasis(match, field, 'parameter');
        var end = fieldEndLength(field);
        var entry = { field: field, basis: basis, end: end, error: null, values: null };
        if (field.byteOffset === null || field.byteOffset === undefined) {
          entry.error = 'No captured byte offset on this parameter-schema field; no value is assumed.';
        } else if (basis === null) entry.error = 'Offset basis is not specified; parameter schema offsets are shown, but no value is assumed.';
        else if (basis !== 'block') entry.error = 'Parameter schema field uses "' + basis + '" basis, which is not a parameter block.';
        else if (!end) entry.error = 'Unsupported field type/offset/width.';
        else {
          var absolute = Number(field.byteOffset || 0);
          entry.absolute = absolute;
          if (absolute + end.byteLength > block.bytes) entry.error = 'Field range is outside the captured parameter block.';
          else maxNeeded = Math.max(maxNeeded, absolute + end.byteLength);
        }
        prepared.push(entry);
      });
      if (!maxNeeded) {
        prepared.forEach(function (entry) {
          if (entry.error) host.appendChild(h('p', { class: 'small muted', text: (entry.field.name || '(field)') + ': ' + entry.error }));
        });
        return;
      }
      var bytes = await explorer.readRange(block, 0, maxNeeded);
      var rows = prepared.map(function (entry) {
        var valueCell = h('div');
        var state = h('div');
        if (entry.error) {
          valueCell.textContent = '—';
          state.appendChild(badge('not extracted', 'warn'));
          state.appendChild(h('div', { class: 'small muted', text: entry.error }));
        } else {
          try {
            entry.values = Codec.extractField(bytes.subarray(entry.absolute, entry.absolute + entry.end.byteLength), 0, {
              type: entry.field.type, bitOffset: entry.end.bitOffset, bitWidth: entry.end.bits, count: entry.end.count, decode: entry.field.decode
            });
            if (!entry.values.supported) {
              state.appendChild(badge('unsupported decode', 'danger'));
              valueCell.textContent = '—';
              state.appendChild(h('div', { class: 'small muted', text: entry.values.reason }));
            } else {
              valueCell.appendChild(h('div', { class: 'mono' }, entry.values.values.slice(0, 8).map(function (v, i) { return '[' + i + '] ' + fmtNumber(v); }).join(' ') + (entry.values.values.length > 8 ? ' …' : '')));
              state.appendChild(badge('extracted', 'ok'));
            }
          } catch (error) {
            valueCell.textContent = '—';
            state.appendChild(badge('error', 'danger'));
            state.appendChild(h('div', { class: 'small muted', text: error.message }));
          }
        }
        return [entry.field.name || '(unnamed)', entry.basis || 'unspecified', entry.absolute === undefined ? '—' : String(entry.absolute), valueCell, state];
      });
      host.appendChild(h('p', { class: 'table-note', text: 'Parameter schema byteOffsets are interpreted as offsets from the start of the captured parameter block. Values are raw numeric extractions; transforms are shown separately and are not applied.' }));
      host.appendChild(dataTable(['Field', 'Basis', 'Block byte offset', 'Raw / decoded values', 'State'], rows));
    } catch (error) {
      host.appendChild(notice('Schema extraction failed', error.message, 'danger'));
    } finally {
      if (button) button.disabled = false;
    }
  }

  function renderBindings(record) {
    var rows = [];
    (record.inputs || []).forEach(function (item) { rows.push(['Input', item.uniqueName, item.version || '—', item.resourceId ? resourceLink(item.resourceId, null) : 'unresolved', item.mip === null || item.mip === undefined ? '—' : String(item.mip), item.slice === null || item.slice === undefined ? '—' : String(item.slice), item.stencil ? 'stencil' : 'colour/depth', String((item.payloadIds || []).length)]); });
    (record.outputs || []).forEach(function (item) { rows.push(['Output', item.uniqueName, item.version || '—', item.resourceId ? resourceLink(item.resourceId, null) : 'unresolved', item.mip === null || item.mip === undefined ? '—' : String(item.mip), item.slice === null || item.slice === undefined ? '—' : String(item.slice), item.stencil ? 'stencil' : 'colour/depth', String((item.payloadIds || []).length)]); });
    if (!rows.length) return h('p', { class: 'muted small', text: 'No input/output bindings were captured for this pass.' });
    return h('div', {},
      h('p', { class: 'table-note', text: 'Bindings match resource + mip + slice + aspect (stencil vs colour/depth) and version when the native name carries one. A colour/depth binding never lists stencil bytes; a stencil binding never lists colour/depth bytes. Payloads remain independently addressable through the resource record even when no pass binds them.' }),
      dataTable(['Direction', 'Native subresource name', 'Version', 'Resource', 'Mip', 'Slice', 'Aspect', 'Captured payloads'], rows));
  }

  function bindingPayloadRefs(binding, payloadById) {
    // Pass-side entries carry payloadIds; resource-side entries carry a single
    // payloadId. Normalize both shapes to one ordered, duplicate-free list so a
    // mixed/legacy record can never render the same payload twice.
    var ids = [];
    var seen = new Set();
    function addId(value) {
      if (value === null || value === undefined) return;
      var text = String(value);
      if (!text || seen.has(text)) return;
      seen.add(text);
      ids.push(text);
    }
    if (binding && Array.isArray(binding.payloadIds)) binding.payloadIds.forEach(addId);
    if (binding && binding.payloadId !== null && binding.payloadId !== undefined) addId(binding.payloadId);
    var lookup = payloadById && typeof payloadById.get === 'function' ? payloadById : null;
    return ids.map(function (id) {
      var payload = lookup ? lookup.get(id) : null;
      var bytes = payload && payload.bytes !== null && payload.bytes !== undefined ? Number(payload.bytes) : null;
      var chunks = payload && Array.isArray(payload.chunks) ? payload.chunks.length : null;
      return {
        payloadId: id,
        payload: payload || null,
        resourceId: (payload && payload.resourceId) || null,
        bytes: bytes,
        chunks: chunks,
        available: !!(payload && bytes > 0 && chunks > 0)
      };
    });
  }

  function renderNativeBindingTable(bindings, side, payloads) {
    if (!(bindings || []).length) return h('p', { class: 'muted small', text: 'No native uniform binding entries reference this record.' });
    var payloadById = null;
    if (side === 'resource' && Array.isArray(payloads)) {
      payloadById = new Map();
      payloads.forEach(function (payload) { if (payload && payload.id) payloadById.set(String(payload.id), payload); });
    }
    var rows = (bindings || []).map(function (binding) {
      var entry = binding.entry || {};
      var completion = binding.completion || {};
      var timing = binding.recordTiming || {};
      var payloadCell = h('div');
      var payloadRefs = bindingPayloadRefs(binding, payloadById);
      if (payloadRefs.length) {
        payloadRefs.forEach(function (ref) {
          if (ref.resourceId) payloadCell.appendChild(h('span', { class: 'mono small' }, resourceLink(ref.resourceId, ref.payloadId), ' '));
          else payloadCell.appendChild(h('span', { class: 'mono small', text: ref.payloadId + ' ' }));
          if (side === 'resource') {
            payloadCell.appendChild(h('span', { class: 'small muted', text: ref.available ? fmtBytes(ref.bytes) + ' · ' + ref.chunks + ' chunk(s)' : 'payload reference without bytes in this resource record' }));
          }
        });
      } else if (side === 'resource') {
        payloadCell.appendChild(h('span', { class: 'small muted', text: 'descriptor-only; bytes belong to the pass-side export' }));
      } else {
        payloadCell.appendChild(h('span', { class: 'small muted', text: 'no captured bytes' }));
      }
      return [
        binding.passId ? passLink(binding.passId, binding.passId) : (binding.passPointer || '—'),
        binding.memberPath || '—',
        entry.BaseType || '—',
        entry.MemberOffset === null || entry.MemberOffset === undefined ? '—' : String(entry.MemberOffset),
        entry.ElementStrideBytes === null || entry.ElementStrideBytes === undefined ? '—' : String(entry.ElementStrideBytes),
        entry.ArrayNumElements === null || entry.ArrayNumElements === undefined ? '—' : String(entry.ArrayNumElements),
        entry.ResourceKind || '—',
        entry.Pointer || entry.NativePointer || '—',
        entry.DumpedAs || entry.DumpFile || entry.ExportFirstFile || '—',
        entry.DumpStatus || (completion.dumped ? 'Dumped' : '—'),
        completion.dumped ? 'Dumped / ' + (completion.timing || '') : (completion.observed === false ? 'unobserved' : 'not completed'),
        timing.timingRisk || entry.TimingRisk || '—',
        payloadCell,
        h('details', {}, h('summary', { text: 'all raw fields' }), h('pre', { class: 'json small', text: JSON.stringify(entry, null, 2) }))
      ];
    });
    return h('div', {},
      h('p', { class: 'table-note', text: 'Every binding member is listed, including nested member paths, arrays, offsets, strides, pointers, typed view ranges, export scope and skip reasons. ExportArrayFirstInViewUnits/ExportArrayCountInViewUnits count array view units; ExportFaceFirst/ExportFaceCount are cube-face ranges. They are distinct and are never interchanged. OwnerPassExecutionDependencyClaimed=false and SourceStateProvenAtScheduling=false mean a completed file/hash proves completion only, never owner-time value identity. A sampler row records only its bound pointer and StaticInitializerCaptured=false; the sampler initializer remains source-derived/unknown and is not inferred from the pointer.' }),
      dataTable(['Pass', 'Member path', 'Base type', 'Offset', 'Stride', 'Array', 'Resource kind', 'Pointer', 'Dumped as / file', 'Dump status', 'Completion', 'Timing risk', 'Payload(s)', 'Raw'], rows));
  }

  function renderReferencedStruct(ref, record) {
    var card = h('div', { class: 'field-card' });
    card.appendChild(h('h4', { text: ref.member || '(referenced member)' }));
    card.appendChild(h('p', { class: 'small muted', text: 'Content pointer ' + (ref.contentPointer || '—') + ' · metadata pointer ' + (ref.metadataPointer || '—') }));
    if (!ref.payload) {
      card.appendChild(notice('Referenced content not captured', 'No raw block exists for this referenced struct. Names may be available from metadata; values are not fabricated.', 'warn'));
    } else {
      var p = ref.payload;
      card.appendChild(h('p', { class: 'small', text: 'Captured block ' + p.bytes + ' B · SHA-256 ' + p.sha256 + ' · ' + (p.chunks || []).length + ' chunks' }));
      var host = h('div');
      card.appendChild(host);
      var btn = h('button', { type: 'button', text: 'Show captured referenced block', on: { click: function () {
        var meta = ref.metadata || null;
        var entries = meta ? flattenStructure(String(ref.metadataPointer || ''), 0, '', 0, new Set()) : [];
        explorer.readRange(p, 0, p.bytes).then(function (bytes) {
          clear(host);
          if (entries.length) host.appendChild(renderMetadataValues(entries, bytes, 'Referenced struct'));
          renderHexViewer(host, p, 0);
        }).catch(function (error) { host.appendChild(notice('Referenced block failed', error.message, 'danger')); });
      } } });
      card.appendChild(btn);
    }
    if (ref.metadata) card.appendChild(details('Referenced structure metadata', h('pre', { class: 'json', text: JSON.stringify(ref.metadata, null, 2) })));
    card.appendChild(details('Complete ReferencedStructs member record (all source fields)', h('pre', { class: 'json', text: JSON.stringify(ref, null, 2) })));
    return card;
  }

  // --------------------------------------------------------------- coverage
  function renderCoverage() {
    var host = $('coverage-content');
    clear(host);
    var counts = state.coverage.counts || state.index.counts || {};
    host.appendChild(h('h2', { text: 'Coverage and explicit gaps' }));
    host.appendChild(notice('Coverage is not assumed complete', state.coverage.statement || 'Coverage is not claimed complete. Every known gap is listed below.', state.coverage.complete ? 'ok' : 'warn'));
    var cards = [
      ['Native records', counts.nativeRecords, ''],
      ['Resources', counts.resources, ''],
      ['Passes', counts.passes, ''],
      ['Parameter payloads', counts.parameterBlocks, ''],
      ['Payload files', counts.payloadFiles, ''],
      ['Raw payload bytes', fmtBytes(counts.payloadBytes), ''],
      ['Unique chunk bytes', fmtBytes(counts.uniqueChunkBytes), ''],
      ['Compressed chunk bytes', fmtBytes(counts.compressedChunkBytes), ''],
      ['Unique chunks', counts.chunkFiles, ''],
      ['Resources missing payload', counts.resourcesMissingPayload, counts.resourcesMissingPayload ? 'is-warn' : ''],
      ['Unsupported formats', counts.unsupportedResourceFormats, counts.unsupportedResourceFormats ? 'is-warn' : ''],
      ['Pass draw counts captured', counts.passDrawCountsCaptured, (counts.passDrawCountsCaptured || 0) > 0 ? '' : 'is-warn'],
      ['Pass draw counts not captured', counts.passDrawCountsMissing, (counts.passDrawCountsMissing || 0) > 0 ? 'is-warn' : ''],
      ['Resource source-formula fields', counts.resourceSourceEquationFields, counts.resourceSourceEquationFields ? 'is-warn' : ''],
      ['Explicit gaps', counts.gapCount, counts.gapCount ? 'is-danger' : 'is-ok']
    ];
    var cardGrid = h('div', { class: 'cards' });
    cards.forEach(function (pair) {
      var card = h('div', { class: 'card ' + (pair[2] || '') });
      card.appendChild(h('span', { class: 'card-value', text: fmtNumber(pair[1]) }));
      card.appendChild(h('span', { class: 'card-label', text: pair[0] }));
      cardGrid.appendChild(card);
    });
    host.appendChild(cardGrid);
    if ((counts.passDrawCountsMissing || 0) > 0) {
      host.appendChild(notice('Pass draw counts are explicitly not captured',
        counts.passDrawCountsMissing + ' of ' + (counts.passes || 0) + ' passes keep drawCount = null because Base/PassDrawCounts.json has no record for them. Null means not captured; the explorer never substitutes zero.', 'warn'));
    }

    host.appendChild(h('h3', { text: 'Native record inventory by kind' }));
    var kinds = counts.nativeRecordKinds || {};
    var kindRows = Object.keys(kinds).sort().map(function (key) { return [key, String(kinds[key])]; });
    host.appendChild(kindRows.length ? dataTable(['Record kind', 'Count'], kindRows) : h('p', { class: 'muted', text: 'No native records.' }));
    host.appendChild(h('p', { class: 'table-note' }, 'All native JSON objects, CSV rows, text records and raw payloads are counted. Full details: ', h('a', { href: packageUrl('inventory.json'), text: 'inventory.json' })));

    host.appendChild(h('h3', { text: 'Explicit coverage categories' }));
    var categories = state.coverage.categories || [];
    if (!categories.length) {
      host.appendChild(h('p', { class: 'muted small', text: 'This package did not record explicit coverage categories (older package format).' }));
    } else {
      categories.forEach(function (category) {
        var card = h('div', { class: 'field-card' });
        card.appendChild(h('h4', { text: category.label + ' (' + category.count + ')' }));
        if (!category.count) {
          card.appendChild(h('p', { class: 'muted small', text: 'No records in this category.' }));
        } else {
          card.appendChild(h('p', { class: 'small muted', text: 'This category is separate from the other coverage categories; a record may also appear in explicit gaps.' }));
          card.appendChild(details('First ' + Math.min(category.items.length, 50) + ' record(s)' + (category.omitted ? ' · ' + category.omitted + ' omitted from this preview' : ''), h('pre', { class: 'json', text: JSON.stringify(category.items.slice(0, 50), null, 2) })));
        }
        host.appendChild(card);
      });
    }

    if ((state.coverage.unsupportedFormats || []).length) {
      host.appendChild(h('h3', { text: 'Unsupported semantic decodes (raw retained)' }));
      host.appendChild(dataTable(['Resource', 'Format', 'Reason'], state.coverage.unsupportedFormats.map(function (item) {
        return [resourceLink(item.resourceId, item.name), item.format || '—', item.reason || 'No explicit decoder.'];
      })));
    }

    host.appendChild(h('h3', { text: 'Explicit gaps' }));
    var gapControls = h('div', { class: 'controls', style: 'grid-template-columns: 1fr 12rem auto; margin-bottom: .7rem' });
    var gapSearch = h('input', { type: 'search', placeholder: 'Filter gaps by kind, message or source', 'aria-label': 'Filter gaps' });
    var severity = h('select', { 'aria-label': 'Filter gap severity' });
    [['all', 'All severities'], ['error', 'Error'], ['warning', 'Warning'], ['info', 'Info']].forEach(function (pair) { severity.appendChild(h('option', { value: pair[0], text: pair[1] })); });
    gapControls.appendChild(h('div', { class: 'search-field' }, h('label', { text: 'Gap search' }), gapSearch));
    gapControls.appendChild(h('div', { class: 'filter-field' }, h('label', { text: 'Severity' }), severity));
    gapControls.appendChild(h('span', { class: 'small muted', text: 'Source paths open the retained native file.' }));
    host.appendChild(gapControls);
    var gapHost = h('div');
    host.appendChild(gapHost);
    function drawGaps() {
      state.gapSearch = gapSearch.value;
      state.gapSeverity = severity.value;
      var gaps = (state.coverage.gaps || []).filter(function (gap) {
        if (state.gapSeverity !== 'all' && gap.severity !== state.gapSeverity) return false;
        if (!state.gapSearch.trim()) return true;
        var hay = [gap.kind, gap.message, gap.source, gap.scope].join(' ').toLowerCase();
        return hay.indexOf(state.gapSearch.toLowerCase()) !== -1;
      });
      clear(gapHost);
      gapHost.appendChild(h('p', { class: 'small muted', text: gaps.length + ' of ' + (state.coverage.gaps || []).length + ' gaps shown; ' + state.gapLimit + ' limit.' }));
      var list = h('ul', { class: 'gap-list' });
      gaps.slice(0, state.gapLimit).forEach(function (gap) {
        var li = h('li', { class: 'gap-item severity-' + (gap.severity || 'info') });
        li.appendChild(h('div', { class: 'badge-row' }, badge(gap.kind || 'gap', gap.severity === 'error' ? 'danger' : gap.severity === 'warning' ? 'warn' : 'accent'), badge(gap.scope || 'capture'), gap.severity ? badge(gap.severity) : null));
        li.appendChild(h('p', { text: gap.message || '' }));
        if (gap.source) {
          var path = String(gap.source).replace(/:\d+$/, '');
          li.appendChild(h('p', { class: 'gap-meta' }, h('a', { href: packageUrl('native/' + path), text: 'Native source: ' + path })));
        }
        if (gap.schemaId) li.appendChild(h('p', { class: 'gap-meta', text: 'Schema: ' + gap.schemaId }));
        if (gap.fields) li.appendChild(h('p', { class: 'gap-meta', text: 'Fields: ' + (Array.isArray(gap.fields) ? gap.fields.join(', ') : gap.fields) }));
        list.appendChild(li);
      });
      gapHost.appendChild(list);
      if (gaps.length > state.gapLimit) {
        gapHost.appendChild(h('button', { type: 'button', text: 'Show more gaps (' + (gaps.length - state.gapLimit) + ' remaining)', on: { click: function () { state.gapLimit += GAP_PAGE; drawGaps(); } } }));
      }
    }
    gapSearch.addEventListener('input', function () { state.gapLimit = GAP_PAGE; drawGaps(); });
    severity.addEventListener('change', function () { state.gapLimit = GAP_PAGE; drawGaps(); });
    drawGaps();

    host.appendChild(h('h3', { text: 'Coverage counts by schema status' }));
    host.appendChild(dataTable(['Status', 'Resources', 'Passes'], [
      ['Matched', String(counts.schemaMatchedResources || 0), String(counts.schemaMatchedPasses || 0)],
      ['Ambiguous', String(counts.schemaAmbiguousResources || 0), '—'],
      ['Unmatched', String(counts.schemaUnmatchedResources || 0), String((state.passes.total || 0) - (counts.schemaMatchedPasses || 0))],
      ['Schema status', state.index.schema ? state.index.schema.status : 'unknown', '—']
    ]));
  }

  // ----------------------------------------------------------------- schema
  function renderSchema() {
    var host = $('schema-content');
    clear(host);
    host.appendChild(h('h2', { text: 'Source schema' }));
    if (!state.schema || state.schema.status === 'missing' || state.schema.status === 'invalid') {
      host.appendChild(notice('Source schema unavailable', 'No joined source schema is present in this package. Raw metadata and bytes remain inspectable, but units, quantities, source anchors, packed-field offsets and math terms are unavailable. Supply source-schema.json at package time; nothing was inferred in its place.', 'danger'));
      host.appendChild(jsonDetails('Schema placeholder', state.schema || {}, true));
      return;
    }
    var rs = Array.isArray(state.schema.resourceSchemas) ? state.schema.resourceSchemas : [];
    var ps = Array.isArray(state.schema.parameterSchemas) ? state.schema.parameterSchemas : [];
    var samplerBindings = Array.isArray(state.schema.samplerBindings) ? state.schema.samplerBindings : [];
    var coverageGaps = Array.isArray(state.schema.coverageGaps)
      ? state.schema.coverageGaps
      : (Array.isArray(state.schema.gaps) ? state.schema.gaps : []);
    var parts = Array.isArray(state.schema.parts) ? state.schema.parts : [];
    var variantCount = rs.reduce(function (sum, schema) {
      return sum + (schema.storage && Array.isArray(schema.storage.formatVariants) ? schema.storage.formatVariants.length : 0);
    }, 0);
    var gatedFieldCount = rs.reduce(function (sum, schema) {
      return sum + (Array.isArray(schema.fields) ? schema.fields.filter(function (field) {
        return Array.isArray(field.appliesToFormats) && field.appliesToFormats.length;
      }).length : 0);
    }, 0);
    host.appendChild(notice('Selector-based joins only', 'Resource schemas require an explicit resource-name selector and, when supplied, a pass-name selector. Multiple matches are ambiguous and all candidates are shown. Decode strings that are not known numeric tags are never evaluated.', 'ok'));
    host.appendChild(kvGrid([
      ['Schema version', state.schema.schemaVersion !== undefined ? state.schema.schemaVersion : '—'],
      ['Status', state.schema.status || '—'],
      ['Engine', state.schema.engine && state.schema.engine.version ? state.schema.engine.version : '—'],
      ['Resource schemas', String(rs.length)],
      ['Parameter schemas', String(ps.length)],
      ['Source refs', String(state.sourceRefMap.size)],
      ['Sampler bindings', String(samplerBindings.length)],
      ['Schema coverage gaps', String(coverageGaps.length)],
      ['Format variants', String(variantCount) + ' (' + gatedFieldCount + ' format-gated field' + (gatedFieldCount === 1 ? '' : 's') + ')'],
      ['Schema parts', String(parts.length)],
      ['Transforms', String((state.schema.transforms || []).length)],
      ['Math terms', String((state.schema.mathTerms || []).length)]
    ]));
    if (state.schema.coverage) host.appendChild(jsonDetails('Declared schema coverage', state.schema.coverage, true));

    host.appendChild(h('h3', { text: 'Resource schemas' }));
    if (!rs.length) host.appendChild(h('p', { class: 'muted', text: 'No resource schemas.' }));
    rs.forEach(function (schema, index) { host.appendChild(renderSchemaEntry(schema, 'resource', index)); });
    host.appendChild(h('h3', { text: 'Parameter schemas' }));
    if (!ps.length) host.appendChild(h('p', { class: 'muted', text: 'No parameter schemas.' }));
    ps.forEach(function (schema, index) { host.appendChild(renderSchemaEntry(schema, 'parameter', index)); });

    host.appendChild(h('h3', { text: 'Sampler bindings' }));
    if (!samplerBindings.length) host.appendChild(h('p', { class: 'muted', text: 'No sampler bindings declared.' }));
    samplerBindings.forEach(function (binding) {
      var card = h('div', { class: 'field-card' });
      card.appendChild(h('h4', { text: binding.id || binding.parameter || 'sampler binding' }));
      card.appendChild(h('div', { class: 'badge-row' },
        badge(binding.parameter || '(no parameter)'),
        binding.qualifiedShader ? badge('shader: ' + binding.qualifiedShader) : null,
        binding.decodingStatus ? badge('decoding: ' + binding.decodingStatus) : null));
      if (binding.assignment) card.appendChild(h('div', { class: 'equation', text: binding.assignment }));
      if (binding.derivation) card.appendChild(h('p', { class: 'small muted', text: 'Derivation: ' + binding.derivation }));
      if (binding.runtimeGate) card.appendChild(h('p', { class: 'small muted', text: 'Runtime gate: ' + binding.runtimeGate }));
      if (binding.state) card.appendChild(details('Sampler state', h('pre', { class: 'json', text: JSON.stringify(binding.state, null, 2) }), true));
      if (binding.sourcePath) card.appendChild(h('p', { class: 'small muted', text: 'Source: ' + binding.sourcePath + (binding.sourceLine ? ':' + binding.sourceLine : '') }));
      appendSourceRefs(card, binding.sourceRefs || []);
      host.appendChild(card);
    });

    host.appendChild(h('h3', { text: 'Schema coverage gaps (unresolved source facts)' }));
    if (!coverageGaps.length) host.appendChild(h('p', { class: 'muted', text: 'No schema coverage gaps declared.' }));
    coverageGaps.forEach(function (gap, index) {
      var card = h('div', { class: 'field-card' });
      card.appendChild(h('h4', { text: gap.name || gap.id || ('schema gap ' + (index + 1)) }));
      if (gap.missingSourceFact) card.appendChild(h('p', { text: gap.missingSourceFact }));
      if (gap.affects) card.appendChild(h('p', { class: 'small muted', text: 'Affects: ' + gap.affects }));
      if (gap.appliesTo) card.appendChild(h('p', { class: 'small muted', text: 'Applies to: ' + (Array.isArray(gap.appliesTo) ? gap.appliesTo.join(', ') : gap.appliesTo) }));
      if (gap.impact) card.appendChild(h('p', { class: 'small muted', text: 'Impact: ' + gap.impact }));
      host.appendChild(card);
    });

    host.appendChild(h('h3', { text: 'Schema parts' }));
    if (!parts.length) host.appendChild(h('p', { class: 'muted', text: 'No merged schema parts declared.' }));
    else host.appendChild(dataTable(['Part', 'SHA-256', 'Reference namespace', 'Resource schemas', 'Parameter schemas', 'Source refs'], parts.map(function (part) {
      return [part.path || '—', part.sha256 || '—', part.referenceNamespace || '—',
        String(part.resourceSchemas || 0), String(part.parameterSchemas || 0), String(part.sourceRefs || 0)];
    })));

    host.appendChild(h('h3', { text: 'Transforms, math terms and source anchors' }));
    (state.schema.transforms || []).forEach(function (t, i) {
      var card = h('div', { class: 'field-card' });
      card.appendChild(h('h4', { text: t.name || t.id || ('transform ' + (i + 1)) }));
      card.appendChild(h('div', { class: 'equation', text: t.equation || t.expression || JSON.stringify(t) }));
      appendSourceRefs(card, t.sourceRefs);
      host.appendChild(card);
    });
    (state.schema.mathTerms || []).forEach(function (t, i) {
      var card = h('div', { class: 'field-card' });
      card.appendChild(h('h4', { text: t.term || t.name || t.id || ('term ' + (i + 1)) }));
      if (t.definition) card.appendChild(h('p', { text: t.definition }));
      if (t.equation) card.appendChild(h('div', { class: 'equation', text: t.equation }));
      appendSourceRefs(card, t.sourceRefs);
      host.appendChild(card);
    });
    if (state.sourceRefMap.size) {
      host.appendChild(h('h3', { text: 'All source references' }));
      state.sourceRefMap.forEach(function (ref, id) {
        var card = h('div', { class: 'field-card' });
        card.appendChild(h('h4', { text: id }));
        appendSourceRefs(card, [ref]);
        host.appendChild(card);
      });
    }
    host.appendChild(jsonDetails('Complete source schema as retained', state.schema, false));
  }

  function renderSchemaEntry(schema, kind, index) {
    var card = h('div', { class: 'field-card' });
    card.appendChild(h('h4', { text: schema.id || (kind + ' schema ' + (index + 1)) }));
    var selectors = Array.isArray(schema.selectors) ? schema.selectors : [];
    var selectorText = selectors.map(function (s) { return [s.resourceNameRegex, s.passNameRegex].filter(Boolean).join(' + '); }).join('; ') || (schema.qualifiedShader ? 'qualifiedShader=' + schema.qualifiedShader : '—');
    card.appendChild(h('p', { class: 'small' }, h('strong', { text: 'Selectors: ' }), selectorText));
    card.appendChild(h('div', { class: 'badge-row' }, schema.quantity ? badge('quantity: ' + schema.quantity) : null, schema.units ? badge('units: ' + schema.units) : null, schema.coordinateSpace ? badge('space: ' + schema.coordinateSpace) : null, schema.storage ? badge('storage: ' + storageLabel(schema.storage)) : null, schema.decodingStatus ? badge('decoding: ' + schema.decodingStatus) : null));
    var fields = Array.isArray(schema.fields) ? schema.fields : [];
    if (fields.length) {
      card.appendChild(dataTable(['Field', 'Type', 'Byte offset', 'Bit offset/width', 'Count', 'Applies to / condition / variant', 'Decode / source formula', 'Units', 'Source anchors'], fields.map(function (field) {
        var applicability = [];
        if (Array.isArray(field.appliesToFormats) && field.appliesToFormats.length) applicability.push(field.appliesToFormats.join(', '));
        if (field.condition) applicability.push('condition: ' + field.condition);
        if (field.variant) applicability.push('variant: ' + field.variant);
        if (field.optional) applicability.push('optional');
        return [
          field.name || '(unnamed)', field.type || '—',
          field.byteOffset === null || field.byteOffset === undefined ? '—' : String(field.byteOffset),
          field.bitOffset === null || field.bitOffset === undefined ? '—' : String(field.bitOffset) + (field.bitWidth ? '/' + field.bitWidth : ''),
          String(field.count || 1), applicability.length ? applicability.join(' · ') : '—', field.decode || '—', field.units || '—',
          { node: (function () { var d = h('div'); appendSourceRefs(d, field.sourceRefs); return d; })() }
        ];
      })));
    } else {
      card.appendChild(h('p', { class: 'muted small', text: 'No fields declared.' }));
    }
    if (schema.gaps) card.appendChild(details('Schema-declared gaps', h('pre', { text: JSON.stringify(schema.gaps, null, 2) })));
    appendNamedSourceRefs(card, 'Producer sources', schema.producerSources || []);
    appendNamedSourceRefs(card, 'Consumer sources', schema.consumerSources || []);
    renderFormatVariants(card, schema);
    renderTransforms(card, schema);
    renderTerms(card, schema.mathTerms);
    return card;
  }

  // ---------------------------------------------------------------- capture
  function renderCapture() {
    var host = $('capture-content');
    clear(host);
    host.appendChild(h('h2', { text: 'Capture metadata' }));
    if (state.capture.fixture) host.appendChild(notice('Synthetic fixture', state.capture.notice || 'This package is a labelled synthetic fixture, not runtime evidence.', 'warn'));
    if (state.capture.evidenceKind === 'unavailable') host.appendChild(notice('No capture present', 'This is a placeholder package with no runtime records. Nothing on this page is inferred.', 'danger'));
    host.appendChild(kvGrid([
      ['Dump status', state.capture.status || '—'],
      ['Evidence kind', state.capture.evidenceKind || '—'],
      ['Adapter', state.index.adapter || '—'],
      ['Generated', state.index.generatedUtc || '—'],
      ['Native metadata files', String((state.capture.nativeFiles || []).length)]
    ]));
    if (state.capture.screenshot) {
      host.appendChild(h('h3', { text: 'Captured frame screenshot' }));
      host.appendChild(h('img', { class: 'screenshot', src: packageUrl(state.capture.screenshot.nativePath), alt: 'Screenshot retained from the captured DumpGPU frame. Synthetic fixtures are labelled and are not runtime evidence.', loading: 'lazy' }));
      host.appendChild(h('p', { class: 'small muted', text: state.capture.screenshot.nativePath + ' · ' + fmtBytes(state.capture.screenshot.bytes) + ' · SHA-256 ' + state.capture.screenshot.sha256 }));
    }
    host.appendChild(h('h3', { text: 'Base/Infos.json fields' }));
    var infos = state.capture.infos || {};
    var infoRows = Object.keys(infos).sort().map(function (key) {
      var value = infos[key];
      return [key, typeof value === 'object' ? JSON.stringify(value) : String(value)];
    });
    host.appendChild(infoRows.length ? dataTable(['Field', 'Value'], infoRows) : h('p', { class: 'muted', text: 'No Infos.json fields were retained.' }));

    host.appendChild(h('h3', { text: 'DumpGPU console variables' }));
    var cvars = state.capture.consoleVariables || [];
    host.appendChild(cvars.length ? dataTable(['Name', 'Type', 'Set by', 'Value'], cvars.map(function (row) {
      return [row.Name || '—', row.Type || '—', row.SetBy || '—', row.Value || '—'];
    })) : h('p', { class: 'muted', text: 'No ConsoleVariables.csv rows were retained.' }));

    host.appendChild(h('h3', { text: 'Native RHI inputs (outside ResourceDescs.json)' }));
    var nativeItems = (state.nativeInputs && state.nativeInputs.items) || [];
    if (!nativeItems.length) {
      host.appendChild(h('p', { class: 'muted small', text: 'No native RHI input identity records were captured. Native uniform buffer pointers and bytes remain visible from the pass records.' }));
    } else {
      var nativeRows = nativeItems.map(function (item) {
        var detail = item.kind === 'native-buffer'
          ? (item.views || []).length + ' typed view subrange(s)'
          : (item.exports || []).length + ' exported subresource(s)';
        return [item.kind, item.name || '—', item.pointer || '—', item.id,
          item.kind === 'native-buffer' ? String(item.byteSize || '—') : ([item.extent ? item.extent.join(' × ') : '—', item.format || '—'].join(' · ')),
          detail,
          h('details', {}, h('summary', { text: 'raw identity' }), h('pre', { class: 'json small', text: JSON.stringify(item, null, 2) }))];
      });
      host.appendChild(h('p', { class: 'small muted', text: 'Native RHI buffers and textures are bound through native uniform buffers; they are not RDG ResourceDescs and are listed here with their exact pointer, view ranges, export files and timing risks.' }));
      host.appendChild(dataTable(['Kind', 'Native name', 'Pointer', 'Id', 'ByteSize / extent · format', 'Views / exports', 'Raw'], nativeRows));
    }

    host.appendChild(h('h3', { text: 'Retained native metadata files' }));
    host.appendChild(dataTable(['Path', 'Kind', 'Bytes', 'SHA-256', 'Open'], (state.capture.nativeFiles || []).map(function (file) {
      return [file.path, file.kind || '—', fmtBytes(file.bytes), { text: shortSha(file.sha256), class: 'mono' }, h('a', { href: packageUrl(file.packagePath), text: 'Download' })];
    })));
  }

  // --------------------------------------------------------------- wiring
  function wireTabs() {
    ['explore', 'coverage', 'schema', 'capture'].forEach(function (tab) {
      var button = $('tab-' + tab);
      button.addEventListener('click', function () {
        navigate('#' + tab, true);
      });
      button.addEventListener('keydown', function (event) {
        var tabs = ['explore', 'coverage', 'schema', 'capture'];
        var idx = tabs.indexOf(tab);
        if (event.key === 'ArrowRight') { event.preventDefault(); navigate('#' + tabs[(idx + 1) % tabs.length], true); }
        if (event.key === 'ArrowLeft') { event.preventDefault(); navigate('#' + tabs[(idx - 1 + tabs.length) % tabs.length], true); }
      });
    });
  }

  function wireFilters() {
    var search = $('search');
    var kind = $('kind-filter');
    var status = $('status-filter');
    var clearButton = $('clear-filters');
    var timer = null;
    search.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        state.search = search.value;
        state.passLimit = LIST_PAGE;
        state.resourceLimit = LIST_PAGE;
        renderLists();
      }, 80);
    });
    kind.addEventListener('change', function () { state.kind = kind.value; state.resourceLimit = LIST_PAGE; renderLists(); });
    status.addEventListener('change', function () { state.status = status.value; state.passLimit = LIST_PAGE; state.resourceLimit = LIST_PAGE; renderLists(); });
    clearButton.addEventListener('click', function () {
      search.value = ''; kind.value = 'all'; status.value = 'all';
      state.search = ''; state.kind = 'all'; state.status = 'all';
      state.passLimit = LIST_PAGE; state.resourceLimit = LIST_PAGE;
      renderLists();
      search.focus();
    });
  }

  function wireTheme() {
    var saved = null;
    try { saved = localStorage.getItem('frame-explorer-theme'); } catch (e) { /* private mode */ }
    if (saved === 'dark' || saved === 'light') document.documentElement.setAttribute('data-theme', saved);
    $('theme-toggle').addEventListener('click', function () {
      var current = document.documentElement.getAttribute('data-theme');
      var next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('frame-explorer-theme', next); } catch (e) { /* ignore */ }
    });
  }

  function wireHash() {
    window.addEventListener('hashchange', function () { navigate(window.location.hash, false); });
  }

  async function init() {
    wireTheme();
    try {
      await explorer.load();
    } catch (error) {
      showFatal(error);
      return;
    }
    renderPackageBanner();
    renderLists();
    wireTabs();
    wireFilters();
    wireHash();
    if (window.location.hash) navigate(window.location.hash, false);
    else navigate('#explore', false);
  }

  if (typeof module === 'object' && module.exports) {
    module.exports = {
      PackageStore: PackageStore,
      state: state,
      storedChannelNames: storedChannelNames,
      channelList: channelList,
      textureLayout: textureLayout,
      subresourceInfo: subresourceInfo,
      payloadLayout: payloadLayout,
      planOverview: planOverview,
      defaultOverviewSteps: defaultOverviewSteps,
      formatForResource: formatForResource,
      payloadLabel: payloadLabel,
      bindingPayloadRefs: bindingPayloadRefs,
      viewportInfo: viewportInfo
    };
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }
})();
