/* Motion and live-UI helpers that only observe the DOM app.js renders:
   view entrance, KPI count-up, segmented-control indicator, and relative
   times that tick every 30s while the page is visible. No fetch, no app
   state. Everything is skipped under reduced motion (data-motion or OS). */
(function () {
  'use strict';
  var root = document.documentElement;
  var $$ = function (selector, scope) { return Array.prototype.slice.call((scope || document).querySelectorAll(selector)); };
  function reduced() {
    var m = root.getAttribute('data-motion');
    if (m) return m === 'reduced';
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  /* KPI count-up: animate .metric-value[data-value] when the value changes
     between renders (never on first paint, so exact text is stable). */
  var previous = Object.create(null);
  function countUp(scope) {
    $$('.metric-value[data-value]', scope).forEach(function (el) {
      var key = el.getAttribute('data-key') || '', to = Number(el.getAttribute('data-value')), final = el.textContent;
      var from = previous[key];
      previous[key] = to;
      if (!Number.isFinite(to) || !Number.isFinite(from) || from === to || reduced()) return;
      el.classList.add('count-up', 'is-counting');
      var start = performance.now(), duration = Math.min(600, 200 + Math.abs(to - from) * 20);
      function frame(now) {
        if (!document.contains(el)) return;
        var t = Math.min(1, (now - start) / duration), eased = 1 - Math.pow(1 - t, 3);
        if (t < 1) { el.textContent = Math.round(from + (to - from) * eased).toLocaleString('en-GB'); requestAnimationFrame(frame); }
        else { el.textContent = final; el.classList.remove('is-counting'); }
      }
      requestAnimationFrame(frame);
    });
  }

  /* Segmented layout switchers: sliding active indicator. */
  function indicators(scope) {
    $$('.view-switch', scope).forEach(function (group) {
      var active = group.querySelector('button[aria-pressed="true"]');
      if (!active) { group.removeAttribute('data-indicator'); return; }
      var g = group.getBoundingClientRect(), a = active.getBoundingClientRect();
      if (!a.width) return;
      group.style.setProperty('--seg-x', (a.left - g.left - group.clientLeft) + 'px');
      group.style.setProperty('--seg-w', a.width + 'px');
      if (!group.hasAttribute('data-indicator')) requestAnimationFrame(function () { group.setAttribute('data-indicator', ''); });
    });
  }

  /* Relative times: <time data-relative datetime="ISO">. */
  function relative(iso) {
    var t = Date.parse(iso);
    if (!Number.isFinite(t)) return '';
    var s = Math.round((Date.now() - t) / 1000);
    if (s < 45) return 'just now';
    var m = Math.round(s / 60);
    if (m < 60) return m + ' min ago';
    var h = Math.round(m / 60);
    if (h < 24) return h + ' h ago';
    var d = Math.round(h / 24);
    return d === 1 ? 'yesterday' : d + ' days ago';
  }
  function tick(scope) {
    $$('time[data-relative]', scope).forEach(function (el) {
      var text = relative(el.getAttribute('datetime'));
      if (text && el.textContent !== text) el.textContent = text;
    });
  }
  var timer = null;
  function schedule() {
    clearInterval(timer);
    timer = null;
    if (!document.hidden) timer = setInterval(function () { tick(document); }, 30000);
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) tick(document); schedule(); });
  schedule();

  /* View entrance and post-render hooks. */
  var lastTab = null, wasSkeleton = false;
  function activeTab() { var a = document.querySelector('#navigation [aria-current="page"]'); return a ? a.getAttribute('data-tab') : null; }
  function onRender(content) {
    var tab = activeTab(), skeleton = !!content.querySelector(':scope > [data-skeleton]');
    if (!reduced() && (tab !== lastTab || (wasSkeleton && !skeleton))) {
      content.classList.remove('is-entering');
      void content.offsetWidth;
      content.classList.add('is-entering');
    }
    lastTab = tab;
    wasSkeleton = skeleton;
    countUp(content);
    indicators(content);
    tick(content);
  }
  function init() {
    var content = document.getElementById('tab-content');
    if (!content) return;
    var pending = false;
    content.addEventListener('animationend', function (event) { if (event.target === content) content.classList.remove('is-entering'); });
    new MutationObserver(function (records) {
      if (records.some(function (r) { return r.target === content; })) onRender(content);
      else if (!pending) { pending = true; requestAnimationFrame(function () { pending = false; indicators(content); }); }
    }).observe(content, { childList: true, subtree: true, attributes: true, attributeFilter: ['aria-pressed'] });
    window.addEventListener('resize', function () { indicators(document); });
    document.addEventListener('io:preferences', function () { requestAnimationFrame(function () { indicators(document); }); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();

  window.IOMotion = Object.freeze({ reduced: reduced, relative: relative });
})();
