/* SECS Vision page helpers, shared by every page: API calls (with the CSRF token), a small DOM builder, formatting,
   plate badges and status pills, the click-to-timestamp video player, dialogs and toasts.
   Text from the server is always inserted with textContent (never innerHTML), so data cannot inject markup. */
const App = (() => {
  const S = window.SECS || {};

  // ---------------------------------------------------------------- API
  async function api(method, path, body) {
    const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (method !== "GET") opts.headers["X-CSRF-Token"] = S.csrf || "";
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    const res = await fetch(path, opts);
    if (res.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); throw new Error("Please log in"); }
    const isJson = (res.headers.get("content-type") || "").includes("json");
    const data = isJson ? await res.json() : await res.text();
    if (!res.ok) throw new Error((data && data.detail) || res.statusText || "Request failed");
    return data;
  }
  const query = (params) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== "") q.set(k, v);
    const s = q.toString();
    return s ? "?" + s : "";
  };
  const get = (path, params) => api("GET", path + query(params));
  const post = (path, body) => api("POST", path, body === undefined ? {} : body);

  // ---------------------------------------------------------------- DOM
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false)
      n.append(k instanceof Node ? k : document.createTextNode(String(k)));
    return n;
  }
  const $ = (sel, root) => (root || document).querySelector(sel);
  const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };

  // ---------------------------------------------------------------- formatting
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const time = (iso) => (iso || "").slice(11, 19);
  const hm = (iso) => (iso || "").slice(11, 16);
  const day = (iso) => iso ? `${+iso.slice(8, 10)} ${MONTHS[+iso.slice(5, 7) - 1]} ${iso.slice(0, 4)}` : "";
  const dt = (iso) => iso ? `${day(iso)} ${time(iso)}` : "";
  const pct = (v, d = 0) => v === null || v === undefined ? "–" : `${(v * 100).toFixed(d)} %`;
  const num = (v, d = 0) => v === null || v === undefined ? "–" : Number(v).toLocaleString("en-MY", { maximumFractionDigits: d, minimumFractionDigits: d });
  const dur = (sec) => { sec = Math.max(0, Math.round(sec)); const m = Math.floor(sec / 60), s = sec % 60; return `${m}:${String(s).padStart(2, "0")}`; };
  const mins = (m) => m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min` : `${m} min`;

  // ---------------------------------------------------------------- plates, pills
  const plate = (text, masked, big) => el("span", { class: "plate" + (masked ? " masked" : "") + (big ? " big" : ""), text: text || "—" });
  const pill = (text, colour, dotted = true) => el("span", { class: `pill ${colour || "gray"}` }, dotted ? el("span", { class: "d" }) : null, text);
  const SEV = { 1: ["High", "red"], 2: ["Hazard", "orange"], 3: ["Medium", "orange"] };
  const sevPill = (rank) => pill(SEV[rank] ? SEV[rank][0] : "Info", SEV[rank] ? SEV[rank][1] : "blue");
  const EVENT_COLOUR = { violation: "red", hazard: "orange", info: "blue", high: "red", medium: "orange" };
  const STATUS = { new: ["New", "blue"], acknowledged: ["Acknowledged", "yellow"], resolved: ["Resolved", "green"], false_alarm: ["False alarm", "gray"] };
  const statusPill = (s) => pill(STATUS[s] ? STATUS[s][0] : s, STATUS[s] ? STATUS[s][1] : "gray", false);
  function provPill(prov) {
    if (prov === "inserted_plate") return el("span", { class: "pill purple", title: "A real vehicle whose plate was digitally replaced by a fictitious one" }, el("span", { class: "d" }), "Simulated · inserted plate");
    if (prov === "synthetic") return el("span", { class: "pill purple", title: "No vehicle in the footage: a synthetic event" }, el("span", { class: "d" }), "Simulated · synthetic");
    return null;
  }
  function readingPill(s) {
    if (s.status === "TENTATIVE") return pill("Reading (provisional)", "yellow");
    if (s.valid_format === false) return pill("Non-standard format", "orange");
    return null;
  }

  // ---------------------------------------------------------------- toasts, dialogs
  function toast(msg, err) {
    const t = el("div", { class: "toast" + (err ? " err" : ""), role: "status", text: msg });
    document.body.append(t);
    setTimeout(() => t.remove(), err ? 6000 : 3200);
  }
  const fail = (e) => toast(e.message || String(e), true);

  /* A modal form: fields = [{name, label, type: text|select|textarea|number|datetime-local, options, value, required}].
     Resolves with {name: value} or null when cancelled. */
  function formDialog(title, fields, okText = "OK", intro = "") {
    return new Promise((resolve) => {
      const inputs = {};
      const form = el("form", { method: "dialog", class: "stack" },
        el("h2", { text: title }), intro ? el("p", { class: "muted", text: intro }) : null,
        ...fields.map((f) => {
          let input;
          if (f.type === "select") input = el("select", { name: f.name, required: f.required }, ...f.options.map((o) => el("option", { value: o, text: o, selected: o === f.value })));
          else if (f.type === "textarea") input = el("textarea", { name: f.name, rows: 3, required: f.required, placeholder: f.placeholder || "" });
          else input = el("input", { name: f.name, type: f.type || "text", value: f.value ?? "", required: f.required, placeholder: f.placeholder || "", step: f.step });
          if (f.type === "textarea" && f.value) input.value = f.value;
          inputs[f.name] = input;
          return el("label", { class: "field" }, f.label, input);
        }),
        el("div", { class: "row", style: { justifyContent: "flex-end" } },
          el("button", { class: "btn", value: "cancel", type: "button", onclick: () => { d.close(); } }, "Cancel"),
          el("button", { class: "btn primary", value: "ok", type: "submit" }, okText)));
      const d = el("dialog", {}, form);
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const out = {};
        for (const [k, i] of Object.entries(inputs)) out[k] = i.value;
        d.close(); d.remove(); resolve(out);
      });
      d.addEventListener("close", () => { if (d.isConnected) { d.remove(); resolve(null); } });
      document.body.append(d);
      d.showModal();
    });
  }

  /* "Show full plate" for masked roles: a reason is required; the server logs it and returns the plate. */
  async function reveal(sightingId) {
    const v = await formDialog("Show full plate", [
      { name: "reason_code", label: "Reason (recorded in the audit log)", type: "select", options: S.revealReasons || [], required: true },
      { name: "note", label: "Note (required for 'Other')", type: "textarea" }], "Show plate",
      "Traffic managers and administrators see masked plates. Revealing one is recorded with your reason and lasts 15 minutes.");
    if (!v) return null;
    return post(`/api/v1/sightings/${sightingId}/reveal`, v);
  }

  // ---------------------------------------------------------------- video (click-to-timestamp)
  /* A player that seeks to a record: load(url, seekSec) sets the source if it changed, seeks and plays. */
  function player(container, caption) {
    const video = el("video", { controls: true, preload: "metadata", playsinline: true, muted: true });
    const cap = el("div", { class: "row", style: { minHeight: "24px", marginBottom: "6px" } });
    const box = el("div", { class: "video" }, video);
    clear(container).append(cap, box);
    let current = "";
    function load(url, seek, label, autoplay = true) {
      if (!url) return;
      const go = () => { try { video.currentTime = Math.max(0, seek || 0); } catch (_) { /* not seekable yet */ } if (autoplay) video.play().catch(() => {}); };
      if (current !== url) {
        current = url;
        video.src = url;
        video.addEventListener("loadedmetadata", go, { once: true });
      } else go();
      clear(cap);
      if (label) cap.append(pill(label, "gray", false));
    }
    if (caption) cap.append(pill(caption, "gray", false));
    return { video, load, el: box };
  }
  const download = (url) => { location.href = url; };

  return { S, api, get, post, query, el, $, clear, time, hm, day, dt, pct, num, dur, mins, plate, pill, sevPill, statusPill,
           provPill, readingPill, EVENT_COLOUR, toast, fail, formDialog, reveal, player, download };
})();
