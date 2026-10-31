/* Police watch list and blacklist (on top of the SIMULATED registry). */
(() => {
  const { el, clear, $, get, post, pill, plate, dt, fail, toast, formDialog } = App;
  let lst = "watch";
  $("#tabs").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    lst = b.dataset.l; $("#tabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); load();
  });
  async function load() {
    const pane = clear($("#pane"));
    let rows;
    try { rows = await get(`/api/v1/lists/${lst}`); } catch (e) { return fail(e); }
    pane.append(el("div", { class: "row between" }, el("h2", { text: lst === "watch" ? "Watch list (vehicles of interest)" : "Blacklist" }),
      el("button", { class: "btn primary", onclick: add }, "+ Add a plate")));
    if (!rows.length) { pane.append(el("div", { class: "empty", text: "Empty." })); return; }
    pane.append(el("div", { class: "scroll" }, el("table", { class: "t" },
      el("thead", {}, el("tr", {}, ...["Plate", "Reason", "Until", "Added", "By", "Past sightings", "Registry (SIMULATED)", "Status", ""].map((h) => el("th", { text: h })))),
      el("tbody", {}, ...rows.map((e) => el("tr", {}, el("td", {}, plate(e.plate)), el("td", { text: e.reason }), el("td", { text: e.valid_to ? dt(e.valid_to) : "–" }),
        el("td", { class: "num", text: dt(e.created_at) }), el("td", { class: "muted", text: e.created_by }),
        el("td", { class: "num" }, e.past_sightings ? el("a", { href: `/plates?plate=${encodeURIComponent(e.plate)}`, text: String(e.past_sightings) }) : "0"),
        el("td", { class: "muted", text: e.registry_status || "–" }),
        el("td", {}, e.active ? pill("Active", lst === "watch" ? "orange" : "red", false) : pill("Removed", "gray", false)),
        el("td", {}, e.simulated ? pill("Simulated", "purple") : null, e.active ? el("button", { class: "btn small", onclick: () => remove(e) }, "Remove") : null)))))));
  }
  async function add() {
    const v = await formDialog(`Add to the ${lst === "watch" ? "watch list" : "blacklist"}`, [
      { name: "plate", label: "Plate", required: true }, { name: "reason", label: "Reason (required; recorded in the audit log)", type: "textarea", required: true },
      { name: "valid_to", label: "Until (optional)", type: "datetime-local" }], "Add");
    if (!v) return;
    try { await post(`/api/v1/lists/${lst}`, { ...v, valid_to: v.valid_to ? v.valid_to + ":00.000" : null }); toast("Added"); load(); } catch (e) { fail(e); }
  }
  async function remove(e) {
    const v = await formDialog(`Remove ${e.plate}`, [{ name: "reason", label: "Reason", type: "textarea", required: true }], "Remove");
    if (!v) return;
    try { await post(`/api/v1/lists/entries/${e.id}/deactivate`, v); toast("Removed"); load(); } catch (er) { fail(er); }
  }
  load();
})();
