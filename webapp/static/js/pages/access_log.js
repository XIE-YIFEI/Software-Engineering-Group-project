/* Access log of the guard's site: every Allow / Stop / Exited decision, searchable, CSV export. */
(() => {
  const { el, clear, $, get, query, pill, plate, dt, fail, download } = App;
  const f = $("#f");
  const ID = { resident: ["Resident", "green"], visitor_pre: ["Pre-registered visitor", "teal"], visitor_unregistered: ["Unregistered visitor", "yellow"], banned: ["Banned", "red"] };
  const DEC = { allowed: ["Allowed", "green"], stopped: ["Stopped", "red"], exited: ["Exited", "gray"] };
  const params = () => Object.fromEntries(new FormData(f).entries());
  async function load() {
    let rows;
    try { rows = await get("/api/v1/gate/access-log", params()); } catch (e) { return fail(e); }
    const box = clear($("#log"));
    if (!rows.length) { box.append(el("div", { class: "empty", text: "No entries." })); return; }
    box.append(el("div", { class: "scroll" }, el("table", { class: "t" },
      el("thead", {}, el("tr", {}, ...["Time", "Plate", "Decision", "Identity", "Direction", "Unit", "Guard", "Note", ""].map((h) => el("th", { text: h })))),
      el("tbody", {}, ...rows.map((e) => el("tr", {}, el("td", { class: "num", text: dt(e.at) }), el("td", {}, plate(e.plate)),
        el("td", {}, pill(...(DEC[e.decision] || [e.decision, "gray"]))), el("td", {}, pill(...(ID[e.identity] || [e.identity, "gray"]))),
        el("td", { text: e.direction }), el("td", { text: e.unit_no }), el("td", { class: "muted", text: e.guard }), el("td", { text: e.note }),
        el("td", {}, e.simulated ? pill("Simulated", "purple") : null)))))));
  }
  f.addEventListener("submit", (ev) => { ev.preventDefault(); load(); });
  $("#csv").addEventListener("click", () => download("/api/v1/gate/access-log.csv" + query(params())));
  load();
})();
