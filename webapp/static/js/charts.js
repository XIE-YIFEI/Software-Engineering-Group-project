/* Small SVG charts without a library (works offline): line charts with a crosshair tooltip, stacked column charts with
   per-column tooltips, sparklines. Rules (dataviz): thin marks (2 px lines, columns <= 24 px with a 4 px rounded data
   end and a 2 px gap between stacked segments), solid hairline grid, one y-axis, a legend whenever there are two or
   more series, labels in text colours (never the series colour), and a table view of every chart so a tooltip never
   gates a value. Series colours are the validated slots --series-1..4 of app.css. */
const Charts = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const SERIES = () => [css("--series-1"), css("--series-2"), css("--series-3"), css("--series-4")];
  const node = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) n.setAttribute(k, v); return n; };
  const txt = (s, attrs) => { const t = node("text", attrs); t.textContent = s; return t; };

  function niceStep(span, n) {
    const raw = span / Math.max(1, n), p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const m = raw / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
  }
  function ticks(min, max, n = 4) {
    if (max <= min) max = min + 1;
    const step = niceStep(max - min, n);
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    const out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
    return { lo, hi, out };
  }
  const compact = (v) => Math.abs(v) >= 1000 ? (v / 1000).toFixed(v % 1000 ? 1 : 0) + "K" : String(+v.toFixed(2));

  /* Redraw a chart when its container's width changes (charts are drawn at the pixel width they get). */
  function keepFitted(container, draw) {
    container._redraw = draw;
    if (container._ro) return;
    container._ro = new ResizeObserver(() => {
      const w = container.clientWidth;
      if (w && Math.abs(w - (container._w || 0)) > 8 && container._redraw) container._redraw();
    });
    container._ro.observe(container);
  }

  function frame(container, opts, kind) {
    App.clear(container);
    container.classList.add("chart");
    container._w = container.clientWidth;
    const head = App.el("div", { class: "row between" });
    const legend = App.el("div", { class: "legend" });
    if (opts.series.length >= 2) for (const s of opts.series)
      legend.append(App.el("span", { class: "key" }, App.el("i", { class: kind === "bar" ? "box" : "", style: { background: s.color, ...(s.dash ? { backgroundImage: "linear-gradient(90deg, transparent 50%, var(--card-solid) 50%)", backgroundSize: "6px 2px" } : {}) } }), s.name));
    const tableBtn = App.el("button", { class: "btn small ghost", type: "button", text: "Table" });
    head.append(legend, tableBtn);
    const wrap = App.el("div", { style: { position: "relative" } });
    const tip = App.el("div", { class: "tip" });
    const tableBox = App.el("div", { class: "scroll", style: { display: "none", maxHeight: "260px" } });
    tableBtn.addEventListener("click", () => {
      const show = tableBox.style.display === "none";
      tableBox.style.display = show ? "block" : "none";
      tableBtn.textContent = show ? "Chart only" : "Table";
      if (show && !tableBox.firstChild) tableBox.append(table(opts));
    });
    wrap.append(tip);
    container.append(head, wrap, tableBox);
    return { wrap, tip };
  }

  function table(opts) {
    const fx = opts.xFormat || String, fy = opts.yFormat || ((v) => v);
    return App.el("table", { class: "t" },
      App.el("thead", {}, App.el("tr", {}, App.el("th", { text: opts.xLabel || "" }), ...opts.series.map((s) => App.el("th", { class: "num", text: s.name })))),
      App.el("tbody", {}, ...opts.x.map((x, i) => App.el("tr", {}, App.el("td", { text: fx(x) }),
        ...opts.series.map((s) => App.el("td", { class: "num", text: s.values[i] === null || s.values[i] === undefined ? "–" : fy(s.values[i]) }))))));
  }

  function showTip(tip, wrap, px, py, title, rows) {
    App.clear(tip);
    tip.append(App.el("div", { class: "muted", text: title, style: { marginBottom: "4px" } }));
    for (const r of rows) tip.append(App.el("div", { class: "r" }, App.el("span", { class: "row", style: { gap: "6px" } }, App.el("i", { style: { background: r.color } }), App.el("span", { class: "muted", text: r.name })), App.el("b", { text: r.value })));
    tip.style.display = "block";
    const w = wrap.clientWidth, tw = tip.offsetWidth;
    tip.style.left = Math.min(Math.max(0, px + 14), w - tw) + "px";
    tip.style.top = Math.max(0, py - 10) + "px";
  }

  /* Line chart. opts: {x, xLabel, xFormat, series: [{name, values, color, dash, area}], yFormat, yMin, yMax, height,
     refs: [{y, label}], marks: [{x, label}], band: {lo: [], hi: []} (with series[0]), onClick(i)} */
  function line(container, opts) {
    keepFitted(container, () => line(container, opts));
    const { wrap, tip } = frame(container, opts, "line");
    const W = Math.max(280, wrap.clientWidth || container.clientWidth || 600), H = opts.height || 220;
    const m = { l: 46, r: 12, t: 10, b: 26 };
    const xs = opts.x, n = xs.length;
    const vals = opts.series.flatMap((s) => s.values).filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
    (opts.refs || []).forEach((r) => vals.push(r.y));
    const t = ticks(opts.yMin ?? Math.min(0, ...vals), opts.yMax ?? Math.max(...vals, 0.0001));
    const x0 = xs[0], x1 = xs[n - 1] === x0 ? x0 + 1 : xs[n - 1];
    const X = (x) => m.l + (x - x0) / (x1 - x0) * (W - m.l - m.r);
    const Y = (v) => m.t + (1 - (v - t.lo) / (t.hi - t.lo)) * (H - m.t - m.b);
    const s = node("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.title || "line chart" });
    for (const v of t.out) {
      s.append(node("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: v === t.lo ? css("--axis") : css("--grid"), "stroke-width": 1 }));
      s.append(txt((opts.yFormat || compact)(v), { x: m.l - 8, y: Y(v) + 4, "text-anchor": "end", fill: css("--text-3"), "font-size": 11 }));
    }
    const fx = opts.xFormat || String;
    const nx = Math.max(2, Math.min(7, n, Math.floor((W - m.l - m.r) / 78) + 1));   // ~78 px per x label
    for (let k = 0; k < nx; k++) {
      const i = Math.round(k * (n - 1) / Math.max(1, nx - 1));
      s.append(txt(fx(xs[i]), { x: X(xs[i]), y: H - 8, "text-anchor": k === 0 ? "start" : k === nx - 1 ? "end" : "middle", fill: css("--text-3"), "font-size": 11 }));
    }
    for (const r of opts.refs || []) {
      s.append(node("line", { x1: m.l, x2: W - m.r, y1: Y(r.y), y2: Y(r.y), stroke: css("--text-3"), "stroke-width": 1, "stroke-dasharray": "4 4" }));
      if (r.label) s.append(txt(r.label, { x: m.l + 6, y: Y(r.y) - 5, fill: css("--text-2"), "font-size": 11 }));
    }
    let lastRight = -Infinity, row = 0;
    for (const mk of [...(opts.marks || [])].sort((a, b) => a.x - b.x)) {
      if (mk.x < x0 || mk.x > x1) continue;
      s.append(node("line", { x1: X(mk.x), x2: X(mk.x), y1: m.t, y2: H - m.b, stroke: mk.color || css("--text-3"), "stroke-width": mk.width || 1 }));
      if (!mk.label) continue;
      const w = mk.label.length * 6.2 + 8, flip = X(mk.x) + w > W - m.r;          // labels never run past the plot
      const left = flip ? X(mk.x) - w : X(mk.x);
      row = left < lastRight ? row + 1 : 0;                                          // stagger labels that would collide
      lastRight = left + w;
      s.append(txt(mk.label, { x: flip ? X(mk.x) - 4 : X(mk.x) + 4, y: m.t + 10 + row * 13, "text-anchor": flip ? "end" : "start",
                               fill: mk.color && mk.color !== css("--text-3") ? css("--text") : css("--text-2"), "font-size": 11 }));
    }
    if (opts.band && opts.series[0]) {
      let d = "", back = "";
      opts.band.lo.forEach((lo, i) => { const hi = opts.band.hi[i]; if (lo === null || hi === null) return; d += (d ? "L" : "M") + X(xs[i]) + "," + Y(hi); back = "L" + X(xs[i]) + "," + Y(lo) + back; });
      if (d) s.append(node("path", { d: d + back + "Z", fill: opts.series[0].color, opacity: 0.1 }));
    }
    opts.series.forEach((ser) => {
      let d = "", started = false;
      ser.values.forEach((v, i) => {
        if (v === null || v === undefined || Number.isNaN(v)) { started = false; return; }
        d += (started ? "L" : "M") + X(xs[i]).toFixed(1) + "," + Y(v).toFixed(1);
        started = true;
      });
      if (ser.area && d) s.append(node("path", { d: d + `L${X(xs[n - 1])},${Y(t.lo)}L${X(xs[0])},${Y(t.lo)}Z`, fill: ser.color, opacity: 0.1 }));
      s.append(node("path", { d, fill: "none", stroke: ser.color, "stroke-width": ser.width || 2, "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": ser.dash ? "6 4" : null }));
    });
    const cross = node("line", { y1: m.t, y2: H - m.b, stroke: css("--text-2"), "stroke-width": 1, visibility: "hidden" });
    const dots = opts.series.map((ser) => node("circle", { r: 4, fill: ser.color, stroke: css("--card-solid"), "stroke-width": 2, visibility: "hidden" }));
    s.append(cross, ...dots);
    const hit = node("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent", style: opts.onClick ? "cursor:pointer" : null });
    s.append(hit);
    const nearest = (px) => {
      const x = x0 + (px - m.l) / (W - m.l - m.r) * (x1 - x0);
      let best = 0, bd = Infinity;
      for (let i = 0; i < n; i++) { const dd = Math.abs(xs[i] - x); if (dd < bd) { bd = dd; best = i; } }
      return best;
    };
    const pos = (ev) => { const r = s.getBoundingClientRect(); return [(ev.clientX - r.left) * W / r.width, (ev.clientY - r.top) * H / r.height, ev.clientX - r.left, ev.clientY - r.top]; };
    hit.addEventListener("pointermove", (ev) => {
      const [px, , cx, cy] = pos(ev), i = nearest(px);
      cross.setAttribute("x1", X(xs[i])); cross.setAttribute("x2", X(xs[i])); cross.setAttribute("visibility", "visible");
      opts.series.forEach((ser, k) => {
        const v = ser.values[i];
        if (v === null || v === undefined) { dots[k].setAttribute("visibility", "hidden"); return; }
        dots[k].setAttribute("cx", X(xs[i])); dots[k].setAttribute("cy", Y(v)); dots[k].setAttribute("visibility", "visible");
      });
      showTip(tip, wrap, cx, cy, fx(xs[i]), opts.series.map((ser) => ({ name: ser.name, color: ser.color,
        value: ser.values[i] === null || ser.values[i] === undefined ? "–" : (opts.yFormat || compact)(ser.values[i]) })));
    });
    hit.addEventListener("pointerleave", () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); });
    if (opts.onClick) hit.addEventListener("click", (ev) => opts.onClick(nearest(pos(ev)[0])));
    wrap.append(s);
    return { svg: s, X, Y };
  }

  /* Stacked columns. opts: {x, xLabel, xFormat, series: [{name, values, color}], yFormat, height, onClick(i)} */
  function bars(container, opts) {
    keepFitted(container, () => bars(container, opts));
    const { wrap, tip } = frame(container, opts, "bar");
    const W = Math.max(280, wrap.clientWidth || container.clientWidth || 600), H = opts.height || 200;
    const m = { l: 40, r: 8, t: 10, b: 26 };
    const n = opts.x.length;
    const totals = opts.x.map((_, i) => opts.series.reduce((a, s) => a + (s.values[i] || 0), 0));
    const t = ticks(0, Math.max(1, ...totals));
    const band = (W - m.l - m.r) / Math.max(1, n), bw = Math.max(2, Math.min(24, band * 0.7));
    const Y = (v) => m.t + (1 - v / t.hi) * (H - m.t - m.b);
    const s = node("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.title || "column chart" });
    for (const v of t.out) {
      s.append(node("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: v === 0 ? css("--axis") : css("--grid"), "stroke-width": 1 }));
      s.append(txt(compact(v), { x: m.l - 8, y: Y(v) + 4, "text-anchor": "end", fill: css("--text-3"), "font-size": 11 }));
    }
    const fx = opts.xFormat || String, every = Math.ceil(n / 8);
    opts.x.forEach((x, i) => {
      const cx = m.l + band * (i + 0.5);
      if (i % every === 0) s.append(txt(fx(x), { x: cx, y: H - 8, "text-anchor": "middle", fill: css("--text-3"), "font-size": 11 }));
      let base = 0;
      const g = node("g", { tabindex: 0, style: opts.onClick ? "cursor:pointer" : null });
      const segs = opts.series.map((ser) => ({ ser, v: ser.values[i] || 0 })).filter((z) => z.v > 0);
      segs.forEach((z, k) => {
        const y0 = Y(base), y1 = Y(base + z.v), top = k === segs.length - 1;
        const h = Math.max(0, y0 - y1 - (k > 0 ? 2 : 0));            // 2 px surface gap above the segment below
        const yTop = y0 - (k > 0 ? 2 : 0) - h, x = cx - bw / 2, r = top ? Math.min(4, h, bw / 2) : 0;
        const d = r ? `M${x},${yTop + h}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + bw - r}Q${x + bw},${yTop} ${x + bw},${yTop + r}V${yTop + h}Z`
                    : `M${x},${yTop + h}V${yTop}H${x + bw}V${yTop + h}Z`;
        g.append(node("path", { d, fill: z.ser.color }));
        base += z.v;
      });
      g.append(node("rect", { x: m.l + band * i, y: m.t, width: band, height: H - m.t - m.b, fill: "transparent" }));
      const show = (ev) => {
        const r = s.getBoundingClientRect();
        const px = ev.clientX ? ev.clientX - r.left : cx * r.width / W, py = ev.clientY ? ev.clientY - r.top : 20;
        showTip(tip, wrap, px, py, `${fx(x)} · ${totals[i]} total`, opts.series.map((ser) => ({ name: ser.name, color: ser.color, value: String(ser.values[i] || 0) })));
        g.setAttribute("opacity", 0.85);
      };
      g.addEventListener("pointermove", show); g.addEventListener("focus", show);
      const hide = () => { tip.style.display = "none"; g.removeAttribute("opacity"); };
      g.addEventListener("pointerleave", hide); g.addEventListener("blur", hide);
      if (opts.onClick) g.addEventListener("click", () => opts.onClick(i));
      s.append(g);
    });
    wrap.append(s);
    return { svg: s };
  }

  /* Sparkline: one series, no axes (the card's title names it). */
  function spark(container, values, opts = {}) {
    const W = opts.width || 140, H = opts.height || 28, n = values.length;
    const max = Math.max(1, ...values), s = node("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": opts.label || "trend" });
    if (n > 1) {
      const d = values.map((v, i) => `${i ? "L" : "M"}${(i / (n - 1) * (W - 4) + 2).toFixed(1)},${(H - 3 - v / max * (H - 6)).toFixed(1)}`).join("");
      s.append(node("path", { d, fill: "none", stroke: opts.color || css("--series-1"), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    }
    App.clear(container).append(s);
  }

  /* Re-render charts when their container is resized. */
  function responsive(container, draw) {
    let w = 0;
    const ro = new ResizeObserver(() => { const cw = container.clientWidth; if (Math.abs(cw - w) > 8) { w = cw; draw(); } });
    ro.observe(container);
    return ro;
  }

  return { line, bars, spark, responsive, SERIES };
})();
