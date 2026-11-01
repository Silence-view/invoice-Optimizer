/* MiniCharts: dependency-free inline-SVG/HTML chart strings for the console.
   Every builder returns an escaped string, so callers can drop it into the
   existing innerHTML render path. Colours come from tokens (var(--...)) or
   currentColor, so charts follow theme and accent automatically. Each chart
   carries a text summary (role=img + <title>/<desc> or aria-label) and a
   visually hidden data table. Pointer hover and keyboard focus show one
   shared role=tooltip element; values are always reachable without it. */
(function () {
  'use strict';
  var uid = 0;
  var drawn = Object.create(null);
  var esc = function (value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  var num = function (value) { var n = Number(value); return Number.isFinite(n) ? n : 0; };
  var fmt = function (value) { return Number.isFinite(Number(value)) && value !== null ? Number(value).toLocaleString('en-GB') : '—'; };
  var compact = function (value) {
    var n = num(value), a = Math.abs(n);
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
    if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'k';
    return fmt(n);
  };
  var pct = function (part, total) { return total > 0 ? Math.round(part / total * 1000) / 10 : 0; };
  var TONES = { success: 'var(--success)', warning: 'var(--warning)', danger: 'var(--danger)', info: 'var(--info)', neutral: 'var(--chart-neutral)', accent: 'var(--accent)' };
  /* Fixed categorical order, never cycled past 6: extra parts fold into Other. */
  var colour = function (item, index) {
    if (item && item.tone && TONES[item.tone]) return TONES[item.tone];
    return index < 6 ? 'var(--series-' + (index + 1) + ')' : 'var(--chart-neutral)';
  };
  /* Draw-in only the first time a chart key is painted; SSE re-renders of
     the same chart then update without replaying the entrance. */
  var animate = function (key, cls) {
    if (!key) return '';
    if (drawn[key]) return '';
    drawn[key] = true;
    return ' ' + cls;
  };
  var nextId = function (prefix) { uid += 1; return 'mc-' + prefix + '-' + uid; };
  var table = function (caption, rows) {
    return '<table class="sr-only"><caption>' + esc(caption) + '</caption><thead><tr><th scope="col">Item</th><th scope="col">Value</th></tr></thead><tbody>' +
      rows.map(function (r) { return '<tr><th scope="row">' + esc(r[0]) + '</th><td>' + esc(r[1]) + '</td></tr>'; }).join('') + '</tbody></table>';
  };
  var fold = function (items, max) {
    var list = (items || []).filter(function (x) { return x && num(x.value) >= 0; });
    if (list.length <= max) return list;
    var rest = list.slice(max - 1).reduce(function (s, x) { return s + num(x.value); }, 0);
    return list.slice(0, max - 1).concat([{ label: 'Other', value: rest, tone: 'neutral' }]);
  };
  var figure = function (kind, label, summary, body, rows, interactive) {
    var t = nextId('t'), d = nextId('d');
    return '<figure class="chart chart-' + kind + '"' + (interactive ? ' data-chart tabindex="0" aria-label="' + esc(label + '. ' + summary + ' Use arrow keys to inspect values.') + '"' : '') + '>' +
      body.replace('%TITLE%', '<title id="' + t + '">' + esc(label) + '</title><desc id="' + d + '">' + esc(summary) + '</desc>').replace('%LABELLEDBY%', t + ' ' + d) +
      '<figcaption class="sr-only">' + table(label, rows) + '</figcaption></figure>';
  };

  /* Transparent full-height hit columns (scale with preserveAspectRatio=none)
     so every sparkline point has a hover/keyboard target bigger than the mark. */
  function hits(pts, w, h, data, opts) {
    return pts.map(function (p, i) {
      var left = i ? (pts[i - 1][0] + p[0]) / 2 : 0, right = i < pts.length - 1 ? (p[0] + pts[i + 1][0]) / 2 : w;
      var tipText = opts.tips && opts.tips[i] ? opts.tips[i] : (opts.labels && opts.labels[i] ? opts.labels[i] + ' · ' : '') + fmt(data[i]);
      return '<rect class="chart-hit" x="' + left.toFixed(2) + '" y="0" width="' + Math.max(0.5, right - left).toFixed(2) + '" height="' + h + '" data-chart-tip="' + esc(tipText) + '"/>';
    }).join('');
  }

  /* Sparkline: 120x32 line + 12% area + terminal dot. */
  function sparkline(values, opts) {
    opts = opts || {};
    var data = (values || []).map(num), label = opts.label || 'Trend';
    if (data.length < 2) return '';
    var w = opts.width || 120, h = opts.height || 32, pad = 2;
    var min = Math.min.apply(null, data), max = Math.max.apply(null, data), span = max - min || 1;
    var pts = data.map(function (v, i) { return [i / (data.length - 1) * w, h - pad - (v - min) / span * (h - pad * 2)]; });
    var line = pts.map(function (p, i) { return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join('');
    var last = pts[pts.length - 1];
    var summary = label + ': from ' + fmt(data[0]) + ' to ' + fmt(data[data.length - 1]) + '.';
    var key = opts.key || 'spark:' + label;
    var svg = '<svg class="sparkline" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" role="img" aria-labelledby="%LABELLEDBY%" focusable="false">%TITLE%' +
      '<path d="' + line + 'L' + w + ' ' + h + 'L0 ' + h + 'Z" fill="currentColor" fill-opacity=".12" stroke="none"/>' +
      '<path class="' + animate(key, 'chart-draw').trim() + '" pathLength="1" d="' + line + '" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>' +
      '<path class="sparkline-dot" d="M' + last[0].toFixed(1) + ' ' + last[1].toFixed(1) + 'h0" stroke="currentColor" stroke-width="5" stroke-linecap="round" vector-effect="non-scaling-stroke"/>' + (opts.interactive ? hits(pts, w, h, data, opts) : '') + '</svg>';
    var names = opts.labels || [];
    return figure('sparkline', label, opts.summary || summary, svg, data.map(function (v, i) { return [names[i] || 'Point ' + (i + 1), opts.tips && opts.tips[i] ? opts.tips[i] : fmt(v)]; }), !!opts.interactive);
  }

  /* Horizontal bar list (HTML): label · value · share, bars relative to max. */
  function bar(items, opts) {
    opts = opts || {};
    var list = fold(items, opts.max || 8), label = opts.label || 'Bar chart';
    var total = list.reduce(function (s, x) { return s + num(x.value); }, 0);
    var max = Math.max.apply(null, list.map(function (x) { return num(x.value); }).concat([0])) || 1;
    var key = opts.key || 'bar:' + label, grow = animate(key, 'chart-grow-x');
    var format = opts.format || fmt;
    var rowsHtml = list.map(function (x, i) {
      var v = num(x.value), share = pct(v, total);
      var shown = x.display != null ? x.display : format(v);
      var text = x.tip || (x.label + ' · ' + shown + (opts.share === false ? '' : ' · ' + share + '%'));
      return '<li class="chart-bar-row" data-chart-tip="' + esc(text) + '"><span class="chart-bar-label">' + esc(x.label) + '</span><span class="chart-bar-value">' + esc(shown) + (opts.share === false ? '' : ' <span class="muted">· ' + share + '%</span>') + '</span>' +
        '<span class="chart-bar-track" aria-hidden="true"><span class="chart-bar-fill' + grow + '" style="width:' + (v / max * 100).toFixed(2) + '%;background:' + (TONES[x.tone] || TONES[opts.tone] || 'var(--accent)') + ';--ci:' + Math.min(i, 12) + '"></span></span></li>';
    }).join('');
    var summary = opts.summary || (list.length ? list.map(function (x) { return x.label + ' ' + (x.display != null ? x.display : format(num(x.value))); }).join(', ') + '.' : 'No data.');
    var focus = list.length ? ' data-chart tabindex="0" aria-label="' + esc(label + '. ' + summary + ' Use arrow keys to inspect values.') + '"' : '';
    return '<figure class="chart chart-bar"' + focus + '><figcaption class="chart-caption" aria-hidden="' + (list.length ? 'true' : 'false') + '">' + esc(label) + '</figcaption><ul class="chart-bar-list" aria-label="' + esc(label + ': ' + summary) + '">' + rowsHtml + '</ul></figure>';
  }

  /* 100% stacked horizontal bar with 2px surface gaps and a legend.
     opts.compact renders only the 4px proportion bar (for KPI tiles). */
  function stackedBar(parts, opts) {
    opts = opts || {};
    var list = fold(parts, 6), label = opts.label || 'Composition';
    var total = list.reduce(function (s, x) { return s + num(x.value); }, 0);
    var summary = total > 0 ? list.map(function (x) { return x.label + ' ' + fmt(num(x.value)) + ' (' + pct(num(x.value), total) + '%)'; }).join(', ') + '.' : 'No records yet.';
    var h = opts.compact ? 4 : 12, w = 240, gap = 2, x = 0;
    var visible = list.filter(function (p) { return num(p.value) > 0; });
    var usable = w - gap * Math.max(0, visible.length - 1);
    var key = opts.key || 'stack:' + label, grow = animate(key, 'chart-grow-x');
    var segs = total > 0 ? list.map(function (p, i) {
      var v = num(p.value); if (!v) return '';
      var sw = Math.max(1, v / total * usable), rect = '<rect class="chart-mark' + grow + '" x="' + x.toFixed(2) + '" y="0" width="' + sw.toFixed(2) + '" height="' + h + '" rx="' + (opts.compact ? 2 : 3) + '" style="fill:' + colour(p, i) + '" data-chart-tip="' + esc(p.label + ' · ' + fmt(v) + ' · ' + pct(v, total) + '%') + '"/>';
      x += sw + gap; return rect;
    }).join('') : '<rect x="0" y="0" width="' + w + '" height="' + h + '" rx="2" style="fill:var(--surface-sunken)"/>';
    var svg = '<svg class="chart-stack" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" role="img" aria-labelledby="%LABELLEDBY%" focusable="false">%TITLE%' + segs + '</svg>';
    if (opts.compact) {
      return '<span class="chart-proportion" role="img" aria-label="' + esc(label + ': ' + summary) + '">' + svg.replace(' role="img" aria-labelledby="%LABELLEDBY%"', ' aria-hidden="true"').replace('%TITLE%', '') + '</span>';
    }
    var legend = '<ul class="chart-legend" aria-hidden="true">' + list.map(function (p, i) {
      return '<li><span class="chart-key" style="background:' + colour(p, i) + '"></span>' + esc(p.label) + ' <strong>' + esc(fmt(num(p.value))) + '</strong></li>';
    }).join('') + '</ul>';
    return figure('stacked', label, summary, svg + legend, list.map(function (p) { return [p.label, fmt(num(p.value))]; }), total > 0);
  }

  /* Donut: stroke-dasharray arcs on a circle, centre total, legend. */
  function donut(parts, opts) {
    opts = opts || {};
    var list = fold(parts, 6), label = opts.label || 'Distribution';
    var total = list.reduce(function (s, x) { return s + num(x.value); }, 0);
    var r = 40, c = 2 * Math.PI * r, offset = 0, gap = total > 0 && list.filter(function (p) { return num(p.value) > 0; }).length > 1 ? 2 : 0;
    var key = opts.key || 'donut:' + label, draw = animate(key, 'chart-draw-arc');
    var arcs = total > 0 ? list.map(function (p, i) {
      var v = num(p.value); if (!v) return '';
      var len = Math.max(0.5, v / total * c - gap);
      var arc = '<circle class="chart-mark' + draw + '" cx="50" cy="50" r="' + r + '" fill="none" stroke-width="12" style="stroke:' + colour(p, i) + ';--ci:' + i + '" stroke-dasharray="' + len.toFixed(2) + ' ' + (c - len).toFixed(2) + '" stroke-dashoffset="' + (-offset).toFixed(2) + '" transform="rotate(-90 50 50)" data-chart-tip="' + esc(p.label + ' · ' + fmt(v) + ' · ' + pct(v, total) + '%') + '"/>';
      offset += v / total * c; return arc;
    }).join('') : '';
    var summary = total > 0 ? list.map(function (p) { return p.label + ' ' + fmt(num(p.value)) + ' (' + pct(num(p.value), total) + '%)'; }).join(', ') + '.' : 'No records yet.';
    var svg = '<svg class="chart-donut-svg" viewBox="0 0 100 100" role="img" aria-labelledby="%LABELLEDBY%" focusable="false">%TITLE%' +
      '<circle cx="50" cy="50" r="' + r + '" fill="none" stroke-width="12" style="stroke:var(--surface-sunken)"/>' + arcs +
      '<text x="50" y="49" text-anchor="middle" class="chart-donut-total">' + esc(compact(total)) + '</text><text x="50" y="63" text-anchor="middle" class="chart-donut-caption">' + esc(opts.center || 'total') + '</text></svg>';
    var legend = '<ul class="chart-legend chart-legend-stack" aria-hidden="true">' + list.map(function (p, i) {
      return '<li><span class="chart-key" style="background:' + colour(p, i) + '"></span><span>' + esc(p.label) + '</span><strong>' + esc(fmt(num(p.value))) + '</strong></li>';
    }).join('') + '</ul>';
    return figure('donut', label, summary, '<div class="chart-donut-layout">' + svg + legend + '</div>', list.map(function (p) { return [p.label, fmt(num(p.value))]; }), total > 0);
  }

  /* Histogram / column chart over ordered bins [{label, value}]. */
  function histogram(bins, opts) {
    opts = opts || {};
    var list = (bins || []).map(function (b) { return { label: b.label, value: num(b.value), tone: b.tone, tip: b.tip, display: b.display }; }), label = opts.label || 'Histogram';
    var shown = function (b) { return b.display != null ? b.display : fmt(b.value); };
    if (!list.length) return '';
    var w = 240, h = 72, base = h - 14, gap = 2, n = list.length;
    var bw = Math.min(24, (w - gap * (n - 1)) / n), step = (w - bw) / Math.max(1, n - 1);
    var max = Math.max.apply(null, list.map(function (b) { return b.value; })) || 1;
    var key = opts.key || 'hist:' + label, grow = animate(key, 'chart-grow-y');
    var cols = list.map(function (b, i) {
      var bh = Math.max(b.value ? 2 : 0, b.value / max * (base - 4)), x = n === 1 ? (w - bw) / 2 : i * step;
      return '<rect class="chart-mark' + grow + '" x="' + x.toFixed(2) + '" y="' + (base - bh).toFixed(2) + '" width="' + bw.toFixed(2) + '" height="' + bh.toFixed(2) + '" rx="2" style="fill:' + (b.tone ? TONES[b.tone] : 'var(--accent)') + ';--ci:' + Math.min(i, 12) + '" data-chart-tip="' + esc(b.tip || b.label + ' · ' + shown(b)) + '"/>';
    }).join('');
    var axis = '<line x1="0" x2="' + w + '" y1="' + base + '" y2="' + base + '" style="stroke:var(--chart-grid)" stroke-width="1"/>' +
      '<text x="0" y="' + h + '" class="chart-axis-label">' + esc(list[0].label) + '</text>' +
      (n > 1 ? '<text x="' + w + '" y="' + h + '" text-anchor="end" class="chart-axis-label">' + esc(list[n - 1].label) + '</text>' : '');
    var summary = opts.summary || list.map(function (b) { return b.label + ' ' + shown(b); }).join(', ') + '.';
    var svg = '<svg class="chart-histogram" viewBox="0 0 ' + w + ' ' + h + '" role="img" aria-labelledby="%LABELLEDBY%" focusable="false">%TITLE%' + cols + axis + '</svg>';
    return figure('histogram', label, summary, svg, list.map(function (b) { return [b.label, shown(b)]; }), true);
  }

  /* Observed columns against a reference marker per bin (one shared y-scale),
     e.g. a first-digit distribution against Benford's expected shares.
     bins: [{label, value, reference, tip}]; values and references are shares. */
  function paired(bins, opts) {
    opts = opts || {};
    var list = (bins || []).map(function (b) { return { label: b.label, value: num(b.value), reference: b.reference == null ? null : num(b.reference), tip: b.tip }; });
    var label = opts.label || 'Observed against expected';
    if (!list.length) return '';
    var w = 240, h = 84, base = h - 14, gap = 4, n = list.length;
    var bw = (w - gap * (n - 1)) / n;
    var max = Math.max.apply(null, list.map(function (b) { return Math.max(b.value, b.reference || 0); })) || 1;
    var y = function (v) { return base - v / max * (base - 4); };
    var key = opts.key || 'paired:' + label, grow = animate(key, 'chart-grow-y');
    var marks = list.map(function (b, i) {
      var x = i * (bw + gap), top = y(b.value);
      var ref = b.reference == null ? '' : '<line class="chart-reference" x1="' + (x - 1).toFixed(2) + '" x2="' + (x + bw + 1).toFixed(2) + '" y1="' + y(b.reference).toFixed(2) + '" y2="' + y(b.reference).toFixed(2) + '"/>';
      return '<rect class="chart-mark' + grow + '" x="' + x.toFixed(2) + '" y="' + top.toFixed(2) + '" width="' + bw.toFixed(2) + '" height="' + Math.max(b.value ? 1 : 0, base - top).toFixed(2) + '" rx="2" style="fill:var(--series-1);--ci:' + Math.min(i, 12) + '" data-chart-tip="' + esc(b.tip || b.label + ' · ' + fmt(b.value)) + '"/>' + ref +
        '<text x="' + (x + bw / 2).toFixed(2) + '" y="' + h + '" text-anchor="middle" class="chart-axis-label">' + esc(b.label) + '</text>';
    }).join('');
    var svg = '<svg class="chart-histogram chart-paired" viewBox="0 0 ' + w + ' ' + h + '" role="img" aria-labelledby="%LABELLEDBY%" focusable="false">%TITLE%' +
      '<line x1="0" x2="' + w + '" y1="' + base + '" y2="' + base + '" style="stroke:var(--chart-grid)" stroke-width="1"/>' + marks + '</svg>';
    var legend = '<ul class="chart-legend" aria-hidden="true"><li><span class="chart-key" style="background:var(--series-1)"></span>' + esc(opts.valueLabel || 'Observed') + '</li><li><span class="chart-key chart-key-line"></span>' + esc(opts.referenceLabel || 'Expected') + '</li></ul>';
    var summary = opts.summary || list.map(function (b) { return b.label + ' ' + fmt(b.value) + (b.reference == null ? '' : ' (expected ' + fmt(b.reference) + ')'); }).join(', ') + '.';
    return figure('paired', label, summary, svg + legend, list.map(function (b) { return [b.label, b.tip || fmt(b.value)]; }), true);
  }

  /* Workflow funnel (HTML): each step is a real button so existing
     delegated handlers ([data-tab]) navigate; bars are relative to step 1. */
  function funnel(steps, opts) {
    opts = opts || {};
    var list = (steps || []).map(function (s) { return { label: s.label, value: num(s.value), tab: s.tab }; }), label = opts.label || 'Workflow';
    var max = Math.max.apply(null, list.map(function (s) { return s.value; }).concat([0])) || 1;
    var key = opts.key || 'funnel:' + label, grow = animate(key, 'chart-grow-x');
    var items = list.map(function (s, i) {
      var inner = '<span class="funnel-label">' + esc(s.label) + '</span><strong class="funnel-value">' + esc(fmt(s.value)) + '</strong><span class="funnel-track" aria-hidden="true"><span class="funnel-fill' + grow + '" style="width:' + Math.max(s.value ? 1.5 : 0, s.value / max * 100).toFixed(2) + '%;--ci:' + i + '"></span></span>';
      return '<li>' + (s.tab ? '<button type="button" class="funnel-step" data-tab="' + esc(s.tab) + '">' + inner + '</button>' : '<div class="funnel-step">' + inner + '</div>') + '</li>';
    }).join('');
    return '<figure class="chart chart-funnel"><figcaption class="chart-caption">' + esc(label) + '</figcaption><ol class="funnel-list">' + items + '</ol></figure>';
  }

  /* Area chart (investing-platform style): 2px line coloured by the direction
     of the selected range, vertical gradient fill, dotted start reference,
     right-hand axis with a value pill, dashed crosshair + pinned readout, and
     an optional range control. points: [{label, value, display, note}]. */
  var areaRegistry = Object.create(null), areaOrder = [];
  function area(points, opts) {
    opts = opts || {};
    var all = (points || []).filter(function (p) { return p && Number.isFinite(Number(p.value)); });
    var label = opts.label || 'Trend';
    if (!all.length) return '';
    var id = opts.id || nextId('area');
    areaRegistry[id] = { points: all, opts: opts };
    areaOrder.push(id);
    if (areaOrder.length > 24) delete areaRegistry[areaOrder.shift()];
    var ranges = (opts.ranges || []).filter(function (r) { return r[1] < all.length; });
    var span = opts.range && opts.range < all.length ? opts.range : all.length;
    var list = all.slice(all.length - span), data = list.map(function (p) { return num(p.value); });
    var show = function (p) { return p.display != null ? p.display : fmt(p.value); };
    var first = data[0], last = data[data.length - 1];
    var dir = data.length < 2 || last === first ? 'flat' : last > first ? 'up' : 'down';
    var stroke = 'var(--' + dir + ', var(--accent))';
    var w = 600, h = 200, padR = 64, padT = 12, padB = 26, plotW = w - padR, plotH = h - padT - padB;
    var min = Math.min.apply(null, data.concat([0])), max = Math.max.apply(null, data);
    if (max === min) max = min + 1;
    var y = function (v) { return padT + plotH - (v - min) / (max - min) * plotH; };
    var x = function (i) { return data.length === 1 ? plotW / 2 : 8 + i / (data.length - 1) * (plotW - 16); };
    var pts = data.map(function (v, i) { return [x(i), y(v)]; });
    var line = pts.map(function (p, i) { return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join('');
    var gid = id + '-grad';
    var grid = [0, 1, 2, 3].map(function (k) {
      var v = min + (max - min) * k / 3, yy = y(v);
      return '<line class="area-grid" x1="0" x2="' + plotW + '" y1="' + yy.toFixed(1) + '" y2="' + yy.toFixed(1) + '"/>' +
        '<text class="chart-axis-label area-axis" x="' + (w - 4) + '" y="' + (yy + 4).toFixed(1) + '" text-anchor="end">' + esc(compact(v)) + '</text>';
    }).join('');
    var xLabels = list.map(function (p, i) {
      if (list.length > 6 && i % Math.ceil(list.length / 6) !== 0 && i !== list.length - 1) return '';
      return '<text class="chart-axis-label" x="' + x(i).toFixed(1) + '" y="' + (h - 6) + '" text-anchor="' + (i === 0 && list.length > 1 ? 'start' : i === list.length - 1 && list.length > 1 ? 'end' : 'middle') + '">' + esc(p.label) + '</text>';
    }).join('');
    var lastPt = pts[pts.length - 1];
    var ref = data.length > 1 ? '<line class="area-reference" x1="0" x2="' + plotW + '" y1="' + y(first).toFixed(1) + '" y2="' + y(first).toFixed(1) + '"/>' : '';
    var pill = '<g class="area-pill" style="--area-colour:' + stroke + '"><rect x="' + (plotW + 4) + '" y="' + (lastPt[1] - 10).toFixed(1) + '" width="' + (padR - 6) + '" height="20" rx="4"/><text x="' + (plotW + 4 + (padR - 6) / 2) + '" y="' + (lastPt[1] + 4).toFixed(1) + '" text-anchor="middle">' + esc(compact(last)) + '</text></g>';
    var hitsHtml = pts.map(function (p, i) {
      var left = i ? (pts[i - 1][0] + p[0]) / 2 : 0, right = i < pts.length - 1 ? (p[0] + pts[i + 1][0]) / 2 : plotW;
      var tipText = list[i].label + ' · ' + show(list[i]) + (list[i].note ? ' · ' + list[i].note : '');
      return '<rect class="chart-hit" x="' + left.toFixed(1) + '" y="0" width="' + Math.max(1, right - left).toFixed(1) + '" height="' + (h - padB) + '" data-chart-tip="' + esc(tipText) + '" data-area-x="' + p[0].toFixed(1) + '" data-area-y="' + p[1].toFixed(1) + '" data-area-label="' + esc(list[i].label) + '" data-area-value="' + esc(show(list[i])) + '"/>';
    }).join('');
    var key = opts.key || 'area:' + label;
    var svg = '<svg class="chart-area-svg" viewBox="0 0 ' + w + ' ' + h + '" role="img" aria-labelledby="%LABELLEDBY%" focusable="false">%TITLE%' +
      '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:' + stroke + ';stop-opacity:.34"/><stop offset="1" style="stop-color:' + stroke + ';stop-opacity:0"/></linearGradient></defs>' +
      grid + ref +
      (data.length > 1 ? '<path d="' + line + 'L' + lastPt[0].toFixed(1) + ' ' + (padT + plotH) + 'L' + pts[0][0].toFixed(1) + ' ' + (padT + plotH) + 'Z" fill="url(#' + gid + ')" stroke="none"/>' +
        '<path class="area-line' + animate(key, 'chart-draw') + '" pathLength="1" d="' + line + '" fill="none" style="stroke:' + stroke + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' : '') +
      '<circle class="area-end" cx="' + lastPt[0].toFixed(1) + '" cy="' + lastPt[1].toFixed(1) + '" r="4" style="fill:' + stroke + '"/>' +
      '<line class="area-crosshair" x1="0" x2="0" y1="' + padT + '" y2="' + (padT + plotH) + '" hidden/>' +
      '<circle class="area-focus" r="5" cx="0" cy="0" style="fill:' + stroke + '" hidden/>' +
      pill + xLabels + hitsHtml + '</svg>';
    var summary = opts.summary || label + ': ' + list.map(function (p) { return p.label + ' ' + show(p); }).join(', ') + '.';
    var rangeHtml = ranges.length ? '<div class="area-ranges" role="group" aria-label="Time range">' + ranges.concat([['All', all.length]]).map(function (r) {
      return '<button type="button" class="area-range" data-area-range="' + r[1] + '" data-area-id="' + id + '" aria-pressed="' + (r[1] === span) + '">' + esc(r[0]) + '</button>';
    }).join('') + '</div>' : '';
    var head = '<div class="area-head"><div class="area-readout" aria-hidden="true"><span class="area-readout-label">' + esc(list[list.length - 1].label) + '</span> <strong class="area-readout-value">' + esc(show(list[list.length - 1])) + '</strong></div>' + rangeHtml + '</div>';
    var note = list.length < 2 ? '<p class="area-note small muted">' + esc(opts.empty || 'Only one period has data so far.') + '</p>' : '';
    return '<div class="area-chart is-' + dir + '" data-area-host="' + id + '">' + head +
      figure('area', label, summary, svg, list.map(function (p) { return [p.label, show(p)]; }), true) + note + '</div>';
  }
  function areaFocus(mark) {
    var host = mark && mark.closest && mark.closest('.area-chart');
    if (!host || !mark.hasAttribute('data-area-x')) return;
    var cross = host.querySelector('.area-crosshair'), dot = host.querySelector('.area-focus');
    var xv = mark.getAttribute('data-area-x'), yv = mark.getAttribute('data-area-y');
    if (cross) { cross.setAttribute('x1', xv); cross.setAttribute('x2', xv); cross.removeAttribute('hidden'); }
    if (dot) { dot.setAttribute('cx', xv); dot.setAttribute('cy', yv); dot.removeAttribute('hidden'); }
    var l = host.querySelector('.area-readout-label'), v = host.querySelector('.area-readout-value');
    if (l) l.textContent = mark.getAttribute('data-area-label');
    if (v) v.textContent = mark.getAttribute('data-area-value');
  }
  function areaReset(host) {
    if (!host) return;
    var cross = host.querySelector('.area-crosshair'), dot = host.querySelector('.area-focus'), marks = host.querySelectorAll('[data-area-x]');
    if (cross) cross.setAttribute('hidden', '');
    if (dot) dot.setAttribute('hidden', '');
    var lastMark = marks[marks.length - 1];
    if (lastMark) {
      var l = host.querySelector('.area-readout-label'), v = host.querySelector('.area-readout-value');
      if (l) l.textContent = lastMark.getAttribute('data-area-label');
      if (v) v.textContent = lastMark.getAttribute('data-area-value');
    }
  }
  document.addEventListener('pointerleave', function (event) {
    if (event.target && event.target.classList && event.target.classList.contains('chart-area')) areaReset(event.target.closest('.area-chart'));
  }, true);
  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('[data-area-range]');
    if (!button) return;
    var id = button.getAttribute('data-area-id'), entry = areaRegistry[id], host = button.closest('.area-chart');
    if (!entry || !host) return;
    var opts = {}; Object.keys(entry.opts).forEach(function (k) { opts[k] = entry.opts[k]; });
    opts.range = Number(button.getAttribute('data-area-range')); opts.id = id;
    var holder = document.createElement('div');
    holder.innerHTML = area(entry.points, opts);
    var next = holder.firstChild;
    host.replaceWith(next);
    var again = next.querySelector('[data-area-range="' + opts.range + '"]');
    if (again) again.focus();
  });

  /* Shared tooltip: hover and keyboard (arrow keys on a focused chart). */
  var tip = null, active = null, described = null;
  function ensureTip() {
    if (tip) return tip;
    tip = document.createElement('div');
    tip.id = 'chart-tooltip';
    tip.className = 'chart-tooltip';
    tip.setAttribute('role', 'tooltip');
    tip.hidden = true;
    document.body.appendChild(tip);
    return tip;
  }
  function show(mark, owner) {
    var t = ensureTip(), rect = mark.getBoundingClientRect();
    t.textContent = mark.getAttribute('data-chart-tip');
    t.hidden = false;
    var x = Math.min(window.innerWidth - t.offsetWidth - 8, Math.max(8, rect.left + rect.width / 2 - t.offsetWidth / 2));
    var y = rect.top - t.offsetHeight - 8;
    if (y < 8) y = rect.bottom + 8;
    t.style.left = x + 'px';
    t.style.top = y + 'px';
    if (active && active !== mark) active.classList.remove('is-active');
    active = mark;
    mark.classList.add('is-active');
    areaFocus(mark);
    if (owner) { undescribe(owner); described = owner; owner.setAttribute('aria-describedby', t.id); }
  }
  /* Only the chart showing the tip points at it; otherwise a chart left
     earlier would be described by another chart's current value. */
  function undescribe(keep) {
    if (described && described !== keep) described.removeAttribute('aria-describedby');
    if (described !== keep) described = null;
  }
  function hide() {
    if (tip) tip.hidden = true;
    if (active) { active.classList.remove('is-active'); areaReset(active.closest && active.closest('.area-chart')); }
    active = null;
    undescribe(null);
  }
  document.addEventListener('pointerover', function (event) {
    var mark = event.target.closest && event.target.closest('[data-chart-tip]');
    if (mark) show(mark, mark.closest('[data-chart]')); else if (active) hide();
  });
  document.addEventListener('focusout', function (event) { if (event.target.matches && event.target.matches('[data-chart]')) hide(); });
  /* Keyboard focus scrolls an off-screen chart into view (smoothly, over several
     frames): keep the focused chart's tooltip and follow its mark; hover tips hide. */
  document.addEventListener('scroll', function () {
    var owner = active && active.closest('[data-chart]');
    if (owner && owner.contains(document.activeElement) && active.isConnected) show(active, null); else hide();
  }, true);
  document.addEventListener('keydown', function (event) {
    var chart = event.target.closest && event.target.closest('[data-chart]');
    if (!chart || ['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape'].indexOf(event.key) < 0) return;
    if (event.key === 'Escape') { if (active) { hide(); event.stopPropagation(); } return; }
    var marks = Array.prototype.slice.call(chart.querySelectorAll('[data-chart-tip]'));
    if (!marks.length) return;
    event.preventDefault();
    var i = marks.indexOf(active);
    if (event.key === 'Home') i = 0;
    else if (event.key === 'End') i = marks.length - 1;
    else i = (i + (event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1) + marks.length) % marks.length;
    show(marks[i], chart);
  });

  window.MiniCharts = Object.freeze({
    sparkline: sparkline,
    bar: bar,
    stackedBar: stackedBar,
    donut: donut,
    histogram: histogram,
    paired: paired,
    funnel: funnel,
    area: area,
    format: Object.freeze({ number: fmt, compact: compact, percent: pct })
  });
})();
