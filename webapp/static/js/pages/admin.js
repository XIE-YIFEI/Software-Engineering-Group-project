/* Administration pages: users, data, privacy & audit (retention, audit log, correction pool), settings. */
(() => {
  const { el, clear, $, get, post, api, pill, dt, num, fail, toast, formDialog } = App;
  const table = (heads, rows) => el("div", { class: "scroll" }, el("table", { class: "t" }, el("thead", {}, el("tr", {}, ...heads.map((h) => el("th", { text: h })))), el("tbody", {}, ...rows)));
  const page = document.querySelector("[data-admin]").dataset.admin;

  async function users() {
    const box = $("#users");
    let us;
    try { us = await get("/api/v1/users"); } catch (e) { return fail(e); }
    clear(box).append(el("div", { class: "row between" }, el("h2", { text: "Accounts" }), el("button", { class: "btn primary", onclick: add }, "+ Add a user")),
      table(["User", "Name", "Role", "Site", "Last login", "Status", ""], us.map((u) => el("tr", {},
        el("td", {}, el("b", { text: u.username }), u.demo ? pill("demo account", "purple", false) : null), el("td", { text: u.display_name }), el("td", { text: u.role }),
        el("td", { text: u.site || "" }), el("td", { class: "num", text: u.last_login_at ? dt(u.last_login_at) : "never" }),
        el("td", {}, u.active ? pill("Active", "green", false) : pill("Disabled", "gray", false)),
        el("td", {}, el("button", { class: "btn small", onclick: () => patch(u.id, { active: !u.active }) }, u.active ? "Disable" : "Enable"),
          el("button", { class: "btn small", onclick: async () => { const v = await formDialog(`New password for ${u.username}`, [{ name: "password", label: "Password (at least 8 characters)", type: "password", required: true }], "Set");
            if (v) patch(u.id, { password: v.password }); } }, "Reset password"))))),
      el("p", { class: "faint", style: { fontSize: "12px" }, text: "Changing a user's role, password or status signs them out everywhere. Demo accounts have published passwords: disable them before other computers can reach the server." }));
  }
  async function add() {
    const v = await formDialog("Add a user", [{ name: "username", label: "Username", required: true }, { name: "display_name", label: "Display name" },
      { name: "role", label: "Role", type: "select", options: ["police", "guard", "traffic", "admin"], required: true },
      { name: "site_code", label: "Site code (guards only, e.g. demo-condo)" }, { name: "password", label: "Password (at least 8 characters)", type: "password", required: true }], "Add");
    if (!v) return;
    try { await post("/api/v1/users", { ...v, site_code: v.site_code || null }); toast("User added"); users(); } catch (e) { fail(e); }
  }
  async function patch(id, body) { try { await api("PATCH", `/api/v1/users/${id}`, body); toast("Saved"); users(); } catch (e) { fail(e); } }

  async function data() {
    let d;
    try { d = await get("/api/v1/data"); } catch (e) { return fail(e); }
    const box = clear($("#data"));
    box.append(el("section", { class: "card" }, el("h2", { text: "Analysis runs" }), table(["Run", "Camera", "Origin", "Results file", "SHA-256", "Sightings", "Crops", "Alerts", "Imported"],
      d.runs.map((r) => el("tr", {}, el("td", { text: r.label }), el("td", { text: r.camera }), el("td", { text: r.origin }), el("td", {}, el("code", { text: r.results_path })),
        el("td", {}, el("code", { title: r.results_sha256, text: r.results_sha256.slice(0, 12) + "…" })), el("td", { class: "num", text: r.sightings }),
        el("td", { class: "num", text: r.crops }), el("td", { class: "num", text: r.alerts }), el("td", { class: "num", text: dt(r.imported_at) }))))),
      el("section", { class: "card" }, el("h2", { text: "Congestion series and forecasts" }), table(["Day", "CSV", "Calibrated", "Forecast sections", "Imported"],
        d.series_days.map((s) => el("tr", {}, el("td", { text: s.label }), el("td", {}, el("code", { text: s.csv_path })), el("td", { text: s.calibrated ? "yes" : "no" }),
          el("td", { class: "num", text: s.forecast_sections }), el("td", { class: "num", text: dt(s.imported_at) })))),
        table(["Day", "Section", "Model", "Trained on", "Origin"], d.forecast_runs.map((f) => el("tr", {}, el("td", { text: f.day }), el("td", { text: f.entity }),
          el("td", { text: f.model }), el("td", { text: f.train_day }), el("td", { text: f.origin === "committed" ? `committed (${f.source_path})` : "computed at import" }))))),
      el("section", { class: "card" }, el("h2", { text: "Where things are" }), el("dl", { class: "kv" },
        el("dt", { text: "Data folder" }), el("dd", {}, el("code", { text: d.data_dir })),
        el("dt", { text: "Vehicle registry" }), el("dd", {}, el("code", { text: d.registry }), " ", pill("SIMULATED", "purple")),
        el("dt", { text: "Last demo import" }), el("dd", { text: d.last_import ? dt(d.last_import) : "never" }),
        el("dt", { text: "Re-import" }), el("dd", {}, el("code", { text: ".venv\\Scripts\\python -m webapp.importer --demo" })))));
  }

  async function privacy() {
    const showPurge = (r) => clear($("#purgeout")).append(el("div", { class: "notice info", text:
      `${r.dry_run ? "Would delete" : "Deleted"}: ${r.sightings} sightings (${r.alerts} closed alerts with them), ${r.access_events} access-log entries, ${r.corrections} correction-pool items, ${r.audit_rows} audit rows, ${r.files} files. Cut-offs: ordinary sightings recorded before ${dt(r.cutoffs.retention_days)}.` }));
    $("#preview").addEventListener("click", async () => { try { showPurge(await post("/api/v1/retention/purge?dry_run=true")); } catch (e) { fail(e); } });
    $("#purge").addEventListener("click", async () => {
      const v = await formDialog("Purge now?", [{ name: "ok", label: "Type PURGE to delete what the retention settings no longer allow", required: true }], "Purge");
      if (!v || v.ok !== "PURGE") return;
      try { showPurge(await post("/api/v1/retention/purge?dry_run=false")); } catch (e) { fail(e); }
    });
    $("#verify").addEventListener("click", async () => {
      try { const r = await get("/api/v1/audit/verify"); clear($("#verifyout")).append(el("p", { class: r.ok ? "notice info" : "notice warn", text: r.ok ? `Hash chain intact: ${num(r.rows_checked)} rows checked.` : `Chain broken at row ${r.first_bad_id} (after ${r.rows_checked} good rows).` })); } catch (e) { fail(e); }
    });
    const load = async () => {
      let rows;
      try { rows = await get("/api/v1/audit", Object.fromEntries(new FormData($("#af")).entries())); } catch (e) { return fail(e); }
      clear($("#audit")).append(table(["Time", "User", "Role", "Action", "Target", "Why", "Details"], rows.map((r) => el("tr", {},
        el("td", { class: "num", text: dt(r.at) }), el("td", { text: r.username }), el("td", { class: "muted", text: r.role }), el("td", {}, pill(r.action, r.action.startsWith("reveal") ? "orange" : "gray", false)),
        el("td", { class: "muted", text: [r.target_type, r.target_id].filter(Boolean).join(" ") }), el("td", { text: r.reason }),
        el("td", { class: "muted", style: { fontSize: "12px", maxWidth: "420px" }, text: JSON.stringify(r.details) })))));
    };
    $("#af").addEventListener("submit", (ev) => { ev.preventDefault(); load(); });
    load();
    get("/api/v1/corrections").then((cs) => clear($("#corr")).append(cs.length ? table(["Time", "OCR read", "Corrected to", "Crop", "By"], cs.map((c) => el("tr", {},
      el("td", { class: "num", text: dt(c.created_at) }), el("td", { text: c.ocr_compact }), el("td", { text: c.corrected_compact }), el("td", { text: c.has_crop ? "yes" : "no" }), el("td", { text: c.created_by }))))
      : el("div", { class: "empty", text: "No corrections yet: false alarms with a corrected plate land here (kept for the correction-pool retention period)." }))).catch(fail);
  }

  async function settings() {
    const form = $("#settings");
    let s;
    try { s = await get("/api/v1/settings"); } catch (e) { return fail(e); }
    const LABEL = { retention_days: "Ordinary sightings kept (days)", retention_alert_days: "Sightings with alerts kept after closing (days)",
      retention_access_days: "Access log kept (days)", retention_corrections_days: "OCR correction pool kept (days)", retention_audit_days: "Audit log kept (days)",
      overstay_hours: "Visitor overstay after (hours)", reveal_minutes: "A 'Show full plate' lasts (minutes)", session_hours: "A login lasts (hours)",
      default_pass_hours: "Default visitor pass (hours)", max_speed_kmh: "Impossible travel above (km/h)" };
    clear(form).append(el("h2", { text: "Settings" }), ...Object.entries(s).map(([k, v]) => el("label", { class: "field", style: { marginBottom: "10px" } }, LABEL[k] || k, el("input", { name: k, type: "number", value: v, min: 1, step: "any" }))),
      el("button", { class: "btn primary", type: "submit" }, "Save"));
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const body = Object.fromEntries([...new FormData(form).entries()].map(([k, v]) => [k, +v]));
      try { await api("PUT", "/api/v1/settings", body); toast("Settings saved (audit-logged)"); } catch (e) { fail(e); }
    };
  }
  ({ users, data, privacy, settings })[page]();
})();
