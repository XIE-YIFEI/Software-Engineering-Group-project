/* Overview: camera status, the day's flow, open alerts, current congestion (and system status for admins). */
(async () => {
  const { el, clear, $, get, pill, plate, sevPill, provPill, pct, num, day, time, hm, fail } = App;
  const KIND = { anpr: "Plate camera", gate: "Gate camera", lane: "Lane camera", congestion: "Congestion camera" };
  let o;
  const wanted = new URLSearchParams(location.search).get("date") || undefined;
  try { o = await get("/api/v1/overview", { date: wanted }); } catch (e) { return fail(e); }

  const cams = clear($("#cameras"));
  for (const c of o.cameras) {
    const stats = c.kind === "congestion"
      ? [`${c.runs} recorded day${c.runs === 1 ? "" : "s"}`, c.last_seen ? `last minute ${day(c.last_seen)} ${hm(c.last_seen)}` : ""]
      : [`${num(c.sightings)} plates` + (c.synthetic ? ` (+${c.synthetic} simulated)` : ""),
         c.flow_total !== null ? `${num(c.flow_total)} vehicles counted` : "", c.open_alerts ? `${c.open_alerts} open alerts` : ""];
    cams.append(el("div", { class: "card tile" },
      el("div", { class: "row between" }, el("span", { class: "label", text: KIND[c.kind] || c.kind }), pill(c.status === "replay" ? "Replay" : c.status, c.status === "live" ? "green" : "gray")),
      el("div", { style: { fontWeight: 600, margin: "6px 0 4px" }, text: c.name }),
      ...stats.filter(Boolean).map((s) => el("div", { class: "sub", text: s })),
      el("div", { class: "faint", style: { fontSize: "11px", marginTop: "6px" }, text: c.notional_clock ? "Recorded public footage · notional clock" : "Recorded public footage · real clock" })));
  }

  const flow = clear($("#flow"));
  const pick = el("select", { onchange: (ev) => { location.search = `?date=${ev.target.value}`; } },
    ...o.dates.map((d) => el("option", { value: d, text: day(d), selected: d === o.date })));
  flow.append(el("div", { class: "row between" }, el("h2", { text: `Flow on ${day(o.date)}` + (o.demo_day ? " (demo day)" : "") }),
    o.dates.length > 1 ? pick : null),
    el("div", { class: "row", style: { alignItems: "baseline", gap: "12px" } },
      el("div", { style: { fontSize: "44px", fontWeight: 600, lineHeight: 1 }, text: num(o.flow_total) }), el("span", { class: "muted", text: "vehicles counted at the counting lines" })),
    el("div", { class: "row", style: { margin: "10px 0 14px", gap: "16px" } },
      ...Object.entries(o.flow_by_type).sort((a, b) => b[1] - a[1]).map(([k, v]) => el("span", { class: "muted" }, el("b", { class: "num", style: { color: "var(--text)" }, text: num(v) }), ` ${k}`))));
  for (const [cam, total] of Object.entries(o.flow_by_camera)) {
    const sp = el("span");
    flow.append(el("div", { class: "row between", style: { padding: "6px 0", borderTop: "1px solid var(--line)" } },
      el("span", { text: cam }), el("span", { class: "row" }, sp, el("b", { class: "num", text: num(total) }))));
    Charts.spark(sp, o.sparklines[cam] || [], { label: `vehicles per 10 s, ${cam}` });
  }
  flow.append(el("p", { class: "faint", style: { fontSize: "12px", marginBottom: 0 }, text: "Counted independently of the plates; synthetic vehicles are never counted. Sparklines: vehicles per 10 s." }));

  const al = clear($("#alerts"));
  const traffic = window.SECS.user.role === "traffic";
  al.append(el("div", { class: "row between" }, el("h2", { text: traffic ? "Lane hazards and violations" : "Open alerts" }),
    traffic ? el("a", { href: "/traffic", text: "Traffic analytics ›" }) : el("a", { href: "/alerts", text: "Alert centre ›" })),
    el("div", { class: "row", style: { marginBottom: "12px" } },
      pill(`${o.alerts.by_rank.High} high`, "red"), pill(`${o.alerts.by_rank.Hazard} hazard`, "orange"), pill(`${o.alerts.by_rank.Medium} medium`, "orange"),
      o.alerts.simulated ? pill(`${o.alerts.simulated} of ${o.alerts.total_open} simulated`, "purple") : null));
  if (!o.top_alerts.length) al.append(el("div", { class: "empty", text: traffic ? "Lane events are listed in Traffic analytics." : "No open alerts." }));
  for (const a of o.top_alerts) {
    al.append(el("a", { href: `/alerts#${a.id}`, class: "row", style: { padding: "7px 0", borderTop: "1px solid var(--line)", color: "inherit", textDecoration: "none" } },
      sevPill(a.rank), el("span", { style: { minWidth: "130px" }, text: a.title }), a.plate ? plate(a.plate, a.masked) : null,
      el("span", { class: "muted", text: `${a.camera_name} · ${time(a.seen_at)}` }), provPill(a.provenance)));
  }

  const cg = clear($("#congestion"));
  if (!o.congestion) { cg.append(el("h2", { text: "Congestion" }), el("div", { class: "empty", text: "No congestion camera." })); }
  else {
    const s = o.congestion.sections[0], d = o.congestion_day;
    cg.append(el("div", { class: "row between" }, el("h2", { text: `Congestion · ${d.camera} (replay of ${d.label}, ${o.congestion.at})` }), el("a", { href: "/forecast", text: "Open the forecast ›" })),
      el("div", { class: "row", style: { gap: "18px" } },
        pill(s.status, s.status_colour),
        el("span", { class: "muted" }, "Now ", el("b", { style: { color: "var(--text)" }, text: pct(s.now) }), ` of jam capacity · ${num(s.vehicles, 0)} vehicles in view`),
        ...["5", "15", "30"].map((h) => el("span", { class: "muted" }, `+${h} min `, el("b", { style: { color: "var(--text)" }, text: pct(s.forecast[h]) }), ` ±${Math.round((s.band[h] || 0) * 100)}`))),
      el("p", { class: "faint", style: { fontSize: "12px", marginBottom: 0 }, text: `${s.model}, trained on another day (${d.train_day}). Validated on 2 days of one road only.` }));
  }

  if (o.system) {
    const sy = $("#system");
    sy.style.display = "";
    sy.append(el("div", { class: "grid g4" }, ...Object.entries({ "Database": `${o.system.database_mb} MB`, "Last import": o.system.last_import ? `${day(o.system.last_import)} ${time(o.system.last_import)}` : "never",
      "Jobs queued or running": o.system.jobs_queued, "Audit-log rows": num(o.system.audit_rows) }).map(([k, v]) => el("div", { class: "tile" }, el("div", { class: "label", text: k }), el("div", { class: "value", style: { fontSize: "20px" }, text: String(v) })))));
  }
})();
