/* Residents and visitors of the guard's site: time-bounded visitor passes, who is inside (overstays first), the
   resident list and the site's ban list. */
(() => {
  const { el, clear, $, get, post, pill, plate, dt, hm, mins, fail, toast, formDialog } = App;
  let tab = "passes";
  $("#tabs").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    tab = b.dataset.t; $("#tabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); load();
  });
  const table = (heads, rows) => el("div", { class: "scroll" }, el("table", { class: "t" }, el("thead", {}, el("tr", {}, ...heads.map((h) => el("th", { text: h })))), el("tbody", {}, ...rows)));

  async function load() {
    const pane = clear($("#pane"));
    try {
      if (tab === "passes") {
        const rows = await get("/api/v1/passes");
        pane.append(el("div", { class: "row between" }, el("h2", { text: "Visitor passes" }), el("button", { class: "btn primary", onclick: issue }, "+ Issue a pass")));
        pane.append(rows.length ? table(["Plate", "Unit", "Label", "Valid from", "Valid to", "Status", ""], rows.map((p) => el("tr", {},
          el("td", {}, plate(p.plate)), el("td", { text: p.unit_no }), el("td", { text: p.visitor_label }), el("td", { class: "num", text: dt(p.valid_from) }),
          el("td", { class: "num", text: dt(p.valid_to) }),
          el("td", {}, p.status === "cancelled" ? pill("Cancelled", "gray", false) : p.valid_now ? pill("Valid now", "teal") : pill(p.valid_to < new Date(Date.now() + 8 * 3600e3).toISOString() ? "Ended" : "Not yet valid", "yellow", false)),
          el("td", {}, p.simulated ? pill("Simulated", "purple") : null, p.status === "active" && p.valid_to >= new Date(Date.now() + 8 * 3600e3).toISOString() ? el("button", { class: "btn small", onclick: async () => {
            try { await post(`/api/v1/passes/${p.id}/cancel`); toast("Pass cancelled"); load(); } catch (e) { fail(e); } } }, "Cancel") : null)))) : el("div", { class: "empty", text: "No passes." }));
      } else if (tab === "inside") {
        const rows = await get("/api/v1/gate/inside");
        pane.append(el("h2", { text: "Visitors inside now" }), el("p", { class: "muted", text: "Overstays first: inside longer than the configured number of hours, or past the end of their pass." }));
        pane.append(rows.length ? table(["", "Plate", "Unit", "Entered", "Inside", "Pass", "Note", ""], rows.map((i) => el("tr", { class: i.overstay ? "overstay" : "" },
          el("td", {}, i.overstay ? pill("Overstay", "orange") : null), el("td", {}, plate(i.plate)), el("td", { text: i.unit_no }), el("td", { class: "num", text: dt(i.entered_at) }),
          el("td", { text: mins(i.minutes_inside) }), el("td", { text: i.pass_valid_to ? (i.pass_expired ? `ended ${hm(i.pass_valid_to)}` : `to ${hm(i.pass_valid_to)}`) : "no pass" }),
          el("td", { class: "muted", text: i.note }),
          el("td", {}, i.simulated ? pill("Simulated", "purple") : null, el("button", { class: "btn small", onclick: async () => {
            try { await post("/api/v1/gate/decisions", { plate: i.plate, decision: "exited", note: "exit recorded by the guard" }); toast("Exit recorded"); load(); } catch (e) { fail(e); } } }, "Exited"))))) : el("div", { class: "empty", text: "Nobody inside." }));
      } else {
        const lst = tab, rows = await get(`/api/v1/lists/${lst}`);
        pane.append(el("div", { class: "row between" }, el("h2", { text: lst === "resident" ? "Residents" : "Banned at this site" }),
          el("button", { class: "btn primary", onclick: () => add(lst) }, lst === "resident" ? "+ Add a resident vehicle" : "+ Ban a plate")));
        pane.append(rows.length ? table(["Plate", lst === "resident" ? "Unit" : "Reason", "Label", "Added", "By", "Status", ""], rows.map((e) => el("tr", {},
          el("td", {}, plate(e.plate)), el("td", { text: lst === "resident" ? e.unit_no : e.reason }), el("td", { text: e.label }), el("td", { class: "num", text: dt(e.created_at) }),
          el("td", { class: "muted", text: e.created_by }), el("td", {}, e.active ? pill("Active", lst === "resident" ? "green" : "red", false) : pill("Removed", "gray", false)),
          el("td", {}, e.simulated ? pill("Simulated", "purple") : null, e.active ? el("button", { class: "btn small", onclick: async () => {
            try { await post(`/api/v1/lists/entries/${e.id}/deactivate`, { reason: "removed by the guard" }); toast("Removed"); load(); } catch (er) { fail(er); } } }, "Remove") : null)))) : el("div", { class: "empty", text: "Empty." }));
      }
    } catch (e) { fail(e); }
  }
  async function issue() {
    const v = await formDialog("Issue a visitor pass", [
      { name: "plate", label: "Plate", required: true }, { name: "unit_no", label: "Unit number", required: true },
      { name: "visitor_label", label: "Label (optional; no names or IC numbers)", placeholder: "Guest / Delivery / Contractor" },
      { name: "hours", label: "Valid for (hours, from now)", type: "number", value: 4, step: "0.5" }], "Issue");
    if (!v) return;
    try { await post("/api/v1/passes", { ...v, hours: +v.hours || 4 }); toast("Pass issued"); load(); } catch (e) { fail(e); }
  }
  async function add(lst) {
    const v = await formDialog(lst === "resident" ? "Add a resident vehicle" : "Ban a plate at this site", lst === "resident"
      ? [{ name: "plate", label: "Plate", required: true }, { name: "unit_no", label: "Unit number", required: true }, { name: "label", label: "Label (optional)" }]
      : [{ name: "plate", label: "Plate", required: true }, { name: "reason", label: "Reason (required)", type: "textarea", required: true }], "Add");
    if (!v) return;
    try { await post(`/api/v1/lists/${lst}`, v); toast("Added"); load(); } catch (e) { fail(e); }
  }
  load();
})();
