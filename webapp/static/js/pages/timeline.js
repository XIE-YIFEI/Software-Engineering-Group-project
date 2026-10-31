/* Cross-camera timeline of one plate: sightings in time order with distance, time gap and implied speed between them;
   impossible travel is flagged. Each camera sighting plays its video one second before the sighting. */
(async () => {
  const { el, clear, $, get, pill, plate, provPill, dt, fail, player } = App;
  const box = $("#chain"), vp = player($("#player"), "Select a sighting to play it");
  let t;
  try { t = await get(`/api/v1/plates/${encodeURIComponent(box.dataset.plate)}/timeline`); } catch (e) { clear(box).append(el("div", { class: "empty", text: e.message })); return; }
  clear(box);
  box.append(el("div", { class: "row between" }, el("div", { class: "row" }, plate(t.plate), t.impossible ? pill("Impossible travel: cloned plate?", "red") : pill("No impossible travel", "green")),
    t.simulated ? pill("Contains simulated data", "purple") : null));
  if (t.simulated) box.append(el("p", { class: "notice", text: "Our cameras are at different places on different dates, so no real vehicle appears at two of them. This chain uses simulated sightings (the fictitious plate XSD 4004) to show the check." }));
  const chain = el("div", { class: "timeline-chain", style: { marginTop: "12px" } });
  t.points.forEach((p, i) => {
    chain.append(el("div", { class: "tl-point" },
      el("div", { class: "num", style: { fontWeight: 600 }, text: dt(p.seen_at).slice(-8) }),
      el("div", { class: "tl-node", style: { background: p.source === "registry" ? "var(--purple)" : "var(--blue)" } }),
      el("div", {}, el("div", { style: { fontWeight: 600 }, text: p.place }), el("div", { class: "muted", style: { fontSize: "12px" }, text: `${dt(p.seen_at)} · ${p.run_label}` }),
        el("div", { style: { marginTop: "4px" } }, provPill(p.provenance))),
      p.video ? el("button", { class: "btn small", onclick: () => vp.load(p.video.url, p.video.seek_sec, `${t.plate} · ${p.place}`) }, "▶ Play") : el("span")));
    const leg = t.legs[i];
    if (leg) chain.append(el("div", { class: "tl-leg" + (leg.impossible ? " bad" : ""), text: leg.message }));
  });
  if (!t.points.length) chain.append(el("div", { class: "empty", text: "No sightings of this plate." }));
  box.append(chain, el("p", { class: "faint", style: { fontSize: "12px" }, text: t.rule }));
  if (t.near_plates.length) box.append(el("p", { class: "muted", style: { fontSize: "12px" } }, "Plates one character away (possible OCR misreads, not included): ",
    ...t.near_plates.map((p) => el("a", { href: `/timeline/${encodeURIComponent(p)}`, style: { marginRight: "8px" }, text: p }))));
})();
