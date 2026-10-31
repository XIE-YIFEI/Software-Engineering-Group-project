/* Congestion forecast: a replay clock over a recorded day; per section the current state and the +5 / +15 / +30 min
   forecasts with their error band and a warning; forecast vs actual (model accuracy); the synced time-lapse; and the
   what-if "open more lanes" queue model with its assumptions. */
(async () => {
  const { el, clear, $, get, pill, pct, num, fail, player } = App;
  const toMin = (iso) => +iso.slice(11, 13) * 60 + +iso.slice(14, 16);
  const hhmm = (m) => `${String(Math.floor(m / 60) % 24).padStart(2, "0")}:${String(Math.round(m) % 60).padStart(2, "0")}`;
  const vp = player($("#player"), "Time-lapse");
  let days, day, minutes = [], idx = 0, horizon = 30, cap = 20, timer = null, acc = null, pending = null;

  try { days = await get("/api/v1/forecast/days"); } catch (e) { return fail(e); }
  const dsel = $("#day");
  for (const d of days.filter((d) => d.sections.length)) dsel.append(el("option", { value: d.code, text: `${d.label} · ${d.camera}` }));
  dsel.addEventListener("change", () => setDay(dsel.value));
  $("#horizon").addEventListener("click", (ev) => { const b = ev.target.closest("button"); if (!b) return; horizon = +b.dataset.h; $("#horizon").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); drawAcc(); });
  $("#cap").addEventListener("click", (ev) => { const b = ev.target.closest("button"); if (!b) return; cap = +b.dataset.p; $("#cap").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); drawWhatIf(); });
  $("#section").addEventListener("change", () => { drawAcc(); drawWhatIf(); });
  $("#wstart").addEventListener("change", drawWhatIf);
  $("#mu").addEventListener("change", drawWhatIf);
  $("#slider").addEventListener("input", (ev) => setIdx(+ev.target.value));
  $("#prev").addEventListener("click", () => setIdx(idx - 1));
  $("#next").addEventListener("click", () => setIdx(idx + 1));
  $("#towarn").addEventListener("click", () => { if (day.first_warning) setIdx(minutes.indexOf(day.first_warning)); });
  $("#play").addEventListener("click", () => {
    if (timer) { clearInterval(timer); timer = null; $("#play").textContent = "Play"; return; }
    $("#play").textContent = "Pause";
    timer = setInterval(() => { if (idx >= minutes.length - 1) { $("#play").click(); return; } setIdx(idx + 1); }, 350);
  });

  async function setDay(code) {
    day = days.find((d) => d.code === code);
    const sec = $("#section"); clear(sec);
    for (const s of day.sections) sec.append(el("option", { value: s, text: s === "To CIQ" ? "To CIQ (whole approach)" : s === "TC1" ? "TC1 (queue head)" : s }));
    $("#daynote").textContent = `${day.camera} · ${day.label}. ${day.model}, trained on ${day.train_day} only. Congestion onsets: ${day.onsets.map((o) => o.slice(11, 16)).join(", ") || "none"}`
      + (day.first_warning ? ` · first warning ${day.first_warning.slice(11, 16)}` : "") + `. Measured with ±${num(day.loo_mae_vehicles, 0)} vehicles error per frame (hand-count validation). Validated on 2 days only.`;
    if (day.video_url) { vp.load(day.video_url, 0, "Time-lapse, 10 s of camera time per frame", false); $("#videonote").textContent = `Time-lapse of ${hhmm(day.video_from_minute)}–${hhmm(day.video_to_minute)} (one keyframe every 10 s of camera time), kept at the replay clock.`; }
    else { clear($("#player")).append(el("div", { class: "card empty", text: "No time-lapse video for this day." })); $("#videonote").textContent = ""; }
    acc = await get("/api/v1/forecast/accuracy", { day: code, section: "To CIQ", horizon: 30 }).catch(fail);
    minutes = acc ? acc.minutes : [];
    $("#slider").max = Math.max(0, minutes.length - 1);
    $("#wstart").value = (day.first_warning || minutes[0] || "T06:00").slice(11, 16);
    $("#mu").value = "";
    const start = day.first_warning ? minutes.indexOf(day.first_warning) : 0;
    idx = Math.max(0, start);
    await drawAcc(); drawWhatIf(); setIdx(idx, true);
  }

  function setIdx(i, force) {
    i = Math.max(0, Math.min(minutes.length - 1, i));
    if (i === idx && !force) return;
    idx = i;
    $("#slider").value = i;
    const m = minutes[i];
    $("#clock").textContent = m ? m.slice(11, 16) : "--:--";
    if (day.video_url && m) {
      const mod = toMin(m), f = day.video_from_minute, t = day.video_to_minute;
      if (mod >= f && mod <= t) { const v = vp.video; const go = () => { v.currentTime = (mod - f) / (t - f) * day.video_duration_sec; }; v.readyState ? go() : v.addEventListener("loadedmetadata", go, { once: true }); }
    }
    clearTimeout(pending);
    pending = setTimeout(() => { drawState(); drawAccNow(); }, timer ? 0 : 80);
  }

  async function drawState() {
    const m = minutes[idx]; if (!m) return;
    let st;
    try { st = await get("/api/v1/forecast/state", { day: day.code, at: m.slice(11, 16) }); } catch (e) { return fail(e); }
    const box = clear($("#sections"));
    for (const s of st.sections) {
      const STATE = { free: ["Free", "green"], dense: ["Dense", "yellow"], queue: ["Queue", "red"], "no data": ["No data", "gray"] };
      box.append(el("div", { class: "card tile", style: s.warning ? { borderColor: "rgba(255,159,10,.6)" } : {} },
        el("div", { class: "row between" }, el("span", { class: "label", text: s.label }), pill(...STATE[s.state])),
        el("div", { class: "value", text: pct(s.now) }),
        el("div", { class: "sub", text: s.vehicles !== null ? `${num(s.vehicles, 0)} of ${num(s.capacity)} vehicles (jam capacity)` : "" }),
        el("div", { style: { margin: "8px 0" } }, pill(s.status, s.status_colour)),
        el("div", { class: "row", style: { gap: "12px", fontSize: "13px" } }, ...["5", "15", "30"].map((h) =>
          el("span", { class: "muted" }, `+${h} `, el("b", { class: "num", style: { color: "var(--text)" }, text: pct(s.forecast[h]) }), el("span", { class: "faint", text: ` ±${Math.round((s.band[h] || 0) * 100)}` })))),
        el("div", { class: "faint", style: { fontSize: "11px", marginTop: "6px" }, text: s.origin === "committed" ? "committed forecast (forecast_demo.py)" : "computed at import (alpr.forecast)" })));
    }
  }

  async function drawAcc() {
    try { acc = await get("/api/v1/forecast/accuracy", { day: day.code, section: $("#section").value, horizon }); } catch (e) { return fail(e); }
    drawAccNow();
    const s = clear($("#accstats"));
    s.append(pill(`MAE this day ${num(acc.mae, 3)} (${Math.round(acc.mae * 100)} pts)`, "blue", false),
      acc.band !== null ? pill(`expected band ±${Math.round(acc.band * 100)} pts`, "gray", false) : null,
      acc.precision !== null ? pill(`congestion precision ${num(acc.precision, 2)} · recall ${num(acc.recall, 2)}`, "gray", false) : null,
      ...acc.onsets.map((o, i) => pill(`onset ${o.slice(11, 16)}: warned ${acc.warning_min[i] === null ? "never" : acc.warning_min[i] + " min"} ahead`, "orange", false)));
  }

  function drawAccNow() {
    if (!acc) return;
    const x = acc.minutes.map(toMin), now = minutes[idx] ? toMin(minutes[idx]) : null, c = Charts.SERIES();
    Charts.line($("#acc"), { x, xLabel: "minute", xFormat: hhmm, yFormat: (v) => `${Math.round(v * 100)} %`, yMin: 0, height: 250,
      title: `measured fill vs the forecast made ${horizon} minutes earlier`,
      series: [{ name: "Measured", values: acc.measured, color: c[0] }, { name: `Forecast made ${horizon} min earlier`, values: acc.forecast, color: c[1], dash: true }],
      refs: [{ y: acc.threshold, label: "Congestion (50 % of jam capacity)" }],
      marks: [...acc.onsets.map((o) => ({ x: toMin(o), label: "onset" })), ...(now !== null ? [{ x: now, label: hhmm(now), color: "#ffffff", width: 1.5 }] : [])],
      onClick: (i) => setIdx(minutes.indexOf(acc.minutes[i])) });
  }

  async function drawWhatIf() {
    let w;
    const params = { day: day.code, section: $("#section").value, capacity_pct: cap, start: $("#wstart").value || undefined, mu: $("#mu").value || undefined };
    try { w = await get("/api/v1/forecast/whatif", params); } catch (e) { clear($("#whatif")).append(el("div", { class: "empty", text: e.message })); return; }
    const c = Charts.SERIES(), x = w.minutes.map(toMin);
    Charts.line($("#whatif"), { x, xLabel: "minute", xFormat: hhmm, yFormat: (v) => num(v, 0), yMin: 0, height: 240, title: "queue in view: measured vs projected",
      series: [{ name: "Measured queue (vehicles in view)", values: w.baseline, color: c[0] }, { name: `Projected with +${w.capacity_pct} % capacity`, values: w.projected, color: c[2], dash: true }],
      refs: [{ y: w.threshold_vehicles, label: "Congested (50 % of jam capacity)" }],
      marks: [{ x: toMin(w.start_minute), label: `+${w.capacity_pct} % from ${w.start_minute.slice(11, 16)}` }] });
    $("#mu").placeholder = `estimated ${num(w.mu_estimate, 2)}`;
    $("#wstats").textContent = `μ = ${num(w.mu, 2)} veh/min (${w.mu_source}${w.mu_estimated_at ? `, fastest clearing ending ${w.mu_estimated_at.slice(11, 16)}` : ""}) · congested ${w.congested_minutes[0]} → ${w.congested_minutes[1]} min · peak ${num(w.peak[0], 0)} → ${num(w.peak[1], 0)} vehicles in view`;
    const ul = clear($("#assumptions"));
    for (const a of w.assumptions) ul.append(el("li", { text: a }));
  }

  if (dsel.options.length) setDay(dsel.value);
  else clear($("#sections")).append(el("div", { class: "card empty", text: "No congestion series imported." }));
})();
