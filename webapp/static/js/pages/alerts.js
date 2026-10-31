/* Alert centre: alerts sorted by severity, the workflow new -> acknowledged -> resolved / false alarm (with notes and,
   for a false alarm, the correct plate for the OCR correction pool), registry comparison, evidence packs. */
(() => {
  const { el, clear, $, get, post, pill, plate, sevPill, statusPill, provPill, dt, time, fail, toast, formDialog, player, S } = App;
  const CODES = { BLACKLIST: "Blacklisted", DUPLICATE: "Cloned plate", WRONG_WAY: "Wrong way", SPEEDING: "Speeding", SOLID_LINE: "Crossed a solid line",
    TAILGATING: "Tailgating", STOPPED: "Stopped in a live lane", EXPIRED: "Road tax expired", UNMATCHED: "Not registered",
    TYPE_MISMATCH: "Type mismatch", ILLEGAL_FORMAT: "Illegal format", WATCHLIST: "Watch-list hit", BANNED_AT_SITE: "Banned at this site" };
  for (const [k, v] of Object.entries(CODES)) $("#code").append(el("option", { value: k, text: v }));
  const vp = player($("#player"), "Video of the alert");
  let status = "open", current = null;
  const police = S.user.role === "police", guard = S.user.role === "guard";
  if (S.user.masked) clear($("#player")).append(el("div", { class: "card muted", text: "Videos of plate cameras show the full plates, so they are not shown to roles that see masked plates." }));

  $("#tabs").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    status = b.dataset.s; $("#tabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); load();
  });
  for (const id of ["#severity", "#code", "#sim"]) $(id).addEventListener("change", load);

  async function load() {
    let res, counts;
    try {
      [res, counts] = await Promise.all([get("/api/v1/alerts", { status, severity: $("#severity").value, code: $("#code").value,
        include_simulated: $("#sim").checked, page_size: 200 }), get("/api/v1/alerts/counts")]);
    } catch (e) { return fail(e); }
    $("#counts").textContent = `${counts.total_open} open · ${counts.simulated} of them simulated`;
    const list = clear($("#list"));
    if (!res.items.length) { list.append(el("div", { class: "empty", text: "No alerts." })); return; }
    const tb = el("tbody");
    for (const a of res.items) {
      const tr = el("tr", { class: "click" + (current === a.id ? " sel" : ""), onclick: () => select(a.id, tr) },
        el("td", {}, sevPill(a.rank)),
        el("td", {}, el("b", { text: a.title }), el("div", { class: "muted", style: { fontSize: "12px", maxWidth: "340px" }, text: a.message })),
        el("td", {}, a.plate ? plate(a.plate, a.masked) : el("span", { class: "faint", text: "no plate" })),
        el("td", {}, el("div", { text: a.camera_name + (a.lane ? ` · ${a.lane}` : "") }), el("div", { class: "faint num", style: { fontSize: "12px" }, text: dt(a.seen_at) })),
        el("td", {}, el("div", { class: "stack", style: { gap: "4px" } }, statusPill(a.status), provPill(a.provenance))));
      tb.append(tr);
    }
    list.append(el("div", { class: "scroll" }, el("table", { class: "t" }, el("thead", {}, el("tr", {}, ...["Severity", "Alert", "Plate", "Camera · time", "Status"].map((h) => el("th", { text: h })))), tb)),
      el("div", { class: "muted", style: { padding: "10px 14px" }, text: `${res.total} alert${res.total === 1 ? "" : "s"} · sorted by severity (high and lane violations, then hazards, then medium), newest first` }));
    const want = +location.hash.slice(1);
    if (want && !current) { const a = res.items.find((x) => x.id === want); if (a) select(a.id); }
  }

  async function select(id, tr) {
    current = id;
    history.replaceState(null, "", `#${id}`);
    document.querySelectorAll("#list tr.sel").forEach((x) => x.classList.remove("sel"));
    if (tr) tr.classList.add("sel");
    let a;
    try { a = await get(`/api/v1/alerts/${id}`); } catch (e) { return fail(e); }
    if (a.video) vp.load(a.video.url, a.video.seek_sec, `${a.title} · ${time(a.seen_at)}`);
    const d = clear($("#detail"));
    d.append(el("div", { class: "row between" }, el("div", { class: "row" }, sevPill(a.rank), el("h2", { style: { margin: 0 }, text: a.title })), statusPill(a.status)),
      el("p", { style: { margin: "8px 0" }, text: a.message }));
    if (a.provenance !== "camera") d.append(el("p", { class: "notice", style: { marginTop: 0 }, text: `${a.provenance_label}. Fictitious plate: this is not an alert about a real vehicle.` }));
    if (a.note) d.append(el("p", { class: "notice warn", text: a.note }));
    const kv = el("dl", { class: "kv" });
    for (const [k, v] of [["Plate", a.plate], ["Seen", dt(a.seen_at)], ["Camera", a.camera_name + (a.lane ? ` · lane ${a.lane}` : "")], ["Run", a.run_label], ["Source", a.source]])
      if (v) kv.append(el("dt", { text: k }), el("dd", { text: v }));
    if (a.speed_text) kv.append(el("dt", { text: "Measured" }), el("dd", { text: a.speed_text }));
    d.append(kv);
    const imgs = el("div", { class: "row", style: { marginTop: "10px", alignItems: "flex-start" } });
    if (a.crop_url) imgs.append(el("img", { src: a.crop_url, alt: "plate crop", style: { height: "56px", borderRadius: "6px", border: "1px solid var(--line)", background: "#000" } }));
    if (a.scene_url) imgs.append(el("a", { href: a.scene_url, target: "_blank", rel: "noopener" }, el("img", { src: a.scene_url, alt: "scene frame", style: { height: "90px", borderRadius: "6px", border: "1px solid var(--line)" } })));
    if (imgs.childNodes.length) d.append(imgs);
    if (a.comparison.length) {
      d.append(el("h3", { style: { marginTop: "14px" }, text: "Registry (SIMULATED) vs observed" }),
        el("table", { class: "t" }, el("thead", {}, el("tr", {}, ...["", "Registered", "Observed", ""].map((h) => el("th", { text: h })))),
          el("tbody", {}, ...a.comparison.map((c) => el("tr", {}, el("td", { class: "muted", text: c.field }), el("td", { text: c.registered }), el("td", { text: c.observed }),
            el("td", {}, c.match === null ? null : pill(c.match ? "match" : "differs", c.match ? "green" : "red", false)))))));
    }
    const actions = el("div", { class: "row", style: { marginTop: "14px" } });
    const LABEL = { acknowledged: "Acknowledge", resolved: "Resolve…", false_alarm: "False alarm…" };
    for (const to of a.allowed_transitions) {
      const reopen = ["resolved", "false_alarm"].includes(a.status);
      actions.append(el("button", { class: "btn" + (to === "acknowledged" && !reopen ? " primary" : ""), onclick: () => move(a, to) }, reopen ? "Reopen" : LABEL[to]));
    }
    if (a.can_export) actions.append(
      el("a", { class: "btn", href: `/api/v1/alerts/${a.id}/evidence?format=html`, target: "_blank", rel: "noopener" }, "Evidence pack · HTML"),
      el("a", { class: "btn", href: `/api/v1/alerts/${a.id}/evidence?format=pdf`, target: "_blank", rel: "noopener" }, "PDF"));
    if (police && a.plate && !a.masked) actions.append(el("a", { class: "btn", href: `/timeline/${encodeURIComponent(a.plate)}` }, "Timeline ›"));
    if (!police && !guard) actions.append(el("span", { class: "muted", text: "Read-only for your role." }));
    d.append(actions);
    d.append(el("h3", { style: { marginTop: "16px" }, text: "History" }),
      ...a.history.map((h) => el("div", { class: "row", style: { fontSize: "12px", padding: "3px 0", borderTop: "1px solid var(--line)" } },
        el("span", { class: "faint num", text: dt(h.at) }), el("b", { text: h.action.replace("_", " ") }), el("span", { class: "muted", text: h.user }),
        h.note ? el("span", { text: `“${h.note}”` }) : null, h.corrected_plate ? pill(`correct plate ${h.corrected_plate}`, "blue", false) : null)));
  }

  async function move(a, to) {
    let body = { to, note: "" };
    if (to === "resolved" || to === "false_alarm" || ["resolved", "false_alarm"].includes(a.status)) {
      const fields = [{ name: "note", label: "Note (who checked what; required to close)", type: "textarea", required: to !== "acknowledged" }];
      if (to === "false_alarm" && a.plate && !a.masked) fields.push({ name: "corrected_plate", label: "Correct plate, if the OCR misread it (goes to the correction pool for OCR fine-tuning; retention applies)", placeholder: "optional" });
      const v = await formDialog(to === "false_alarm" ? "Mark as false alarm" : to === "resolved" ? "Resolve alert" : "Reopen alert", fields, "Save");
      if (!v) return;
      body = { ...body, ...v };
    }
    try { await post(`/api/v1/alerts/${a.id}/transition`, body); toast("Saved"); await load(); await select(a.id); } catch (e) { fail(e); }
  }

  window.addEventListener("hashchange", () => { const id = +location.hash.slice(1); if (id && id !== current) select(id); });
  load();
})();
