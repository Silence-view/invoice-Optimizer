/* Command palette, keyboard shortcuts and the Appearance panel.
   DOM-only: this module clicks or focuses existing controls and sets
   location.hash for #case/<id> and #batch/<id>. It never reads or writes
   app state and never calls fetch, so every permission check in app.js
   still applies. Display preferences go through window.IOTheme. */
(function () {
  'use strict';
  var $ = function (selector, root) { return (root || document).querySelector(selector); };
  var $$ = function (selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); };
  var esc = function (value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  var isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
  var TABS = [
    ['overview', 'Overview', 'o'], ['review', 'Review queue', 'r'], ['invoices', 'Invoices', 'i'], ['insights', 'Insights', 'n'],
    ['delivery', 'Delivery', 'd'], ['data', 'Data & model', 'm'], ['email', 'Email setup', 'e'], ['team', 'Team access', 't']
  ];
  var ACCENTS = [['indigo', 'Indigo', 274], ['blue', 'Blue', 252], ['teal', 'Teal', 200], ['emerald', 'Emerald', 160], ['amber', 'Amber', 58], ['rose', 'Rose', 8]];

  function announce(text) {
    var region = $('#sr-status');
    if (!region) return;
    region.textContent = '';
    setTimeout(function () { region.textContent = text; }, 30);
  }
  function visible(el) { return !!el && !el.hidden && !el.disabled && el.getClientRects().length > 0; }
  function workspaceOpen() { var w = $('#workspace'); return !!w && !w.hidden; }
  function modalOpen() { return $$('dialog[open]').some(function (d) { return d.id !== 'command-palette'; }); }
  function typing(target) {
    if (!target || !target.closest) return false;
    return !!target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]');
  }
  function clickTab(tab) {
    var button = $('#navigation [data-tab="' + tab + '"]');
    if (visible(button)) { button.click(); return true; }
    return false;
  }
  var pref = function (key, value) { if (window.IOTheme) window.IOTheme.set(key, value); };
  var prefGet = function (key) { return window.IOTheme ? window.IOTheme.get(key) : null; };

  /* ---------------------------------------------------------------- */
  /* Command palette                                                    */
  /* ---------------------------------------------------------------- */
  var palette = $('#command-palette'), input, list, items = [], activeIndex = 0, mode = 'all', scope = 'all', returnFocus = null;
  /* Result scopes (TradingView/Coinbase style filter pills). Tab cycles them. */
  var SCOPES = [['all', 'All'], ['page', 'Pages'], ['record', 'Records'], ['action', 'Actions'], ['setting', 'Settings']];
  var ICONS = {
    record: '<svg class="icon" viewBox="0 0 16 16" focusable="false"><path d="M3.5 1.5h6l3 3v10h-9z"/><path d="M9.5 1.5v3h3M6 8h4.5M6 10.5h3"/></svg>',
    find: '<svg class="icon" viewBox="0 0 16 16" focusable="false"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg>',
    action: '<svg class="icon" viewBox="0 0 16 16" focusable="false"><path d="M9 1.5L3.5 9H8l-1 5.5L12.5 7H8z"/></svg>',
    setting: '<svg class="icon" viewBox="0 0 16 16" focusable="false"><path d="M2 4.5h7M12 4.5h2M2 11.5h2M7 11.5h7"/><circle cx="10.5" cy="4.5" r="1.5"/><circle cx="5.5" cy="11.5" r="1.5"/></svg>',
    help: '<svg class="icon" viewBox="0 0 16 16" focusable="false"><rect x="1.5" y="4" width="13" height="8.5" rx="1.5"/><path d="M4 7h1M7 7h1M10 7h2M4.5 9.5h7"/></svg>'
  };
  var HEX64 = /^[0-9a-f]{64}$/i;

  function build() {
    if (!palette || input) return;
    palette.innerHTML =
      '<div class="palette-search"><span class="palette-search-icon" aria-hidden="true">' + ICONS.find + '</span>' +
      '<input class="palette-input" type="text" role="combobox" aria-expanded="true" aria-autocomplete="list" aria-controls="palette-list" aria-label="Search commands, pages and records" placeholder="Pages, movement IDs, actions…" autocomplete="off" spellcheck="false">' +
      '<button type="button" class="palette-esc" data-palette-close aria-label="Close command palette"><kbd>Esc</kbd></button></div>' +
      '<div class="palette-scopes" role="group" aria-label="Filter results">' + SCOPES.map(function (s) {
        return '<button type="button" class="palette-scope" data-palette-scope="' + s[0] + '" aria-pressed="' + (s[0] === 'all') + '" tabindex="-1">' + esc(s[1]) + '</button>';
      }).join('') + '</div>' +
      '<div id="palette-list" role="listbox" aria-label="Results"></div>' +
      '<p class="palette-empty muted" hidden>No matching commands. Try a page name, an action or the start of a movement ID.</p>' +
      '<div class="palette-footer" aria-hidden="true"><span><kbd>↑</kbd><kbd>↓</kbd> navigate</span><span><kbd>↵</kbd> open</span><span><kbd>Tab</kbd> filter</span><span><kbd>Esc</kbd> close</span><span class="palette-footer-end"><kbd>?</kbd> all shortcuts</span></div>';
    input = $('.palette-input', palette);
    list = $('#palette-list', palette);
    input.addEventListener('input', function () { if (mode === 'help') { mode = 'all'; palette.classList.remove('is-help'); } render(); });
    input.addEventListener('keydown', onPaletteKey);
    palette.addEventListener('click', function (event) {
      var scopeButton = event.target.closest('[data-palette-scope]');
      if (scopeButton) { setScope(scopeButton.dataset.paletteScope); input.focus(); return; }
      if (event.target.closest('[data-palette-close]')) palette.close();
    });
    list.addEventListener('click', function (event) {
      var option = event.target.closest('[role="option"]');
      if (option) run(Number(option.dataset.index));
    });
    list.addEventListener('pointermove', function (event) {
      var option = event.target.closest('[role="option"]');
      if (option && Number(option.dataset.index) !== activeIndex) setActive(Number(option.dataset.index), false);
    });
    palette.addEventListener('close', function () {
      var target = returnFocus;
      returnFocus = null;
      if (target && document.contains(target) && !modalOpen() && (document.activeElement === document.body || palette.contains(document.activeElement))) target.focus();
    });
    palette.addEventListener('click', function (event) { if (event.target === palette) palette.close(); });
  }

  /* A record row's readable identity: customer, movement ID and amount, so
     two cases of one customer never collapse into the same label. */
  function rowFacts(control) {
    var row = control.closest('tr, .review-card, .invoice-card, .review-item, .swipe-evidence-card') || control;
    var clean = function (node) { return node ? node.textContent.replace(/\s+/g, ' ').trim() : ''; };
    var title = clean(row.querySelector('.cell-main, h3, strong')) || clean(control);
    var id = clean(row.querySelector('.mono'));
    var money = (row.textContent.match(/(?:£|[A-Z]{3} )-?[\d,]+\.\d{2}/) || [])[0] || '';
    return { title: title.slice(0, 80), meta: [id, money].filter(Boolean).join(' · ').slice(0, 90) };
  }

  /* Wait (bounded) for a control rendered by app.js after a tab change. */
  function whenPresent(selector, then, tries) {
    var el = $(selector);
    if (visible(el) && !el.disabled) { then(el); return; }
    if ((tries || 0) > 40) return;
    setTimeout(function () { whenPresent(selector, then, (tries || 0) + 1); }, 100);
  }
  function findMovement(prefix) {
    var apply = function () {
      whenPresent('#case-search', function (field) {
        field.value = prefix;
        field.dispatchEvent(new Event('input', { bubbles: true }));
        field.focus();
        announce('Review queue filtered to movement IDs starting with ' + prefix);
      });
    };
    var current = $('#navigation [data-tab="review"][aria-current="page"]');
    if (!current) clickTab('review');
    apply();
  }

  function commands(query) {
    var out = [], q = query.trim();
    if (mode === 'help') {
      shortcutList().forEach(function (s) { out.push({ group: 'Keyboard shortcuts', kind: 'help', label: s[1], keys: s[0], run: s[2] }); });
      return out;
    }
    var bare = q.replace(/^(case|batch)\s+/i, ''), kind = ((q.match(/^(case|batch)\s+/i) || [])[1] || '').toLowerCase();
    /* Exact internal IDs are 64-hex digests; anything else is a movement ID prefix. */
    if (HEX64.test(bare)) {
      if (kind !== 'batch') out.push({ group: 'Jump to record', kind: 'record', label: 'Open case ' + bare.slice(0, 12) + '…', meta: 'Exact case identifier', always: true, run: function () { location.hash = '#case/' + encodeURIComponent(bare); } });
      if (kind !== 'case') out.push({ group: 'Jump to record', kind: 'record', label: 'Open source batch ' + bare.slice(0, 12) + '…', meta: 'Exact batch identifier', always: true, run: function () { location.hash = '#batch/' + encodeURIComponent(bare); } });
    } else if (/^[A-Za-z0-9][\w.:-]{2,99}$/.test(bare) && !kind) {
      out.push({ group: 'Jump to record', kind: 'find', label: 'Find movement “' + bare + '”', meta: 'Review queue · movement IDs starting with ' + bare, always: true, run: function () { findMovement(bare); } });
    }
    TABS.forEach(function (t) {
      var b = $('#navigation [data-tab="' + t[0] + '"]');
      if (b && !b.hidden) {
        var icon = $('.nav-icon svg', b), count = $('.nav-count', b);
        out.push({ group: 'Pages', kind: 'page', icon: icon ? icon.outerHTML : '', label: t[1], meta: count && !count.hidden ? count.textContent + ' open' : '', keys: 'g ' + t[2], run: function () { clickTab(t[0]); } });
      }
    });
    var seen = Object.create(null), shown = 0;
    $$('#tab-content [data-case], #tab-content [data-invoice]').forEach(function (control) {
      if (control.tagName !== 'BUTTON' || control.disabled) return;
      var key = control.dataset.case ? 'case:' + control.dataset.case : 'invoice:' + control.dataset.invoice;
      if (seen[key] || shown >= 60) return;
      seen[key] = true; shown += 1;
      var facts = rowFacts(control);
      out.push({ group: 'On this page', kind: 'record', label: facts.title, meta: (control.dataset.case ? 'Case' : 'Invoice') + (facts.meta ? ' · ' + facts.meta : ''), secondary: shown > 6, run: function () { if (document.contains(control) && !control.disabled) control.click(); } });
    });
    var refresh = $('#refresh');
    if (visible(refresh)) out.push({ group: 'Actions', kind: 'action', label: 'Refresh current view', keys: 'r', run: function () { refresh.click(); } });
    var search = $('#case-search');
    if (visible(search)) out.push({ group: 'Actions', kind: 'action', label: 'Focus movement search', keys: '/', run: function () { search.focus(); } });
    [['prepare', 'Prepare monthly invoice'], ['upload-source', 'Upload source files'], ['import-worklist', 'Import reviewed worklist'], ['automation-settings', 'Automation settings']].forEach(function (a) {
      var button = $$('[data-action="' + a[0] + '"]').filter(visible)[0];
      if (button && !button.disabled) out.push({ group: 'Actions', kind: 'action', label: a[1], run: function () { if (!button.disabled) button.click(); } });
    });
    out.push({ group: 'Actions', kind: 'help', label: 'Show keyboard shortcuts', keys: '?', run: function () { openPalette('help'); }, keep: true });
    /* Preferences: two entries by default; the full list only when searched. */
    out.push({ group: 'Settings', kind: 'setting', label: 'Appearance…', meta: 'Theme, accent, surface, density, motion', run: function () { openAppearance(); } });
    out.push({ group: 'Settings', kind: 'setting', label: 'Cycle theme', meta: 'System → light → dark', keys: 'Shift T', run: toggleTheme });
    [['system', 'Use system theme'], ['light', 'Switch to light theme'], ['dark', 'Switch to dark theme']].forEach(function (t) {
      out.push({ group: 'Settings', kind: 'setting', secondary: true, label: t[1], run: function () { pref('theme', t[0]); announce(t[1].replace('Switch to ', '').replace('Use ', '') + ' on'); } });
    });
    ACCENTS.forEach(function (a) { out.push({ group: 'Settings', kind: 'setting', secondary: true, label: 'Accent: ' + a[1], run: function () { pref('accent', a[0]); announce('Accent ' + a[1]); } }); });
    out.push({ group: 'Settings', kind: 'setting', secondary: true, label: 'Surface: glass', run: function () { pref('surface', 'glass'); announce('Glass surfaces on'); } });
    out.push({ group: 'Settings', kind: 'setting', secondary: true, label: 'Surface: solid', run: function () { pref('surface', 'solid'); announce('Solid surfaces on'); } });
    out.push({ group: 'Settings', kind: 'setting', secondary: true, label: prefGet('density') === 'compact' ? 'Use comfortable density' : 'Use compact density', keys: 'Shift D', run: toggleDensity });
    out.push({ group: 'Settings', kind: 'setting', secondary: true, label: 'Motion: full', run: function () { pref('motion', 'full'); announce('Full motion'); } });
    out.push({ group: 'Settings', kind: 'setting', secondary: true, label: 'Motion: reduced', run: function () { pref('motion', 'reduced'); announce('Reduced motion'); } });
    out.push({ group: 'Settings', kind: 'setting', secondary: true, label: 'Toggle sidebar', keys: '[', run: toggleSidebar });
    out.push({ group: 'Settings', kind: 'setting', secondary: true, label: 'Reset appearance to defaults', run: function () { if (window.IOTheme) window.IOTheme.reset(); syncAppearance(); announce('Appearance reset to defaults'); } });
    var inScope = function (c) { return scope === 'all' || c.kind === scope || (scope === 'record' && c.kind === 'find') || (scope === 'action' && c.kind === 'help'); };
    if (!q) return out.filter(function (c) { return inScope(c) && (!c.secondary || scope !== 'all'); });
    var words = q.toLowerCase().split(/\s+/);
    var matches = out.filter(function (c) {
      if (!inScope(c)) return false;
      if (c.always) return true;
      var hay = (c.group + ' ' + c.label + ' ' + (c.meta || '') + ' ' + (c.keys || '')).toLowerCase();
      return words.every(function (w) { return hay.indexOf(w) >= 0; });
    });
    /* Words without digits ("insights") are page or action names first: the
       movement-ID search then moves last, so Enter opens the obvious match. */
    if (!/\d/.test(bare) && matches.length > 1 && matches[0].kind === 'find') matches.push(matches.shift());
    return matches;
  }

  /* Highlight query words in a label (escaped first, then marked). */
  function highlight(text, query) {
    var safe = esc(text), words = String(query || '').trim().toLowerCase().split(/\s+/).filter(function (w) { return w.length > 0; });
    if (!words.length) return safe;
    var pattern = new RegExp('(' + words.map(function (w) { return esc(w).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }).join('|') + ')', 'gi');
    return safe.replace(/(<[^>]*>|&[#\w]+;)|([^<&]+)/g, function (m, tag, txt) { return tag || txt.replace(pattern, '<mark>$1</mark>'); });
  }

  function setScope(next) {
    scope = SCOPES.some(function (s) { return s[0] === next; }) ? next : 'all';
    $$('[data-palette-scope]', palette).forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.paletteScope === scope)); });
    render();
  }

  function render() {
    items = commands(input.value);
    var html = '', group = null, gi = 0, q = input.value;
    items.forEach(function (c, i) {
      if (c.group !== group) {
        if (group !== null) html += '</div>';
        group = c.group; gi += 1;
        html += '<div role="group" aria-labelledby="palette-group-' + gi + '"><div class="palette-section" id="palette-group-' + gi + '" role="presentation">' + esc(group) + '</div>';
      }
      var icon = c.icon || ICONS[c.kind] || ICONS.action;
      html += '<div role="option" id="palette-option-' + i + '" data-index="' + i + '" data-kind="' + esc(c.kind || 'action') + '" aria-selected="false">' +
        '<span class="palette-icon" aria-hidden="true">' + icon + '</span>' +
        '<span class="palette-text"><span class="palette-label">' + highlight(c.label, q) + '</span>' + (c.meta ? '<span class="palette-meta">' + highlight(c.meta, q) + '</span>' : '') + '</span>' +
        (c.keys ? '<span class="palette-keys" aria-hidden="true">' + c.keys.split(' ').map(function (k) { return '<kbd>' + esc(k) + '</kbd>'; }).join('') + '</span>' : '<span class="palette-enter" aria-hidden="true">↵</span>') + '</div>';
    });
    if (group !== null) html += '</div>';
    list.innerHTML = html;
    $('.palette-empty', palette).hidden = items.length > 0;
    setActive(0, true);
  }

  function setActive(index, scroll) {
    var options = $$('[role="option"]', list);
    if (!options.length) { activeIndex = -1; input.removeAttribute('aria-activedescendant'); return; }
    activeIndex = Math.max(0, Math.min(options.length - 1, index));
    options.forEach(function (o, i) { o.setAttribute('aria-selected', String(i === activeIndex)); });
    input.setAttribute('aria-activedescendant', options[activeIndex].id);
    if (scroll !== false) options[activeIndex].scrollIntoView({ block: 'nearest' });
  }

  function onPaletteKey(event) {
    var n = items.length;
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive(n ? (activeIndex + 1) % n : 0); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setActive(n ? (activeIndex - 1 + n) % n : 0); }
    else if (event.key === 'Home' && !input.value) { event.preventDefault(); setActive(0); }
    else if (event.key === 'End' && !input.value) { event.preventDefault(); setActive(n - 1); }
    else if (event.key === 'Enter') { event.preventDefault(); if (activeIndex >= 0) run(activeIndex); }
    else if (event.key === 'Tab') {
      event.preventDefault();
      if (mode === 'help') return;
      var at = SCOPES.map(function (s) { return s[0]; }).indexOf(scope);
      setScope(SCOPES[(at + (event.shiftKey ? SCOPES.length - 1 : 1)) % SCOPES.length][0]);
      announce(SCOPES.filter(function (s) { return s[0] === scope; })[0][1] + ' · ' + items.length + ' results');
    }
  }

  function run(index) {
    var command = items[index];
    if (!command) return;
    if (command.keep) { command.run(); return; }
    palette.close();
    command.run();
  }

  function openPalette(nextMode) {
    if (!palette || !workspaceOpen() || modalOpen()) return;
    build();
    mode = nextMode === 'help' ? 'help' : 'all';
    scope = 'all';
    $$('[data-palette-scope]', palette).forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.paletteScope === 'all')); });
    palette.classList.toggle('is-help', mode === 'help');
    if (!palette.open) {
      returnFocus = document.activeElement;
      palette.showModal();
    }
    input.value = '';
    input.setAttribute('aria-label', mode === 'help' ? 'Keyboard shortcuts. Type to search commands' : 'Search commands, pages and records');
    render();
    input.focus();
  }

  /* ---------------------------------------------------------------- */
  /* Keyboard map (C.13)                                                */
  /* ---------------------------------------------------------------- */
  function rowControls() {
    var seen = [];
    return $$('#tab-content [data-case], #tab-content [data-invoice]').filter(function (control) {
      if (control.tagName !== 'BUTTON' || !visible(control)) return false;
      var row = control.closest('tr, .review-card, .invoice-card, .review-item') || control;
      if (seen.indexOf(row) >= 0) return false;
      seen.push(row);
      return true;
    });
  }
  function moveRow(delta) {
    var controls = rowControls();
    if (!controls.length) return;
    var current = document.activeElement && document.activeElement.closest ? document.activeElement.closest('tr, .review-card, .invoice-card, .review-item') : null;
    var index = -1;
    controls.forEach(function (c, i) { if (current && current.contains(c)) index = i; });
    var next = controls[index < 0 ? (delta > 0 ? 0 : controls.length - 1) : Math.max(0, Math.min(controls.length - 1, index + delta))];
    $$('.is-kbd-row').forEach(function (r) { r.classList.remove('is-kbd-row'); });
    var row = next.closest('tr, .review-card, .invoice-card, .review-item');
    if (row) row.classList.add('is-kbd-row');
    next.focus();
    next.scrollIntoView({ block: 'nearest' });
  }
  function toggleRowSelection() {
    var row = document.activeElement && document.activeElement.closest ? document.activeElement.closest('tr, .review-card') : null;
    var box = row && row.querySelector('[data-select-case]');
    if (box && !box.disabled) { box.click(); announce(box.checked ? 'Selected for release' : 'Removed from release selection'); }
  }
  function toggleDensity() { var b = $('#density-toggle'); if (b) b.click(); announce(prefGet('density') === 'compact' ? 'Compact density' : 'Comfortable density'); }
  function toggleTheme() { var b = $('#theme-toggle'); if (b) { b.click(); announce(b.getAttribute('aria-label').split('.')[0]); } }
  function toggleSidebar() { var b = $('#sidebar-toggle'); if (visible(b)) { b.click(); announce(b.getAttribute('aria-label') === 'Collapse sidebar' ? 'Sidebar expanded' : 'Sidebar collapsed'); } }
  function focusSearch() {
    var search = $('#case-search');
    if (visible(search)) { search.focus(); search.select && search.select(); } else openPalette();
  }
  function shortcutList() {
    var mod = isMac ? '⌘' : 'Ctrl';
    return [
      [mod + ' K', 'Open the command palette', function () { openPalette(); }],
      ['/', 'Focus case search', function () { palette.close(); focusSearch(); }],
      ['?', 'Show keyboard shortcuts', function () { openPalette('help'); }],
      ['g o', 'Go to Overview', function () { palette.close(); clickTab('overview'); }],
      ['g r', 'Go to Review queue', function () { palette.close(); clickTab('review'); }],
      ['g i', 'Go to Invoices', function () { palette.close(); clickTab('invoices'); }],
      ['g n', 'Go to Insights', function () { palette.close(); clickTab('insights'); }],
      ['g d', 'Go to Delivery', function () { palette.close(); clickTab('delivery'); }],
      ['g m', 'Go to Data & model', function () { palette.close(); clickTab('data'); }],
      ['g e', 'Go to Email setup', function () { palette.close(); clickTab('email'); }],
      ['g t', 'Go to Team access', function () { palette.close(); clickTab('team'); }],
      ['j', 'Focus the next record row', function () { palette.close(); moveRow(1); }],
      ['k', 'Focus the previous record row', function () { palette.close(); moveRow(-1); }],
      ['↵', 'Open the focused record', function () { palette.close(); }],
      ['x', 'Select the focused case for release', function () { palette.close(); toggleRowSelection(); }],
      ['r', 'Refresh the current view', function () { palette.close(); var r = $('#refresh'); if (r) r.click(); }],
      ['Shift T', 'Cycle theme (system, light, dark)', function () { toggleTheme(); }],
      ['Shift D', 'Toggle density', function () { toggleDensity(); }],
      ['[', 'Collapse or expand the sidebar', function () { toggleSidebar(); }]
    ];
  }

  var pendingG = 0;
  document.addEventListener('keydown', function (event) {
    if (event.defaultPrevented || event.isComposing || !workspaceOpen()) return;
    var key = event.key;
    if ((event.metaKey || event.ctrlKey) && !event.altKey && (key === 'k' || key === 'K')) {
      if (modalOpen()) return;
      event.preventDefault();
      if (palette && palette.open) palette.close(); else openPalette();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if ((palette && palette.open) || modalOpen() || typing(event.target)) return;
    if (event.target.closest && event.target.closest('.card-deck, .version-swipe-area, [data-chart], [popover]')) return;
    if (pendingG && Date.now() - pendingG < 1200) {
      pendingG = 0;
      var tab = TABS.filter(function (t) { return t[2] === key.toLowerCase(); })[0];
      if (tab && !event.shiftKey) { event.preventDefault(); clickTab(tab[0]); }
      return;
    }
    pendingG = 0;
    if (key === 'g' && !event.shiftKey) { pendingG = Date.now(); return; }
    var handled = true;
    if (key === '/') focusSearch();
    else if (key === '?') openPalette('help');
    else if (key === 'j') moveRow(1);
    else if (key === 'k') moveRow(-1);
    else if (key === 'x') toggleRowSelection();
    else if (key === 'r') { var r = $('#refresh'); if (visible(r)) r.click(); }
    else if (key === 'T' && event.shiftKey) toggleTheme();
    else if (key === 'D' && event.shiftKey) toggleDensity();
    else if (key === '[') toggleSidebar();
    else handled = false;
    if (handled) event.preventDefault();
  });

  /* Topbar trigger */
  document.addEventListener('click', function (event) {
    if (event.target.closest && event.target.closest('#command-trigger')) openPalette();
  });
  if (!isMac) $$('#command-trigger kbd, .sidebar-note kbd').forEach(function (k) { if (k.textContent === '⌘K') k.textContent = 'Ctrl K'; });

  /* ---------------------------------------------------------------- */
  /* Appearance panel (addendum §5)                                     */
  /* ---------------------------------------------------------------- */
  var panel = $('#appearance-panel'), button = $('#appearance-button');
  var popoverSupported = !!(panel && typeof panel.showPopover === 'function');

  function radios(name, legend, options, current) {
    return '<fieldset class="appearance-group"><legend>' + esc(legend) + '</legend><div class="appearance-options' + (name === 'accent' ? ' appearance-swatches' : '') + '">' +
      options.map(function (o) {
        var id = 'appearance-' + name + '-' + o[0];
        return '<label class="appearance-option" for="' + id + '"' + (name === 'accent' ? ' style="--swatch-h:' + o[2] + '"' : '') + '><input type="radio" id="' + id + '" name="appearance-' + name + '" value="' + o[0] + '"' + (current === o[0] ? ' checked' : '') + '>' +
          (name === 'accent' ? '<span class="swatch" aria-hidden="true"></span><span class="sr-only">' + esc(o[1]) + '</span>' : '<span>' + esc(o[1]) + '</span>') + '</label>';
      }).join('') + '</div></fieldset>';
  }
  function themeChoice() { try { return localStorage.getItem('io.theme') || 'system'; } catch (e) { return 'system'; } }
  function renderAppearance() {
    if (!panel) return;
    var sidebar = prefGet('sidebar');
    if (sidebar === 'auto') sidebar = document.documentElement.getAttribute('data-nav') === 'rail' ? 'collapsed' : 'expanded';
    panel.innerHTML = '<div class="appearance-header"><h2 id="appearance-title">Appearance</h2><button type="button" class="icon-button appearance-close" aria-label="Close appearance settings">×</button></div>' +
      '<p class="small muted">Display preferences are stored in this browser only.</p>' +
      radios('theme', 'Theme', [['system', 'System'], ['light', 'Light'], ['dark', 'Dark']], themeChoice()) +
      radios('accent', 'Accent', ACCENTS, prefGet('accent')) +
      radios('surface', 'Surface', [['glass', 'Glass'], ['solid', 'Solid']], prefGet('surface')) +
      radios('density', 'Density', [['comfortable', 'Comfortable'], ['compact', 'Compact']], prefGet('density')) +
      radios('motion', 'Motion', [['full', 'Full'], ['reduced', 'Reduced']], prefGet('motion')) +
      radios('sidebar', 'Sidebar', [['expanded', 'Expanded'], ['collapsed', 'Collapsed']], sidebar) +
      '<div class="appearance-footer"><button type="button" class="quiet small" data-appearance-reset>Reset to defaults</button></div>';
  }
  function syncAppearance() {
    if (!panel || !panel.firstChild) return;
    var values = { theme: themeChoice(), accent: prefGet('accent'), surface: prefGet('surface'), density: prefGet('density'), motion: prefGet('motion') };
    Object.keys(values).forEach(function (k) {
      $$('input[name="appearance-' + k + '"]', panel).forEach(function (r) { r.checked = r.value === values[k]; });
    });
  }
  function place() {
    if (!panel || !button) return;
    var rect = button.getBoundingClientRect();
    panel.style.position = 'fixed';
    panel.style.margin = '0';
    panel.style.inset = 'auto';
    panel.style.top = Math.round(rect.bottom + 8) + 'px';
    /* Keep the whole panel on screen when the trigger sits mid-row (mobile). */
    var width = panel.offsetWidth || Math.min(340, window.innerWidth - 16);
    var right = Math.min(window.innerWidth - rect.right, window.innerWidth - width - 8);
    panel.style.right = Math.max(8, Math.round(right)) + 'px';
  }
  function focusPanel() {
    var first = $('input:checked', panel) || $('input', panel);
    if (first) first.focus();
  }
  function openAppearance() {
    if (!panel) return;
    if (popoverSupported) { if (!panel.matches(':popover-open')) panel.showPopover(); return; }
    renderAppearance();
    place();
    panel.hidden = false;
    if (button) button.setAttribute('aria-expanded', 'true');
    focusPanel();
  }
  function closeAppearance(restore) {
    if (!panel) return;
    if (popoverSupported) { if (panel.matches(':popover-open')) panel.hidePopover(); } else panel.hidden = true;
    if (button) { button.setAttribute('aria-expanded', 'false'); if (restore) button.focus(); }
  }
  if (panel && button) {
    button.hidden = false;
    button.setAttribute('aria-controls', 'appearance-panel');
    button.setAttribute('aria-expanded', 'false');
    if (!popoverSupported) panel.hidden = true;
    if (popoverSupported) {
      /* Declarative invoker: light-dismiss and Esc come from the platform. */
      button.setAttribute('popovertarget', 'appearance-panel');
      panel.addEventListener('beforetoggle', function (event) { if (event.newState === 'open') { renderAppearance(); place(); } });
      panel.addEventListener('toggle', function (event) {
        var open = event.newState === 'open';
        button.setAttribute('aria-expanded', String(open));
        if (open) focusPanel();
      });
    } else {
      button.addEventListener('click', function () { if (!panel.hidden) closeAppearance(false); else openAppearance(); });
    }
    panel.addEventListener('change', function (event) {
      var r = event.target;
      if (!r.name || r.name.indexOf('appearance-') !== 0) return;
      var key = r.name.slice(11);
      pref(key, key === 'theme' && r.value === 'system' ? null : r.value);
      announce(r.closest('fieldset').querySelector('legend').textContent + ': ' + (r.closest('label').textContent || r.value));
    });
    panel.addEventListener('click', function (event) {
      if (event.target.closest('.appearance-close')) closeAppearance(true);
      if (event.target.closest('[data-appearance-reset]')) { if (window.IOTheme) window.IOTheme.reset(); renderAppearance(); announce('Appearance reset to defaults'); var f = $('input:checked', panel); if (f) f.focus(); }
    });
    panel.addEventListener('keydown', function (event) { if (event.key === 'Escape' && !popoverSupported) { event.preventDefault(); closeAppearance(true); } });
    document.addEventListener('click', function (event) {
      if (popoverSupported || panel.hidden) return;
      if (!panel.contains(event.target) && !button.contains(event.target)) closeAppearance(false);
    });
    window.addEventListener('resize', function () { if (popoverSupported ? panel.matches(':popover-open') : !panel.hidden) place(); });
  }
  document.addEventListener('io:preferences', syncAppearance);

  window.IOShortcuts = Object.freeze({ open: openPalette, appearance: openAppearance, announce: announce });
})();
