/* Traffic analytics: flow by vehicle type, lane and time; per-lane occupancy, density and speed; lane events with
   click-to-timestamp playback; CSV export. */
(async () => {
  const { el, clear, $, get, query, pill, plate, provPill, pct, num, dur, time, fail, player, download, EVENT_COLOUR } = App;
  let vp = player($("#player"), "Video of the run");
  const TYPES = ["Car", "Motorcycle", "Truck", "Bus"];
  let runs, run, summary;
  try { runs = await get("/api/v1/traffic/runs"); } catch (e) { return fail(e); }
  const sel = $("#run");
  for (const r of runs) sel.append(el("option", { value: r.id, text: `${r.camera_name} · ${r.label}` }));
  const want = new URLSearchParams(location.search).get("run");
  if (want) sel.value = want;
  sel.addEventListener("change", load);
  $("#bin").addEventListener("change", drawFlow);
  $("#metric").addEventListener("change", drawLanes);
  $("#export").addEventListener("change", (ev) => { if (ev.target.value) download("/api/v1/traffic/export.csv" + query({ run_id: sel.value, what: ev.target.value })); ev.target.value = ""; });
  const clock = (sec) => { const o = new Date(run.clock_origin.slice(0, 19) + "Z"); o.setUTCSeconds(o.getUTCSeconds() + Math.round(sec)); return o.toISOString().slice(11, 19); };

  async function load() {
    try { summary = await get("/api/v1/traffic/summary", { run_id: sel.value }); } catch (e) { return fail(e); }
    run = summary.run;
    if (run.video_url) { if (!vp.video.isConnected) vp = player($("#player"), "Video of the run"); vp.load(run.video_url, 0, "", false); }
    else clear($("#player")).append(el("div", { class: "card muted", text: run.camera_kind === "anpr" || run.camera_kind === "gate"
      ? "This plate camera's video shows full plates, so it is not shown to roles that see masked plates." : "No video for this run." }));
    const k = clear($("#kpis"));
    const tile = (label, value, sub) => el("div", { class: "card tile" }, el("div", { class: "label", text: label }), el("div", { class: "value", text: value }), sub ? el("div", { class: "sub", text: sub }) : null);
    k.append(tile("Vehicles counted", num(summary.total), `${dur(summary.duration_sec)} of footage (counting line)`),
      tile("Flow", `${num(summary.veh_per_min, 1)} veh/min`, "all lanes, whole run"));
    for (const [g, v] of Object.entries(summary.groups).slice(0, 2))
      k.append(tile(`${g}`, v.speed_kmh ? `${num(v.speed_kmh)} km/h` : pct(v.occupancy), `${num(v.count)} vehicles · occupancy ${pct(v.occupancy)}`));
    if (!Object.keys(summary.groups).length) k.append(tile("By type", Object.entries(summary.by_type).map(([t, n]) => `${n} ${t}`).join(" · ") || "–"), tile("Camera", run.camera_name, run.label));
    drawFlow(); drawLanes(); drawTable(); drawEvents();
  }

  async function drawFlow() {
    let s;
    try { s = await get("/api/v1/traffic/flow", { run_id: sel.value, by: "type", bin: $("#bin").value }); } catch (e) { return fail(e); }
    const colours = Charts.SERIES();
    const series = TYPES.filter((t) => s.series[t]).map((t) => ({ name: t, values: s.series[t], color: colours[TYPES.indexOf(t)] }));
    Charts.bars($("#flow"), { x: s.x, xLabel: "time", xFormat: clock, series, height: 210, title: "vehicles per time bin by type",
      onClick: (i) => vp.load(run.video_url, Math.max(0, s.x[i] - run.video_offset_sec), clock(s.x[i])) });
  }

  async function drawLanes() {
    const box = clear($("#lanecharts"));
    if (!summary.lanes.length) { $("#lanecard").style.display = "none"; return; }
    $("#lanecard").style.display = "";
    let s;
    try { s = await get("/api/v1/traffic/lanes/series", { run_id: sel.value, metric: $("#metric").value }); } catch (e) { return fail(e); }
    const colours = Charts.SERIES(), m = $("#metric").value;
    const fmt = m === "occupancy" ? (v) => `${Math.round(v * 100)} %` : (v) => num(v, m === "density" ? 0 : 1);
    const all = Object.values(s.series).flat().filter((v) => v !== null);
    const yMax = Math.max(...all, m === "occupancy" ? 1 : 1);
    for (const g of [...new Set(summary.lanes.map((l) => l.group))]) {
      const lanes = summary.lanes.filter((l) => l.group === g);
      const c = el("div");
      box.append(el("div", {}, el("h3", { text: `${g} · ${s.unit}` }), c));
      Charts.line(c, { x: s.x, xLabel: "time", xFormat: clock, yFormat: fmt, yMin: 0, yMax, height: 190, title: `${m} per lane, ${g}`,
        series: lanes.map((l, i) => ({ name: l.name, values: s.series[l.name], color: colours[i % 4] })) });
    }
  }

  function drawTable() {
    const box = clear($("#lanes"));
    box.append(el("h2", { text: "Per lane" }));
    if (!summary.lanes.length) { box.append(el("p", { class: "muted", text: "This camera counts at one counting line (no lanes configured)." }),
      el("div", { class: "row" }, ...Object.entries(summary.by_type).map(([t, n]) => pill(`${n} ${t}`, "gray", false)))); return; }
    box.append(el("div", { class: "scroll" }, el("table", { class: "t" },
      el("thead", {}, el("tr", {}, ...["Lane", "Dir", "Count", "Veh/min", "Occupancy", "Density", "Speed"].map((h, i) => el("th", { class: i > 1 ? "num" : "", text: h })))),
      el("tbody", {}, ...summary.lanes.map((l) => el("tr", {}, el("td", { text: l.name }), el("td", { text: l.direction === "down" ? "↓ in" : "↑ out" }),
        el("td", { class: "num", text: num(l.count) }), el("td", { class: "num", text: num(l.veh_per_min, 1) }), el("td", { class: "num", text: pct(l.occupancy) }),
        el("td", { class: "num", text: `${num(l.density)} /km` }), el("td", { class: "num", text: l.mean_speed_kmh ? `${num(l.mean_speed_kmh)} ± ${num(l.speed_error_kmh, 1)}` : "–" })))))));
    if (summary.outside_lanes) box.append(el("p", { class: "faint", style: { fontSize: "12px", marginBottom: 0 }, text: `${summary.outside_lanes} of ${num(summary.total)} crossings were outside every lane (e.g. the lay-by): counted in the total, not in a lane.` }));
    if (summary.speed_note) box.append(el("p", { class: "faint", style: { fontSize: "12px", marginBottom: 0 }, text: summary.speed_note + " (km/h)." }));
  }

  async function drawEvents() {
    const box = clear($("#events"));
    box.append(el("h2", { text: "Lane events" }));
    let evs;
    try { evs = await get("/api/v1/traffic/events", { run_id: sel.value }); } catch (e) { return fail(e); }
    if (!evs.length) box.append(el("div", { class: "empty", text: "No lane events in this run." }));
    for (const e of evs) box.append(el("div", { class: "row", style: { padding: "6px 0", borderTop: "1px solid var(--line)", fontSize: "13px" } },
      pill(e.title, EVENT_COLOUR[e.severity] || "blue"), el("span", { class: "num faint", text: time(e.seen_at) }), el("span", { text: e.lane + (e.from_lane ? ` (from ${e.from_lane})` : "") }),
      e.plate ? plate(e.plate) : el("span", { class: "muted", text: e.vtype || "" }), provPill(e.provenance),
      e.video ? el("button", { class: "btn small", onclick: () => vp.load(e.video.url, e.video.seek_sec, `${e.title} · ${time(e.seen_at)}`) }, "▶") : null));
    const w = summary.withheld;
    if (Object.keys(w).length) box.append(el("p", { class: "notice warn", style: { marginBottom: 0 }, text:
      `Withheld (real vehicles, public footage): ${Object.entries(w).map(([k, n]) => `${n} ${k.toLowerCase()}`).join(", ")} - counted, not shown and not linked to a vehicle. Violations above are synthetic vehicles with fictitious plates.` }));
  }
  load();
})();
