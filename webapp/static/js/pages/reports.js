/* Daily / weekly reports: preview here, download as CSV or PDF. */
(() => {
  const { el, clear, $, get, query, fail, download, S } = App;
  const f = $("#f");
  const KINDS = { police: [["alerts", "Alerts"], ["traffic", "Traffic"]], traffic: [["traffic", "Traffic"]], guard: [["gate", "Gate"]],
                  admin: [["traffic", "Traffic"], ["alerts", "Alerts"]] }[S.user.role] || [];
  for (const [k, v] of KINDS) f.kind.append(el("option", { value: k, text: v }));
  f.date.value = "2026-10-14";
  if (S.user.role === "guard") f.date.value = new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10);
  const params = () => { const p = Object.fromEntries(new FormData(f).entries()); const span = p.span; delete p.span; return [span, p]; };
  f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const [span, p] = params();
    let rep;
    try { rep = await get(`/api/v1/reports/${span}`, p); } catch (e) { return fail(e); }
    const out = clear($("#out"));
    out.append(el("h2", { text: `${rep.title} · ${rep.period}` }), el("p", { class: "faint", style: { fontSize: "12px" }, text: "Course project demo - not an official document." }));
    for (const s of rep.sections) {
      out.append(el("h3", { style: { marginTop: "16px" }, text: s.title }));
      if (!s.rows.length) { out.append(el("div", { class: "empty", text: "No data in this period." })); continue; }
      out.append(el("div", { class: "scroll" }, el("table", { class: "t" }, el("thead", {}, el("tr", {}, ...s.columns.map((c) => el("th", { text: c })))),
        el("tbody", {}, ...s.rows.map((r) => el("tr", {}, ...r.map((c) => el("td", { text: c === null ? "" : String(c) }))))))));
      if (s.note) out.append(el("p", { class: "faint", style: { fontSize: "12px" }, text: s.note }));
    }
  });
  $("#csv").addEventListener("click", () => { const [span, p] = params(); download(`/api/v1/reports/${span}` + query({ ...p, format: "csv" })); });
  $("#pdf").addEventListener("click", () => { const [span, p] = params(); window.open(`/api/v1/reports/${span}` + query({ ...p, format: "pdf" }), "_blank"); });
})();
