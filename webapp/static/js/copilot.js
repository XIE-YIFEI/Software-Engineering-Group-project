/* SECS Copilot panel: a slide-in panel on every page (button "Copilot" in the top bar) and the full /assistant page.
   Answers stream from POST /api/v1/copilot/chat (server-sent events): text, tool steps, citation chips, action cards
   (none while the copilot is read-only) and the number check. Model text is rendered with a small Markdown subset
   built from DOM nodes (textContent only, never innerHTML), so neither the model nor the data can inject markup. */
const Copilot = (() => {
  const { el, clear, $, get, post, api, pill, plate, dt, time, fail, toast, S } = App;
  const LANGS = { en: "EN", zh: "中文", ms: "BM" };
  let status = null;
  const storeKey = `secs.copilot.${S.user ? S.user.id : 0}`;
  const remember = (k, v) => { try { if (v === undefined) return sessionStorage.getItem(k); if (v === null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, v); } catch (_) { return null; } return null; };
  const lang = () => { try { return localStorage.getItem("secs.lang") || ((navigator.language || "").startsWith("zh") ? "zh" : "en"); } catch (_) { return "en"; } };
  const setLang = (v) => { try { localStorage.setItem("secs.lang", v); } catch (_) { /* storage off */ } };

  // ---------------------------------------------------------------- server-sent events over fetch (POST + CSRF)
  async function stream(path, body, on) {
    const res = await fetch(path, { method: "POST", credentials: "same-origin", body: JSON.stringify(body || {}),
      headers: { "Content-Type": "application/json", Accept: "text/event-stream", "X-CSRF-Token": S.csrf || "" } });
    if (res.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); return; }
    if (!res.ok) { let d = {}; try { d = await res.json(); } catch (_) { /* not JSON */ } throw new Error(d.detail || res.statusText); }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        let ev = "message", data = "";
        for (const line of chunk.split("\n")) { if (line.startsWith("event: ")) ev = line.slice(7); else if (line.startsWith("data: ")) data += line.slice(6); }
        on(ev, data ? JSON.parse(data) : null);
      }
    }
  }

  // ---------------------------------------------------------------- Markdown subset + citation chips
  const INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\[\[[A-Z]:\d+\]\])/g;
  function inline(text, cites) {
    const out = [];
    let last = 0;
    for (const m of text.matchAll(INLINE)) {
      if (m.index > last) out.push(document.createTextNode(text.slice(last, m.index)));
      const t = m[0];
      if (t.startsWith("**")) out.push(el("b", { text: t.slice(2, -2) }));
      else if (t.startsWith("`")) out.push(el("code", { text: t.slice(1, -1) }));
      else out.push(chip(t.slice(2, -2), cites));
      last = m.index + t.length;
    }
    if (last < text.length) out.push(document.createTextNode(text.slice(last)));
    return out;
  }
  const BULLET = /^\s*[-*•] /, NUMBERED = /^\s*\d+[.)] /, HEADING = /^\s*#{1,4}\s+(.*)$/;
  const isTable = (l) => l.trim().startsWith("|");
  function md(text, cites) {
    const frag = document.createDocumentFragment();
    const lines = (text || "").replace(/\r/g, "").split("\n");
    let i = 0;
    const run = (test) => { const out = []; while (i < lines.length && test(lines[i])) out.push(lines[i++]); return out; };
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) { i++; continue; }
      if (BULLET.test(l)) frag.append(el("ul", {}, ...run((x) => BULLET.test(x)).map((x) => el("li", {}, ...inline(x.replace(BULLET, ""), cites)))));
      else if (NUMBERED.test(l)) frag.append(el("ol", {}, ...run((x) => NUMBERED.test(x)).map((x) => el("li", {}, ...inline(x.replace(NUMBERED, ""), cites)))));
      else if (isTable(l)) {
        const rows = run(isTable).filter((x) => !/^\s*\|[\s:|-]+\|\s*$/.test(x)).map((x) => x.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
        frag.append(el("div", { class: "scroll" }, el("table", { class: "t" }, el("thead", {}, el("tr", {}, ...rows[0].map((c) => el("th", {}, ...inline(c, cites))))),
          el("tbody", {}, ...rows.slice(1).map((r) => el("tr", {}, ...r.map((c) => el("td", {}, ...inline(c, cites)))))))));
      } else if (HEADING.test(l)) { frag.append(el("h4", {}, ...inline(l.match(HEADING)[1], cites))); i++; }
      else {
        const p = el("div", { class: "cp-p" });
        run((x) => x.trim() && !BULLET.test(x) && !NUMBERED.test(x) && !isTable(x) && !HEADING.test(x)).forEach((x, k) => {
          if (k) p.append(el("br"));
          p.append(...inline(x, cites));
        });
        frag.append(p);
      }
    }
    return frag;
  }

  function chip(ref, cites) {
    const c = cites && cites[ref];
    if (!c) return el("span", { class: "cite bad", title: "This reference was not returned by any tool" }, `${ref} · unverified`);
    const label = [ref, c.plate || null, c.time ? time(c.time) : null].filter(Boolean).join(" · ");
    return el("button", { type: "button", class: "cite" + (c.simulated ? " sim" : ""), title: `${c.label || ""}${c.simulated ? " · simulated" : ""} · click to open`,
      onclick: () => openRecord(c) }, c.simulated ? el("span", { class: "d" }) : null, label);
  }

  /* A record behind a chip: the crop (sharp or blurred, as the server decides), details, and the video one second
     before it where the role may play it. */
  async function openRecord(c) {
    const body = el("div", { class: "stack", style: { gap: "10px" } });
    const d = el("dialog", { class: "cp-dialog" }, el("div", { class: "row between" }, el("h2", { style: { margin: 0 }, text: `${c.ref} · ${c.label || c.kind}` }),
      el("button", { class: "btn small", onclick: () => d.close() }, "Close")), body);
    d.addEventListener("close", () => d.remove());
    document.body.append(d); d.showModal();
    const kv = el("dl", { class: "kv" });
    const row = (k, v) => { if (v !== null && v !== undefined && v !== "") kv.append(el("dt", { text: k }), el("dd", {}, v)); };
    row("Plate", c.plate ? plate(c.plate, c.masked) : null); row("Time", dt(c.time)); row("Where", c.label);
    if (c.simulated) row("Data", App.pill(c.provenance_label || "Simulated", "purple"));
    body.append(kv);
    if (c.crop_url) body.append(el("img", { src: c.crop_url, alt: "plate crop", style: { maxWidth: "100%", maxHeight: "120px", objectFit: "contain", background: "#000", borderRadius: "6px", border: "1px solid var(--line)" } }));
    if (c.kind === "sighting") {
      try {
        const s = await get(`/api/v1/sightings/${c.id}`, { purpose: c.purpose || "Alert follow-up" });
        row("Camera", s.camera_name); row("Reading", s.status === "TENTATIVE" ? "provisional" : "confirmed"); row("Vehicle", s.vtype);
        if (s.alerts.length) row("Alerts", el("span", { class: "row", style: { gap: "6px" } }, ...s.alerts.map((a) => el("a", { href: `/alerts#${a.id}` }, a.title))));
        if (s.can_reveal) body.append(el("p", { class: "notice info", text: "The plate is masked for your role. Use “Show full plate” in the plate search: it needs a reason and is logged. The copilot is read-only and cannot reveal plates." }));
      } catch (e) { body.append(el("p", { class: "muted", text: e.message })); }
    }
    if (c.video) {
      const box = el("div");
      body.append(box);
      const vp = App.player(box);
      vp.load(c.video.url, c.video.seek_sec, `${time(c.time)} · from 1 s before`);
    } else if (c.kind === "sighting" && S.user.masked) body.append(el("p", { class: "muted", style: { fontSize: "12px" }, text: "Videos of plate cameras show full plates, so they are not shown to your role." }));
    const links = el("div", { class: "row" });
    if (c.link) links.append(el("a", { class: "btn small", href: c.link }, "Open the page ›"));
    if (c.kind === "sighting" && !c.masked) links.append(el("a", { class: "btn small", href: `/plates?plate=${encodeURIComponent(c.plate)}` }, "Plate search ›"));
    if (links.childNodes.length) body.append(links);
  }

  // ---------------------------------------------------------------- pieces of an answer
  function stepsView(steps) {
    const det = el("details", { class: "cp-steps" }, el("summary", { text: `Tool steps (${steps.length})` }));
    for (const s of steps) det.append(stepRow(s));
    return det;
  }
  function stepRow(s, running) {
    const args = Object.entries(s.input || {}).filter(([, v]) => v !== "" && v !== null && v !== undefined).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(", ");
    return el("div", { class: "cp-step" + (s.error ? " err" : "") }, el("span", { class: "cp-step-dot" + (running ? " run" : "") }),
      el("code", { text: `${s.tool}(${args.length > 90 ? args.slice(0, 90) + "…" : args})` }), el("span", { class: "muted", text: running ? " running…" : ` → ${s.summary || ""}` }));
  }
  function checkView(check, ai) {
    if (!check) return document.createTextNode("");
    if (check.template) return el("div", { class: "cp-check ok", text: "Template answer: the numbers are filled in from the system's records." });
    const bad = [...(check.unmatched || []).map((u) => u.text), ...(check.plates_unmatched || []), ...(check.unknown_refs || [])];
    if (check.ok) return el("div", { class: "cp-check ok", title: "Every number, time and plate in the answer was found in the tool results" },
      check.checked ? `✓ Number check: ${check.checked} numbers, times and plates match the tool results` : "✓ Number check: the text contains no numbers or plates");
    return el("div", { class: "cp-check bad", title: (check.unmatched || []).map((u) => `${u.text}: …${u.context}…`).join("\n") },
      `⚠ Number check: ${bad.length} not found in the tool results: ${bad.slice(0, 8).join(" · ")}${bad.length > 8 ? " …" : ""}. Check these against the records.`);
  }
  function panelsView(panels) {
    const lg = lang() === "zh" ? "zh" : "en";
    return (panels || []).filter((p) => p.type === "sop").map((p) => sopView(p.sop, p.legal, `${p.title} (${p.alert})`, lg));
  }
  function sopView(sop, legal, title, lg) {
    const box = el("div", { class: "cp-sop" });
    const draft = sop.status !== "reviewed";
    box.append(el("div", { class: "row between" }, el("b", { text: `Next steps · ${title}` }),
      pill(draft ? "SOP draft, not yet reviewed" : `SOP reviewed ${sop.reviewed_on} by ${sop.reviewed_by}`, draft ? "orange" : "green", false)));
    if (!sop.found) box.append(el("p", { class: "muted", text: "The SOP file has no steps for this alert and role." }));
    else box.append(el("ol", {}, ...(sop[lg] || sop.en).map((s) => el("li", { text: s }))));
    box.append(el("div", { class: "faint", style: { fontSize: "11px" }, text: `Quoted from ${sop.source}${sop.scope === "role default" ? " (role default)" : ""}; the copilot does not write procedures.` }));
    if (legal && legal.sections && legal.sections.length) {
      box.append(el("b", { style: { display: "block", marginTop: "8px" }, text: "Road Transport Act 1987 (Act 333): the alert relates to" }),
        el("ul", {}, ...legal.sections.map((x) => el("li", {}, el("b", { text: `s. ${x.section} ${x.title}` }), ` · ${x.text}`))));
      if (legal.note) box.append(el("div", { class: "faint", style: { fontSize: "11px" }, text: legal.note }));
      box.append(el("div", { class: "faint", style: { fontSize: "11px" }, text: `Verified list (webapp/legal.yaml, checked ${legal.checked}). Not a finding that an offence happened.` }));
    } else if (legal) box.append(el("div", { class: "faint", style: { fontSize: "11px" }, text: "No section of the Road Transport Act 1987 is cited for this kind of alert." }));
    return box;
  }

  /* An action card: nothing happens until Confirm; the fields are the user's to check and complete. While the copilot
     is read-only none is prepared, and the server refuses Confirm on one prepared earlier (its message shows here). */
  function cardView(card) {
    const box = el("div", { class: "cp-card " + card.status });
    function render(c) {
      clear(box); box.className = "cp-card " + c.status;
      box.append(el("div", { class: "row between" }, el("b", { text: c.title }), pill({ pending: "Waiting for your confirmation", confirmed: "Done", cancelled: "Cancelled", failed: "Failed", expired: "Expired" }[c.status] || c.status,
        { pending: "yellow", confirmed: "green", cancelled: "gray", failed: "red", expired: "gray" }[c.status] || "gray", false)));
      for (const l of c.lines || []) box.append(el("div", { class: "muted", style: { fontSize: "12px" }, text: l }));
      if (c.status === "pending") {
        const inputs = {};
        const form = el("div", { class: "stack", style: { gap: "6px", marginTop: "8px" } });
        for (const f of c.fields) {
          let input;
          if (f.type === "select") input = el("select", { name: f.name }, el("option", { value: "", text: "Choose…" }), ...f.options.map((o) => el("option", { value: o, text: o, selected: o === f.value })));
          else if (f.type === "textarea") { input = el("textarea", { name: f.name, rows: 2 }); input.value = f.value || ""; }
          else input = el("input", { name: f.name, type: f.type || "text", value: f.type === "datetime-local" ? (f.value || "").slice(0, 16) : (f.value || "") });
          inputs[f.name] = input;
          form.append(el("label", { class: "field" }, f.label + (f.required ? " *" : ""), input));
        }
        const msg = el("div", { class: "muted", style: { fontSize: "12px" } });
        const ok = el("button", { class: "btn primary small", onclick: async () => {
          const fields = {};
          for (const [k, i] of Object.entries(inputs)) fields[k] = i.value;
          ok.disabled = true;
          try { render(await post(`/api/v1/copilot/actions/${c.id}/confirm`, { fields })); toast("Done (recorded in the audit log)"); }
          catch (e) { msg.textContent = e.message; ok.disabled = false; }
        } }, "Confirm");
        const no = el("button", { class: "btn small", onclick: async () => { try { render(await post(`/api/v1/copilot/actions/${c.id}/cancel`)); } catch (e) { msg.textContent = e.message; } } }, "Cancel");
        form.append(el("div", { class: "row" }, ok, no, el("span", { class: "faint", style: { fontSize: "11px" }, text: `expires ${time(c.expires_at)}` })), msg);
        box.append(form);
      } else if (c.result) box.append(el("div", { class: c.status === "failed" ? "notice warn" : "notice info", style: { marginTop: "6px" }, text: c.result.text || c.result.error || "" }));
    }
    render(card);
    return box;
  }

  // ---------------------------------------------------------------- the panel
  function mount(root, mode) {
    const page = mode === "page";
    let conv = page ? null : +remember(storeKey) || null;
    let busy = false;
    const head = el("div", { class: "cp-head" });
    const list = el("div", { class: "cp-list" });
    const quick = el("div", { class: "cp-quick" });
    const box = el("textarea", { rows: 2, placeholder: "Ask in English, 中文 or Bahasa Melayu… (Enter to send, Shift+Enter for a new line)" });
    const send = el("button", { class: "btn primary", type: "button" }, "Send");
    const foot = el("div", { class: "cp-foot" }, el("div", { class: "row", style: { gap: "8px", alignItems: "flex-end", flexWrap: "nowrap" } }, box, send),
      el("div", { class: "faint", style: { fontSize: "11px", marginTop: "6px" }, text: "AI-generated answers: check the citations and the number-check badge. Questions are logged (who, when, which tools) and kept 30 days." }));
    const side = page ? el("aside", { class: "cp-convs card" }) : null;
    const main = el("div", { class: "cp-main" }, head, list, quick, foot);
    clear(root).append(...(page ? [side, main] : [main]));
    root.classList.add(page ? "cp-page" : "cp-panel");

    const langSel = el("select", { class: "small", title: "Language of template answers and AI explanations" }, ...Object.entries(LANGS).map(([k, v]) => el("option", { value: k, text: v, selected: k === lang() })));
    langSel.addEventListener("change", () => setLang(langSel.value));
    const stateChip = el("span", { class: "chip" }, "…");
    head.append(el("div", { class: "row between", style: { flexWrap: "nowrap" } }, el("div", { class: "row", style: { gap: "8px" } }, el("b", { text: "SECS Copilot" }), stateChip),
      el("div", { class: "row", style: { gap: "6px", flexWrap: "nowrap" } }, langSel, el("button", { class: "btn small", type: "button", onclick: () => newConversation() }, "New"),
        page ? null : el("button", { class: "btn small ghost", type: "button", title: "Close (Esc)", onclick: () => root.dispatchEvent(new Event("cp-close")) }, "✕"))));

    async function loadStatus() {
      try { status = await get("/api/v1/copilot/status"); } catch (e) { return fail(e); }
      clear(stateChip).append(el("span", { class: "dot", style: { background: status.online ? "var(--green)" : "var(--orange)" } }),
        status.online ? `Online · ${status.model_label}` : "Offline: templates, not AI");
      stateChip.title = status.online ? `Effort ${status.effort}${status.fallbacks ? "; a request a safety filter declines is re-run on a fallback model" : ""}. Today ${status.spent_today_usd} of ${status.daily_budget_usd} USD.` : status.reason;
      clear(quick).append(el("span", { class: "faint", style: { fontSize: "11px" }, text: "Quick questions (templates, no AI):" }),
        ...status.quick.map((q) => el("button", { class: "btn small", type: "button", onclick: () => askQuick(q) }, lang() === "zh" ? q.zh : q.en)));
      if (!list.childNodes.length) intro();
    }
    function intro() {
      const masked = S.user.masked;
      clear(list).append(el("div", { class: "cp-intro" },
        el("p", { text: "Ask about plates, alerts, the gate, traffic or the congestion forecast. The copilot answers only from the system's own records, with your role's permissions; every record it names is a clickable chip." }),
        el("p", { class: "muted", text: masked ? "Read-only: the copilot cannot change records or reveal plates. Your role sees masked plates: for a full plate, use “Show full plate” in the plate search (it needs a reason and is logged)." :
          "Read-only: the copilot answers questions but cannot change records. Issue passes, edit lists and handle alerts on their own pages, as usual." }),
        status && !status.online ? el("p", { class: "notice warn", text: `Offline: ${status.reason}. The quick questions below still work (templates, not AI).` }) : null));
    }
    function newConversation() { conv = null; if (!page) remember(storeKey, null); clear(list); intro(); refreshConvs(); }

    function userBubble(text) { list.append(el("div", { class: "cp-msg user" }, el("div", { class: "cp-bubble", text }))); }
    function aiShell(label) {
      const meta = el("div", { class: "cp-meta" }, label || "");
      const steps = el("div", { class: "cp-live-steps" });
      const body = el("div", { class: "cp-text" });
      const extra = el("div", { class: "stack", style: { gap: "8px" } });
      const wrap = el("div", { class: "cp-msg ai" }, meta, steps, body, extra);
      list.append(wrap);
      return { wrap, meta, steps, body, extra };
    }
    const scroll = () => { list.scrollTop = list.scrollHeight; };

    function renderAnswer(shell, d, cards) {
      const cites = { ...(d.record_data || {}), ...(d.citations || {}) };
      clear(shell.meta).append(d.ai ? pill("AI-generated", "blue", false) : pill(d.label || "Offline: templates, not AI", "orange", false),
        el("span", { class: "faint", text: ` ${d.model || ""}${d.fallback ? ` (fallback: ${d.fallback})` : ""}${d.cost_usd ? ` · ${d.cost_usd.toFixed(4)} USD` : ""}` }));
      clear(shell.steps);
      if (d.steps && d.steps.length) shell.steps.append(stepsView(d.steps));
      clear(shell.body).append(md(d.text || "", cites));
      clear(shell.extra);
      for (const id of d.cards || []) if (cards && cards[id]) shell.extra.append(cardView(cards[id]));
      shell.extra.append(...panelsView(d.panels));
      if (d.problem) shell.extra.append(el("div", { class: "notice warn", text: d.problem }));
      const chk = checkView(d.check, d.ai);
      if (chk) shell.extra.append(chk);
      const others = (d.records || []).filter((r) => cites[r]);
      if (others.length) shell.extra.append(el("details", { class: "cp-steps" }, el("summary", { text: `Other records the tools returned (${others.length})` }),
        el("div", { class: "row", style: { gap: "4px" } }, ...others.map((r) => chip(r, cites)))));
    }

    async function ask(text) {
      if (busy || !text.trim()) return;
      busy = true; send.disabled = true;
      userBubble(text);
      const sh = aiShell(pill("AI · thinking…", "blue", false));
      let buf = "", cites = {}, cards = {}, live = {};
      const paint = () => { clear(sh.body).append(md(buf, cites)); scroll(); };
      try {
        await stream("/api/v1/copilot/chat", { message: text, conversation_id: conv, page: pageContext() }, (ev, d) => {
          if (ev === "meta") { conv = d.conversation_id; if (!page) remember(storeKey, String(conv)); if (d.notice) list.insertBefore(el("div", { class: "notice info", text: d.notice }), sh.wrap); }
          else if (ev === "text") { buf += d; paint(); }
          else if (ev === "step") { live[d.id] = stepRow({ tool: d.tool, input: d.input }, true); sh.steps.append(live[d.id]); scroll(); }
          else if (ev === "step_done") { const n = live[d.id]; if (n) n.replaceWith(stepRow({ tool: d.tool, input: {}, summary: d.summary, error: d.error })); }
          else if (ev === "cite") { cites = { ...cites, ...d }; paint(); }
          else if (ev === "card") { cards[d.id] = d; sh.extra.append(cardView(d)); scroll(); }
          else if (ev === "offline") { clear(sh.meta).append(pill("Offline: templates, not AI", "orange", false)); clear(sh.body).append(el("p", { text: `The AI service is not available: ${d.reason}. Use the quick questions below (templates, not AI).` })); }
          else if (ev === "error") { sh.extra.append(el("div", { class: "notice warn", text: d.message })); }
          else if (ev === "done") { if (d.conversation_id === null) { conv = null; if (!page) remember(storeKey, null); } renderAnswer(sh, d.display, cards); refreshConvs(); }
        });
      } catch (e) { sh.extra.append(el("div", { class: "notice warn", text: e.message })); }
      busy = false; send.disabled = false; scroll();
    }

    async function askQuick(q) {
      if (busy) return;
      userBubble((lang() === "zh" ? q.zh : q.en) + "  (quick question)");
      const sh = aiShell(pill("template", "orange", false));
      try { renderAnswer(sh, await post("/api/v1/copilot/quick", { id: q.id, lang: lang() }), {}); } catch (e) { sh.extra.append(el("div", { class: "notice warn", text: e.message })); }
      scroll();
    }

    async function openConversation(id) {
      let h;
      try { h = await get(`/api/v1/copilot/conversations/${id}`); } catch (e) { return fail(e); }
      conv = h.id; if (!page) remember(storeKey, String(conv));
      clear(list);
      for (const it of h.items) {
        if (it.role === "user") userBubble(it.text);
        else renderAnswer(aiShell(), it, h.cards);
      }
      scroll(); refreshConvs();
    }

    async function refreshConvs() {
      if (!side) return;
      let cs = [];
      try { cs = await get("/api/v1/copilot/conversations"); } catch (e) { return; }
      clear(side).append(el("h3", { text: "Conversations" }), el("button", { class: "btn small", style: { marginBottom: "8px" }, onclick: () => newConversation() }, "+ New conversation"),
        ...cs.map((c) => el("div", { class: "cp-conv" + (c.id === conv ? " on" : ""), onclick: () => openConversation(c.id) },
          el("div", { text: c.title || "(untitled)" }), el("div", { class: "faint", style: { fontSize: "11px" }, text: `${dt(c.updated_at)} · ${c.questions} question${c.questions === 1 ? "" : "s"}` }),
          el("button", { class: "btn small ghost", title: "Delete", onclick: async (ev) => { ev.stopPropagation(); try { await api("DELETE", `/api/v1/copilot/conversations/${c.id}`); if (c.id === conv) newConversation(); else refreshConvs(); } catch (e) { fail(e); } } }, "🗑"))),
        cs.length ? null : el("p", { class: "faint", text: "None yet." }));
    }

    send.addEventListener("click", () => { const t = box.value; box.value = ""; ask(t); });
    box.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); send.click(); } });
    loadStatus().then(() => { if (conv) openConversation(conv); });
    refreshConvs();
    return { ask, focus: () => box.focus() };
  }

  function pageContext() {
    const ctx = { path: location.pathname };
    const n = +location.hash.slice(1);
    if (location.pathname === "/alerts" && n) ctx.alert_id = n;
    return ctx;
  }

  // ---------------------------------------------------------------- the slide-in panel on every page
  function drawer() {
    if (location.pathname === "/assistant") return;
    const btn = el("button", { class: "btn small cp-open", type: "button", title: "SECS Copilot (AI assistant)" }, "✦ Copilot");
    const top = document.querySelector(".top .spacer");
    if (top) top.after(btn);
    const root = el("div", { class: "cp-drawer", hidden: true });
    document.body.append(root);
    let ui = null;
    const open = () => { if (!ui) ui = mount(root, "drawer"); root.hidden = false; ui.focus(); };
    const close = () => { root.hidden = true; };
    btn.addEventListener("click", () => (root.hidden ? open() : close()));
    root.addEventListener("cp-close", close);
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !root.hidden && !document.querySelector("dialog[open]")) close(); });
  }

  return { mount, drawer, stream, md, chip, sopView, checkView, cardView, lang, setLang, LANGS };
})();
document.addEventListener("DOMContentLoaded", () => Copilot.drawer());
