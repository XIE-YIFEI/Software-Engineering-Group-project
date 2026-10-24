/* Plate search: fuzzy search with filters and a purpose, results with crops and alerts, click-to-timestamp playback,
   one record's details, "Show full plate" for masked roles, CSV export. */
(() => {
  const { el, clear, $, get, query, pill, plate, sevPill, provPill, readingPill, dt, time, num, fail, toast, reveal, player, download, S } = App;
  const form = $("#search"), results = $("#results"), detail = $("#detail");
  const vp = player($("#player"), "Video of the run");
  const masked = S.user.masked;
  let tol = "1", last = null, selected = null;

  if (masked) {
    clear($("#player")).append(el("div", { class: "card muted", text: "Videos of plate cameras show the full plates, so they are not shown to roles that see masked plates." }));
    $("#masknote").textContent = "Plates are masked for your role. Searching by a plate number reveals that plate and asks for a reason (exact match only).";
    $("#tol").querySelectorAll("button").forEach((b) => { b.disabled = b.dataset.v !== "0"; b.classList.toggle("on", b.dataset.v === "0"); });
    tol = "0";
  }
  $("#tol").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b || b.disabled) return;
    tol = b.dataset.v; $("#tol").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  });
  get("/api/v1/cameras").then((cams) => {
    const sel = form.camera_id;
    for (const c of cams.filter((c) => c.kind !== "congestion")) sel.append(el("option", { value: c.id, text: c.name }));
    const p = new URLSearchParams(location.search);
    if (p.get("camera_id")) sel.value = p.get("camera_id");
  }).catch(fail);

  const params = new URLSearchParams(location.search);
  if (params.get("plate")) form.plate.value = params.get("plate");
  const runFilter = params.get("run_id");
  try { const pp = localStorage.getItem("secs.purpose"); if (pp) form.purpose.value = pp; } catch (_) { /* storage off */ }

  function filters() {
    const f = Object.fromEntries(new FormData(form).entries());
    f.tolerance = tol;
    if (f.start) f.start = f.start.length === 16 ? f.start + ":00" : f.start;
    if (f.end) f.end = f.end.length === 16 ? f.end + ":59" : f.end;
    if (runFilter) f.run_id = runFilter;
    return f;
  }

  async function search(page = 1) {
    const f = filters();
    if (!f.purpose) { toast("Choose the purpose of the search first: it is recorded in the audit log.", true); form.purpose.focus(); return; }
    try { localStorage.setItem("secs.purpose", f.purpose); } catch (_) { /* storage off */ }
    if (masked && f.plate && !f.plate.includes("*") && !f.plate.includes("?")) {
      const v = await App.formDialog("Search by plate number", [
        { name: "reveal_reason", label: "Reason (recorded in the audit log)", type: "select", options: S.revealReasons, required: true }],
        "Search", "Your role sees masked plates. Searching by a plate number reveals that plate for 15 minutes.");
      if (!v) return;
      f.reveal_reason = v.reveal_reason;
    }
    f.page = page; f.page_size = 50;
    let res;
    try { res = await get("/api/v1/sightings", f); } catch (e) { return fail(e); }
    last = f; render(res);
  }

  function render(res) {
    clear(results);
    if (!res.items.length) { results.append(el("div", { class: "empty", text: "No sightings match." })); return; }
    const tb = el("tbody");
    for (const s of res.items) {
      const tr = el("tr", { class: "click" + (selected === s.id ? " sel" : ""), onclick: () => open(s, tr) },
        el("td", {}, s.crop_url ? el("img", { class: "thumb", src: s.crop_url, alt: "plate crop", loading: "lazy" }) : el("span", { class: "thumb none", text: "no crop" })),
        el("td", {}, el("div", { class: "row", style: { gap: "6px" } }, plate(s.plate, s.masked), s.distance ? pill(`≈ ${s.distance}`, "gray", false) : null),
          el("div", { class: "row", style: { gap: "4px", marginTop: "4px" } }, readingPill(s), provPill(s.provenance))),
        el("td", { text: s.vtype || "–" }),
        el("td", { style: { minWidth: "180px" } }, el("div", { text: s.camera_name }), el("div", { class: "faint", style: { fontSize: "12px" }, text: s.run_label })),
        el("td", { class: "num", style: { whiteSpace: "nowrap" } }, el("div", { text: dt(s.seen_at) }), el("div", { class: "faint", style: { fontSize: "12px" }, text: `to ${time(s.last_seen_at)}` })),
        el("td", {}, el("div", { class: "stack", style: { gap: "4px" } }, ...s.alerts.map((a) => el("span", { class: "row", style: { gap: "6px" } }, sevPill(a.rank), el("span", { text: a.title }))))),
        el("td", {}, s.video ? el("button", { class: "btn small", type: "button", title: "Play one second before this record",
          onclick: (ev) => { ev.stopPropagation(); vp.load(s.video.url, s.video.seek_sec, `${s.plate} · ${time(s.seen_at)}`); } }, `▶ ${App.dur(s.video.seek_sec)}`) : null));
      tb.append(tr);
    }
    const pages = Math.ceil(res.total / res.page_size);
    results.append(el("div", { class: "scroll" }, el("table", { class: "t" },
      el("thead", {}, el("tr", {}, ...["Crop", "Plate", "Type", "Camera", "Seen", "Alerts", "Video"].map((h) => el("th", { text: h })))), tb)),
      el("div", { class: "row between", style: { padding: "10px 14px" } }, el("span", { class: "muted", text: `${num(res.total)} sighting${res.total === 1 ? "" : "s"}` }),
        el("span", { class: "row" }, res.page > 1 ? el("button", { class: "btn small", onclick: () => search(res.page - 1) }, "‹ Previous") : null,
          el("span", { class: "muted", text: `page ${res.page} of ${Math.max(1, pages)}` }), res.page < pages ? el("button", { class: "btn small", onclick: () => search(res.page + 1) }, "Next ›") : null)));
  }

  async function open(s, tr) {
    selected = s.id;
    results.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel"));
    if (tr) tr.classList.add("sel");
    if (s.video) vp.load(s.video.url, s.video.seek_sec, `${s.plate} · ${time(s.seen_at)}`);
    let d;
    try { d = await get(`/api/v1/sightings/${s.id}`, { purpose: last ? last.purpose : "" }); } catch (e) { return fail(e); }
    clear(detail);
    detail.append(el("div", { class: "row between" }, el("div", { class: "row" }, plate(d.plate, d.masked), readingPill(d), provPill(d.provenance)),
      d.can_reveal ? el("button", { class: "btn small", onclick: async () => {
        try { const r = await reveal(d.id); if (r) { toast(`Revealed until ${time(r.expires_at)} (logged)`); open(s, tr); search(last.page); } } catch (e) { fail(e); } } }, "Show full plate") : null));
    if (d.provenance !== "camera") detail.append(el("p", { class: "notice", style: { margin: "10px 0 0" }, text: d.provenance_label + "." }));
    const kv = el("dl", { class: "kv", style: { marginTop: "12px" } });
    const add = (k, v) => { if (v !== null && v !== undefined && v !== "") kv.append(el("dt", { text: k }), el("dd", { text: String(v) })); };
    add("Seen", `${dt(d.seen_at)} – ${time(d.last_seen_at)}`);
    add("Camera", d.camera_name); add("Run", d.run_label); add("Vehicle type", d.vtype); add("State / series", d.region);
    add("Reading", d.status === "TENTATIVE" ? "provisional (not confirmed, raises no alert)" : "confirmed");
    if (d.confidence !== null) add("OCR confidence", `${Math.round(d.confidence * 100)} % over ${d.votes} readings`);
    if (d.track_ids.length) add("Track ids", d.track_ids.join(", "));
    add("Other sightings of this plate", d.other_sightings);
    detail.append(kv);
    if (d.alerts.length) detail.append(el("h3", { style: { marginTop: "14px" }, text: "Alerts" }),
      ...d.alerts.map((a) => el("a", { href: `/alerts#${a.id}`, class: "row", style: { padding: "4px 0", color: "inherit" } }, sevPill(a.rank), el("b", { text: a.title }), el("span", { class: "muted", text: a.message }))));
    if (S.user.role === "police") {
      detail.append(el("h3", { style: { marginTop: "14px" }, text: `Registry · ${d.registry_name}` }));
      if (d.registry) {
        const r = d.registry, k2 = el("dl", { class: "kv" });
        for (const [k, v] of [["Body type", r.body_type], ["Colour", r.colour], ["Make / model", r.make_model], ["Road tax to", r.road_tax_expiry], ["Status", r.status], ["Note", r.note]])
          if (v) k2.append(el("dt", { text: k }), el("dd", { text: v }));
        detail.append(k2, el("p", { class: "faint", style: { fontSize: "12px" }, text: r.simulated ? "SIMULATED record (fictitious vehicle)." : "Neutral placeholder for a real plate of the demo footage: no dates, no type." }));
      } else detail.append(el("p", { class: "muted", text: d.registry_note }));
      detail.append(el("div", { class: "row", style: { marginTop: "10px" } }, el("a", { class: "btn small", href: `/timeline/${encodeURIComponent(d.plate)}` }, "Timeline across cameras ›")));
    }
    if (d.scene_url) detail.append(el("h3", { style: { marginTop: "14px" }, text: "Scene frame" }),
      el("a", { href: d.scene_url, target: "_blank", rel: "noopener" }, el("img", { src: d.scene_url, alt: "scene frame", style: { width: "100%", borderRadius: "8px", border: "1px solid var(--line)" } })));
  }

  form.addEventListener("submit", (ev) => { ev.preventDefault(); search(1); });
  $("#csv").addEventListener("click", async () => {
    const f = filters();
    if (!f.purpose) { toast("Choose the purpose first (recorded in the audit log).", true); return; }
    if (masked && f.plate) { toast("Export by plate number is not available to masked roles; export without a plate filter.", true); return; }
    download("/api/v1/sightings/export.csv" + query(f));
  });
  if (form.purpose.value && (params.get("plate") || runFilter)) search(1);
})();
