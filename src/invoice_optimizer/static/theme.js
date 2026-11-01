/* Display preferences only (theme, accent, surface, density, motion, sidebar).
   Loaded synchronously first in <head> so the first paint uses the right
   theme. Never stores tokens, identities or workspace data. */
(function () {
  'use strict';
  var root = document.documentElement;
  var KEYS = {
    theme: ['light', 'dark', 'system'],
    accent: ['indigo', 'blue', 'teal', 'emerald', 'amber', 'rose'],
    surface: ['glass', 'solid'],
    density: ['comfortable', 'compact'],
    motion: ['full', 'reduced', 'system'],
    sidebar: ['expanded', 'collapsed']
  };
  var media = function (q) { return !!(window.matchMedia && window.matchMedia(q).matches); };
  var read = function (key) {
    try { var v = localStorage.getItem('io.' + key); return KEYS[key].indexOf(v) >= 0 ? v : null; } catch (e) { return null; }
  };
  var glassSupported = function () {
    return !!(window.CSS && CSS.supports && (CSS.supports('backdrop-filter', 'blur(1px)') || CSS.supports('-webkit-backdrop-filter', 'blur(1px)')));
  };
  function resolved(key) {
    var v = read(key);
    if (key === 'surface' && !v) return glassSupported() && !media('(prefers-reduced-transparency: reduce)') ? 'glass' : 'solid';
    if (key === 'density' && !v) return 'comfortable';
    if (key === 'accent' && !v) return 'indigo';
    if (key === 'theme' && (!v || v === 'system')) return media('(prefers-color-scheme: dark)') ? 'dark' : 'light';
    if (key === 'motion' && (!v || v === 'system')) return media('(prefers-reduced-motion: reduce)') ? 'reduced' : 'full';
    return v;
  }
  function apply() {
    var theme = read('theme'), motion = read('motion'), sidebar = read('sidebar');
    /* theme/motion: the attribute is absent when following the OS, so the
       CSS media-query fallbacks stay live. */
    if (theme && theme !== 'system') root.setAttribute('data-theme', theme); else root.removeAttribute('data-theme');
    if (motion && motion !== 'system') root.setAttribute('data-motion', motion); else root.removeAttribute('data-motion');
    if (sidebar) root.setAttribute('data-sidebar', sidebar); else root.removeAttribute('data-sidebar');
    applyNav();
    root.setAttribute('data-accent', resolved('accent'));
    root.setAttribute('data-surface', resolved('surface'));
    root.setAttribute('data-density', resolved('density'));
  }
  function set(key, value) {
    if (!KEYS[key]) return false;
    if ((key === 'theme' || key === 'accent' || key === 'surface') && document.startViewTransition && resolved('motion') === 'full' && !set.busy) {
      set.busy = true;
      var ok = true;
      try { document.startViewTransition(function () { ok = set(key, value); }).finished.then(done, done); } catch (e) { done(); return set(key, value); }
      return ok;
    }
    try {
      if (value == null || value === '') localStorage.removeItem('io.' + key);
      else if (KEYS[key].indexOf(value) >= 0) localStorage.setItem('io.' + key, value);
      else return false;
    } catch (e) { /* storage unavailable: apply for this page only */ }
    apply();
    sync();
    try { document.dispatchEvent(new CustomEvent('io:preferences', { detail: { key: key, value: get(key) } })); } catch (e) { /* old engines */ }
    return true;
  }
  function done() { set.busy = false; }
  function get(key) { return key === 'sidebar' ? (read('sidebar') || 'auto') : resolved(key); }
  function reset() { Object.keys(KEYS).forEach(function (key) { try { localStorage.removeItem('io.' + key); } catch (e) { /* ignore */ } }); apply(); sync(); }

  /* Navigation layout, resolved from the viewport and the stored choice:
     expanded | rail | overlay (temporary, below 1280px) | mobile. */
  var overlay = false;
  function navState() {
    if (media('(max-width: 639.98px)')) return 'mobile';
    var v = read('sidebar');
    if (media('(min-width: 1280px)')) return v === 'collapsed' ? 'rail' : 'expanded';
    return overlay ? 'overlay' : 'rail';
  }
  function applyNav() { root.setAttribute('data-nav', navState()); }
  function toggleSidebar() {
    var state = navState();
    if (state === 'mobile') return;
    if (media('(min-width: 1280px)')) { set('sidebar', state === 'expanded' ? 'collapsed' : 'expanded'); return; }
    overlay = state !== 'overlay';
    applyNav();
    sync();
  }
  function nextTheme(choice) { return { system: 'light', light: 'dark', dark: 'system' }[choice] || 'light'; }
  function closeOverlay() { if (overlay) { overlay = false; applyNav(); sync(); } }
  function sync() {
    var b = document.getElementById('theme-toggle');
    if (b) {
      /* Cycle: system -> light -> dark -> system. aria-pressed is "mixed"
         while following the OS, "true" for an explicit dark choice. */
      var choice = read('theme') || 'system', next = nextTheme(choice);
      var names = { system: 'system theme', light: 'light theme', dark: 'dark theme' };
      b.setAttribute('aria-pressed', choice === 'system' ? 'mixed' : String(choice === 'dark'));
      b.setAttribute('aria-label', 'Theme: ' + (choice === 'system' ? 'system (' + resolved('theme') + ')' : choice) + '. Switch to ' + names[next]);
      b.setAttribute('data-theme-choice', choice);
      b.title = b.getAttribute('aria-label');
    }
    var d = document.getElementById('density-toggle');
    if (d) {
      var compact = resolved('density') === 'compact';
      d.setAttribute('aria-pressed', String(compact));
      d.setAttribute('aria-label', compact ? 'Use comfortable density' : 'Use compact density');
      d.title = d.getAttribute('aria-label');
    }
    var s = document.getElementById('sidebar-toggle');
    if (s) {
      var nav = navState(), expanded = nav !== 'rail';
      s.setAttribute('aria-expanded', String(expanded));
      s.setAttribute('aria-label', expanded ? 'Collapse sidebar' : 'Expand sidebar');
      s.title = s.getAttribute('aria-label');
    }
  }

  apply();

  document.addEventListener('click', function (event) {
    var target = event.target && event.target.closest ? event.target.closest('#theme-toggle, #density-toggle, #sidebar-toggle, #navigation [data-tab]') : null;
    if (!target) { if (overlay && !(event.target.closest && event.target.closest('#sidebar'))) closeOverlay(); return; }
    if (target.id === 'theme-toggle') set('theme', nextTheme(read('theme') || 'system'));
    else if (target.id === 'density-toggle') set('density', resolved('density') === 'compact' ? 'comfortable' : 'compact');
    else if (target.id === 'sidebar-toggle') toggleSidebar();
    else closeOverlay();
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') closeOverlay();
  });
  document.addEventListener('DOMContentLoaded', sync);
  if (window.matchMedia) {
    ['(prefers-color-scheme: dark)', '(max-width: 639.98px)', '(min-width: 1280px)'].forEach(function (q) {
      var m = window.matchMedia(q);
      var fn = function () { overlay = false; applyNav(); sync(); };
      if (m.addEventListener) m.addEventListener('change', fn); else if (m.addListener) m.addListener(fn);
    });
  }

  window.IOTheme = Object.freeze({ set: set, get: get, reset: reset, toggleSidebar: toggleSidebar, keys: KEYS });
})();
