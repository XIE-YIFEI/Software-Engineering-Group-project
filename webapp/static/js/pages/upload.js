/* Upload & analyse: queue an image or video; the background worker runs the pipeline and imports the results. */
(async () => {
  const { el, clear, $, get, post, api, pill, dt, fail, toast } = App;
  const form = $("#up");
  try {
    const [cams, scenes] = await Promise.all([get("/api/v1/cameras"), get("/api/v1/jobs/scenes")]);
    for (const c of cams.filter((c) => c.kind !== "congestion")) form.camera_id.append(el("option", { value: c.id, text: c.name }));
    for (const [k, v] of Object.entries(scenes)) form.scene.append(el("option", { value: k, text: v }));
  } catch (e) { fail(e); }
  const now = new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 19);     // Malaysia time
  form.recorded_at.value = now;

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    fd.set("render_video", form.render_video.checked ? "true" : "false");
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true; btn.textContent = "Uploading…";
    try { const j = await api("POST", "/api/v1/jobs", fd); toast(`Job #${j.id} queued`); form.file.value = ""; refresh(); }
    catch (e) { fail(e); }
    finally { btn.disabled = false; btn.textContent = "Start analysis"; }
  });

  const STATUS = { queued: "gray", running: "blue", done: "green", failed: "red", cancelled: "gray" };
  let timer = null;
  async function refresh() {
    let jobs;
    try { jobs = await get("/api/v1/jobs"); } catch (e) { return fail(e); }
    const box = clear($("#jobs"));
    if (!jobs.length) box.append(el("div", { class: "empty", text: "No jobs yet." }));
    for (const j of jobs) {
      box.append(el("div", { style: { padding: "10px 0", borderTop: "1px solid var(--line)" } },
        el("div", { class: "row between" },
          el("div", { class: "row" }, el("b", { text: `#${j.id}` }), pill(j.kind, "gray", false), el("span", { text: j.original_name }), pill(j.status, STATUS[j.status] || "gray"),
            el("span", { class: "muted", text: `${j.camera_name} · recorded ${dt(j.recorded_at)} · by ${j.created_by}` })),
          el("div", { class: "row" },
            ["queued", "running"].includes(j.status) ? el("button", { class: "btn small danger", onclick: async () => { try { await post(`/api/v1/jobs/${j.id}/cancel`); refresh(); } catch (e) { fail(e); } } }, "Cancel") : null,
            j.run_id ? el("a", { class: "btn small", href: `/plates?run_id=${j.run_id}` }, "Plates ›") : null,
            j.run_id && j.kind === "video" ? el("a", { class: "btn small", href: `/traffic?run=${j.run_id}` }, "Traffic ›") : null)),
        el("div", { class: "row", style: { marginTop: "6px" } },
          el("div", { class: "bar-track", style: { flex: 1 } }, el("div", { class: "bar-fill", style: { width: `${Math.round(j.progress * 100)}%`, background: j.status === "failed" ? "var(--red)" : undefined } })),
          el("span", { class: "muted num", style: { minWidth: "40px", textAlign: "right" }, text: `${Math.round(j.progress * 100)} %` })),
        el("div", { class: "muted", style: { fontSize: "12px", marginTop: "4px" }, text: j.error ? j.error.split("\n").slice(-3).join(" ") : j.message })));
    }
    const busy = jobs.some((j) => ["queued", "running"].includes(j.status));
    clearTimeout(timer);
    timer = setTimeout(refresh, busy ? 2000 : 15000);
  }
  refresh();
})();
