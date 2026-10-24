/* Guardhouse mode: the gate video on the left; on the right a big card with the latest plate and its identity colour
   (red banned, green resident, teal visitor with a valid pass, yellow unregistered visitor) and two big buttons,
   Allow (log entry) and Stop & note, both written to the access log. In the demo the recorded video is replayed and
   each plate's card appears when the video reaches it. */
(async () => {
  const { el, clear, $, get, post, pill, plate, provPill, time, hm, mins, fail, toast, formDialog, player } = App;
  const vp = player($("#player"), "Replay of recorded footage");
  const card = $("#card");
  let replay, shown = null, decided = new Set();
  const COLOUR_DOT = { red: "var(--red)", green: "var(--green)", teal: "var(--teal)", yellow: "var(--yellow)" };

  try { replay = await get("/api/v1/gate/replay"); } catch (e) { return fail(e); }
  $("#runnote").textContent = `${replay.label}: replay of recorded footage. ${replay.note}`;
  vp.load(replay.video_url, 0, "", false);
  vp.video.addEventListener("timeupdate", () => {
    const t = vp.video.currentTime;
    const due = replay.cards.filter((c) => c.video_time <= t + 0.05);
    const latest = due[due.length - 1];
    if (latest && latest !== shown) show(latest);
    if (!latest && shown && !shown.manual) { shown = null; clear(card).append(el("div", { class: "empty", text: "Waiting for a vehicle…" })); card.className = "idcard"; }
  });

  async function show(c) {
    shown = c;
    let ident = c.identity;
    try { ident = await get("/api/v1/gate/identify", { plate: c.plate }); } catch (_) { /* keep the replay's identity */ }
    if (shown !== c) return;
    card.className = `idcard ${ident.colour}`;
    clear(card);
    const done = decided.has(c.sighting_id || c.plate);
    card.append(...[
      el("div", { class: "row between" }, plate(c.plate, false, true), c.provenance !== "camera" ? provPill(c.provenance) : null),
      el("div", { class: "ident" }, el("span", { class: "dot", style: { background: COLOUR_DOT[ident.colour] } }), ident.title.toUpperCase()),
      el("div", { text: ident.detail }),
      el("div", { class: "muted", text: `${c.vtype || "Vehicle"} · entry lane · arrived ${time(new Date(Date.now() + 8 * 3600e3).toISOString())}` + (ident.simulated_list ? " · demo list (SIMULATED)" : "") }),
      c.provenance === "synthetic" ? el("div", { class: "notice", text: "Simulated arrival: there is no vehicle in the video at this moment (it shows the other identity colours)." }) : null,
      el("div", { class: "bigbtns" },
        el("button", { class: "bigbtn allow", disabled: done, onclick: () => decide(c, ident, "allowed") }, "ALLOW", el("small", { text: "log the entry" })),
        el("button", { class: "bigbtn stop", disabled: done, onclick: () => decide(c, ident, "stopped") }, "STOP & NOTE", el("small", { text: "refuse entry, add a note" }))),
      el("div", { class: "row" },
        ident.identity !== "resident" && ident.identity !== "banned" ? el("button", { class: "btn", onclick: () => issuePass(c) }, "+ Issue visitor pass") : null,
        done ? pill("Decision logged", "green", false) : null)].filter(Boolean));
    if (ident.colour === "red") toast(`${c.plate}: ${ident.title}. Do not allow entry.`, true);
  }

  async function decide(c, ident, decision) {
    let note = "";
    if (decision === "stopped" || ident.identity === "banned") {
      const v = await formDialog(decision === "stopped" ? "Stop & note" : "Allow a banned vehicle?", [{ name: "note", label: "Note (recorded in the access log)", type: "textarea", required: true }], "Save");
      if (!v) return;
      note = v.note;
    }
    try {
      await post("/api/v1/gate/decisions", { plate: c.plate, decision, note, sighting_id: c.sighting_id || null });
      decided.add(c.sighting_id || c.plate);
      toast(`${c.plate}: ${decision === "allowed" ? "entry logged" : "stopped, note logged"}`);
      show(c); refresh();
    } catch (e) { fail(e); }
  }

  async function issuePass(c) {
    const v = await formDialog(`Visitor pass for ${c.plate}`, [
      { name: "unit_no", label: "Unit number", required: true, placeholder: "e.g. B-07-3" },
      { name: "visitor_label", label: "Label (optional; no names or IC numbers)", placeholder: "Guest / Delivery / Contractor" },
      { name: "hours", label: "Valid for (hours, from now)", type: "number", value: 4, step: "0.5" }], "Issue pass");
    if (!v) return;
    try { await post("/api/v1/passes", { plate: c.plate, unit_no: v.unit_no, visitor_label: v.visitor_label, hours: +v.hours || 4 }); toast("Pass issued"); show(c); } catch (e) { fail(e); }
  }

  $("#manual").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const p = ev.target.plate.value.trim(); if (!p) return;
    show({ plate: p.toUpperCase(), vtype: null, provenance: "camera", sighting_id: null, manual: true, identity: null });
  });

  async function refresh() {
    try {
      const [inside, recent] = await Promise.all([get("/api/v1/gate/inside"), get("/api/v1/gate/access-log")]);
      const box = clear($("#inside"));
      const over = inside.filter((i) => i.overstay).length;
      $("#overstaynote").textContent = over ? `${over} overstay${over === 1 ? "" : "s"} first` : "no overstays";
      if (!inside.length) box.append(el("div", { class: "empty", text: "No visitors inside." }));
      else box.append(el("table", { class: "t" }, el("tbody", {}, ...inside.map((i) => el("tr", { class: i.overstay ? "overstay" : "" },
        el("td", {}, i.overstay ? pill("Overstay", "orange") : null), el("td", {}, plate(i.plate)),
        el("td", { text: i.unit_no ? `unit ${i.unit_no}` : "" }),
        el("td", { class: "muted", text: `in ${mins(i.minutes_inside)}` + (i.pass_valid_to ? (i.pass_expired ? ` · pass ended ${hm(i.pass_valid_to)}` : ` · pass to ${hm(i.pass_valid_to)}`) : " · no pass") }),
        el("td", {}, i.simulated ? pill("Simulated", "purple") : null),
        el("td", {}, el("button", { class: "btn small", onclick: async () => {
          try { await post("/api/v1/gate/decisions", { plate: i.plate, decision: "exited", note: "exit recorded by the guard" }); toast(`${i.plate} exited`); refresh(); } catch (e) { fail(e); } } }, "Exited")))))));
      const rb = clear($("#recent"));
      for (const e of recent.slice(0, 8)) rb.append(el("div", { class: "row", style: { padding: "5px 0", borderTop: "1px solid var(--line)", fontSize: "13px" } },
        el("span", { class: "faint num", text: hm(e.at) }), plate(e.plate), el("span", { text: e.decision }), el("span", { class: "muted", text: e.identity.replace("_", " ") }),
        e.simulated ? pill("Simulated", "purple") : null, e.note ? el("span", { class: "muted", text: `“${e.note}”` }) : null));
    } catch (e) { fail(e); }
  }
  refresh();
  setInterval(refresh, 30000);
})();
