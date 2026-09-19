/* Cybersecurity Research & Funding Tracker — frontend v3 */
(() => {
  "use strict";

  const API = {
    search: (kw, source, closingSoon) => {
      const p = new URLSearchParams();
      p.set("keyword", kw || "cybersecurity");
      if (source) p.set("source", source);
      if (closingSoon) p.set("closing_soon", "true");
      return fetch(`/api/search?${p}`).then(r => r.json());
    },
    sources: () => fetch("/api/sources").then(r => r.json()),
    health:  () => fetch("/api/health").then(r => r.json()),
    stats:   () => fetch("/api/stats").then(r => r.json()),
  };

  const LS = {
    saved: "csft.v3.saved",
    projects: "csft.v3.projects",
    phd: "csft.v3.phd",
    tasks: "csft.v3.tasks",
    prefs: "csft.v3.prefs",
  };

  const state = {
    keyword: "cybersecurity",
    source: "",
    sort: "relevance",
    closingSoon: false,
    sources: [],
    results: [],
    filters: { area: new Set(), type: new Set(), elig: new Set(), status: new Set() },
    saved: loadJSON(LS.saved, []),
    projects: loadJSON(LS.projects, []),
    phd: loadJSON(LS.phd, []),
    tasks: loadJSON(LS.tasks, []),
    drawerOpp: null,
  };

  // ---------------- Utilities ----------------
  function loadJSON(k, fallback) {
    try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : fallback; }
    catch { return fallback; }
  }
  function saveJSON(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} }
  const $  = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }
  function uid() { return Math.random().toString(36).slice(2, 10); }
  function fmtDate(d) {
    if (!d) return "—";
    const dt = new Date(d);
    if (isNaN(dt.getTime())) return esc(d);
    return dt.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
  }
  function relTime(iso) {
    if (!iso) return "—";
    const t = new Date(iso).getTime();
    if (isNaN(t)) return "—";
    const diff = Math.max(0, Date.now() - t);
    const s = Math.round(diff / 1000);
    if (s < 60) return `${s}s ago`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.round(h / 24);
    return `${d}d ago`;
  }
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove("show"), 2200);
  }

  // ---------------- Routing ----------------
  const routes = ["dashboard","opportunities","sources","saved","research","phd","tasks","settings"];
  function route() {
    const hash = (location.hash || "#/dashboard").replace(/^#\//, "");
    const name = routes.includes(hash) ? hash : "dashboard";
    routes.forEach(r => {
      const v = $(`#view-${r}`);
      if (v) v.classList.toggle("hidden", r !== name);
    });
    $$(".topnav a").forEach(a => a.classList.toggle("active", a.dataset.route === name));
    $("#topnav").classList.remove("open");

    if (name === "dashboard") loadDashboard();
    if (name === "opportunities") runSearch();
    if (name === "sources") loadSources();
    if (name === "saved") renderSaved();
    if (name === "research") renderProjects();
    if (name === "phd") renderPhd();
    if (name === "tasks") renderTasks();
  }

  // ---------------- Health / Sources ----------------
  async function checkHealth() {
    const pill = $("#api-status");
    try {
      const d = await API.health();
      pill.className = "status-pill online";
      pill.innerHTML = `<span class="dot"></span>${d.online || 0}/${d.sources || 0} sources online`;
    } catch {
      pill.className = "status-pill offline";
      pill.innerHTML = `<span class="dot"></span>offline`;
    }
  }

  async function ensureSources() {
    if (state.sources.length) return state.sources;
    try {
      const d = await API.sources();
      state.sources = d.sources || [];
    } catch { state.sources = []; }
    // Populate source select
    const sel = $("#source-select");
    if (sel && sel.options.length <= 1) {
      state.sources.forEach(s => {
        const o = document.createElement("option");
        o.value = s.name; o.textContent = s.name;
        sel.appendChild(o);
      });
    }
    return state.sources;
  }

  async function loadSources() {
    const list = $("#sources-list");
    list.innerHTML = '<div class="skeleton card"></div><div class="skeleton card"></div><div class="skeleton card"></div>';
    try {
      const d = await API.sources();
      state.sources = d.sources || [];
      if (!state.sources.length) {
        list.innerHTML = '<p class="muted">No sources configured.</p>'; return;
      }
      list.innerHTML = state.sources.map(s => {
        const status = s.status || "unknown";
        const badgeCls = status === "online" ? "green" : status === "error" ? "orange" : "blue";
        const method = s.method || "unknown";
        const latency = s.latency_ms != null ? `${s.latency_ms} ms` : "—";
        return `
        <div class="card">
          <div class="card-top">
            <div class="src">${esc(s.name)}</div>
            <span class="badge ${badgeCls}">● ${esc(status)}</span>
          </div>
          <p class="desc">${esc(s.country)}</p>
          <div class="meta">
            <span class="badge">${esc(method)}</span>
            <span class="badge">${s.count || 0} cyber opps</span>
            <span class="badge muted">${esc(latency)}</span>
            <span class="badge muted">Checked ${relTime(s.last_checked)}</span>
          </div>
          ${s.error ? `<div class="meta"><span class="badge orange" title="${esc(s.error)}">error: ${esc(s.error)}</span></div>` : ""}
          <div class="actions"><a href="${esc(s.base_url)}" target="_blank" rel="noopener">Open source</a></div>
        </div>`;
      }).join("");
    } catch (e) {
      list.innerHTML = `<p class="muted">Could not load sources: ${esc(e.message)}</p>`;
    }
  }

  // ---------------- Filtering & Sorting ----------------
  function matchFilters(o) {
    const f = state.filters;
    const hay = (o.title + " " + o.description + " " + (o.matched_terms || []).join(" ")).toLowerCase();
    if (f.area.size) {
      if (![...f.area].some(a => hay.includes(a.toLowerCase()))) return false;
    }
    if (f.type.size) {
      if (![...f.type].some(t => (o.funding_type || "").toLowerCase().includes(t.toLowerCase()))) return false;
    }
    if (f.elig.size) {
      if (![...f.elig].some(e => (o.bhutan_relevance || "").toLowerCase().includes(e.toLowerCase()))) return false;
    }
    if (f.status.size) {
      const isOpen = o.is_open;
      const isClosing = o.days_left != null && o.days_left >= 0 && o.days_left <= 30;
      const ok = [...f.status].some(s =>
        (s === "Open" && isOpen) || (s === "Closing Soon" && isClosing)
      );
      if (!ok) return false;
    }
    return true;
  }

  function sortResults(list) {
    const copy = list.slice();
    switch (state.sort) {
      case "deadline":
        copy.sort((a,b) => {
          const av = a.days_left ?? 10**9, bv = b.days_left ?? 10**9;
          return av - bv;
        });
        break;
      case "source":
        copy.sort((a,b) => (a.source||"").localeCompare(b.source||"") || (a.title||"").localeCompare(b.title||""));
        break;
      case "title":
        copy.sort((a,b) => (a.title||"").localeCompare(b.title||""));
        break;
      default:
        copy.sort((a,b) => (b.cybersecurity_relevance - a.cybersecurity_relevance));
    }
    return copy;
  }

  // ---------------- Cards ----------------
  function relClass(score) {
    if (score >= 70) return "green";
    if (score >= 40) return "blue";
    return "orange";
  }

  function opportunityCard(o) {
    const isSaved = state.saved.some(x => x.id === o.id);
    const terms = (o.matched_terms || []).slice(0, 3).join(" · ");
    const deadlineBadge = o.deadline
      ? (o.days_left != null && o.days_left < 0
          ? `<span class="badge muted">Closed ${fmtDate(o.deadline)}</span>`
          : (o.days_left != null && o.days_left <= 30
              ? `<span class="badge orange">Closing in ${o.days_left}d · ${fmtDate(o.deadline)}</span>`
              : `<span class="badge">Deadline ${fmtDate(o.deadline)}</span>`))
      : `<span class="badge muted">Rolling / no deadline listed</span>`;

    return `
      <article class="card" data-id="${esc(o.id)}">
        <div class="card-top">
          <div class="src">${esc(o.source)}</div>
          <div class="rel" title="Cybersecurity relevance">
            <span class="rel-bar"><i style="width:${o.cybersecurity_relevance}%"></i></span>
            ${o.cybersecurity_relevance}%
          </div>
        </div>
        <h3 class="title">${esc(o.title)}</h3>
        <p class="desc">${esc(o.summary || o.description || "").slice(0, 240)}</p>
        <div class="meta">
          <span class="badge ${relClass(o.cybersecurity_relevance)}">${esc(o.funding_type)}</span>
          <span class="badge">${esc(o.country)}</span>
          <span class="badge">${esc(o.bhutan_relevance)}</span>
        </div>
        ${terms ? `<div class="meta"><span class="badge blue">Matched: ${esc(terms)}</span></div>` : ""}
        <div class="meta">${deadlineBadge}</div>
        <div class="actions">
          <button class="open-detail" data-id="${esc(o.id)}">Details</button>
          <a class="primary" href="${esc(o.url)}" target="_blank" rel="noopener">View official</a>
          <button class="save-btn ${isSaved ? "saved" : ""}" data-id="${esc(o.id)}">${isSaved ? "Saved ✓" : "Save"}</button>
        </div>
      </article>`;
  }

  function renderResults(list) {
    const el = $("#opp-list");
    if (!list.length) {
      el.innerHTML = `<div class="panel"><p class="muted">No cybersecurity opportunities matched. Try another keyword or clear filters.</p></div>`;
      return;
    }
    el.innerHTML = list.map(opportunityCard).join("");
    bindCards(el, list);
  }

  function bindCards(root, list) {
    $$(".save-btn", root).forEach(btn => {
      btn.addEventListener("click", () => toggleSave(btn.dataset.id, list, root));
    });
    $$(".open-detail", root).forEach(btn => {
      btn.addEventListener("click", () => openDrawer(btn.dataset.id, list));
    });
  }

  function toggleSave(id, list, root) {
    const opp = (list || state.results).find(x => x.id === id) || state.saved.find(x => x.id === id);
    if (!opp) return;
    const idx = state.saved.findIndex(x => x.id === id);
    if (idx >= 0) { state.saved.splice(idx, 1); toast("Removed"); }
    else { state.saved.push(opp); toast("Saved"); }
    saveJSON(LS.saved, state.saved);
    // Update just the affected button
    const btn = root && $(`.save-btn[data-id="${id}"]`, root);
    if (btn) {
      const saved = state.saved.some(x => x.id === id);
      btn.classList.toggle("saved", saved);
      btn.textContent = saved ? "Saved ✓" : "Save";
    }
    if (!$("#view-saved").classList.contains("hidden")) renderSaved();
    if (state.drawerOpp && state.drawerOpp.id === id) syncDrawerSave();
  }

  // ---------------- Drawer ----------------
  const drawer = $("#detail-drawer");
  const drawerBackdrop = $("#drawer-backdrop");

  function openDrawer(id, list) {
    const o = (list || state.results).find(x => x.id === id) || state.saved.find(x => x.id === id);
    if (!o) return;
    state.drawerOpp = o;
    $("#detail-source").textContent = `${o.source} · ${o.source_method || ""}`;
    $("#detail-title").textContent = o.title;
    const b = o.relevance_breakdown || {};
    const bars = [
      ["Title · strong", b.title_strong, 25],
      ["Title · medium", b.title_medium, 18],
      ["Title · weak",   b.title_weak, 12],
      ["Desc · strong",  b.desc_strong, 6],
      ["Desc · medium",  b.desc_medium, 4],
      ["Desc · weak",    b.desc_weak, 2],
    ].filter(([,v]) => v > 0);

    $("#detail-body").innerHTML = `
      <div class="detail-block">
        <h4>Summary</h4>
        <p>${esc(o.description || o.summary || "No description available.")}</p>
      </div>

      <div class="detail-block">
        <h4>Metadata</h4>
        <dl class="kv">
          <dt>Funding type</dt><dd>${esc(o.funding_type)}</dd>
          <dt>Opportunity type</dt><dd>${esc(o.opportunity_type || o.funding_type)}</dd>
          <dt>Country / region</dt><dd>${esc(o.country)}</dd>
          <dt>Deadline</dt><dd>${o.deadline ? fmtDate(o.deadline) : "—"} ${o.days_left != null ? `(${o.days_left} days)` : ""}</dd>
          <dt>Open date</dt><dd>${o.open_date ? fmtDate(o.open_date) : "—"}</dd>
          <dt>Posted</dt><dd>${o.posted_date ? fmtDate(o.posted_date) : "—"}</dd>
          <dt>Award amount</dt><dd>${o.award_amount ? esc(o.award_amount) : "—"}</dd>
          <dt>Eligibility</dt><dd>${o.eligibility ? esc(o.eligibility) : "See official call"}</dd>
          <dt>Bhutan relevance</dt><dd>${esc(o.bhutan_relevance)}</dd>
          <dt>Official source</dt><dd>${o.official_source ? "Yes" : "Unverified"}</dd>
          <dt>Confidence</dt><dd>${esc(o.confidence || "—")}</dd>
          <dt>Last checked</dt><dd>${relTime(o.last_checked)}</dd>
        </dl>
      </div>

      <div class="detail-block">
        <h4>Cybersecurity relevance · ${o.cybersecurity_relevance}%</h4>
        <div class="term-bars">
          ${bars.length ? bars.map(([label, val, w]) => `
            <div class="term-row">
              <span class="name">${esc(label)}</span>
              <span class="bar"><i style="width:${Math.min(100, (val*w))}%"></i></span>
              <span class="val">×${val}</span>
            </div>`).join("") : `<p class="muted">No breakdown available.</p>`}
        </div>
      </div>

      <div class="detail-block">
        <h4>Matched terms</h4>
        <div class="meta">
          ${(o.matched_terms || []).map(t => `<span class="badge blue">${esc(t)}</span>`).join("") || `<span class="muted">—</span>`}
        </div>
      </div>
    `;
    $("#detail-link").href = o.url || "#";
    syncDrawerSave();
    drawer.classList.add("open");
    drawerBackdrop.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
  }

  function syncDrawerSave() {
    const o = state.drawerOpp;
    if (!o) return;
    const saved = state.saved.some(x => x.id === o.id);
    const btn = $("#detail-save");
    btn.textContent = saved ? "Saved ✓" : "Save";
    btn.classList.toggle("saved", saved);
  }

  function closeDrawer() {
    state.drawerOpp = null;
    drawer.classList.remove("open");
    drawerBackdrop.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  }

  $("#detail-close").addEventListener("click", closeDrawer);
  drawerBackdrop.addEventListener("click", closeDrawer);
  $("#detail-save").addEventListener("click", () => {
    if (state.drawerOpp) toggleSave(state.drawerOpp.id, state.results, null);
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && drawer.classList.contains("open")) closeDrawer();
  });

  // ---------------- Search ----------------
  async function runSearch() {
    const el = $("#opp-list");
    const meta = $("#result-meta");
    meta.textContent = `Searching ${state.sources.length || "30"} sources…`;
    el.innerHTML = '<div class="skeleton card"></div><div class="skeleton card"></div><div class="skeleton card"></div><div class="skeleton card"></div>';
    try {
      const data = await API.search(state.keyword, state.source, state.closingSoon);
      state.results = data.results || [];
      state.sources = state.sources.length ? state.sources : (await ensureSources(), state.sources);
      const filtered = sortResults(state.results.filter(matchFilters));
      const errs = data.errors || [];
      meta.textContent = `Found ${filtered.length} cybersecurity opportunities · ${data.sources_queried || 0} sources queried${errs.length ? ` · ${errs.length} source error(s)` : ""}`;
      renderResults(filtered);
      updateStats(data);
    } catch (e) {
      meta.textContent = "Search failed.";
      el.innerHTML = `<div class="panel"><p class="muted">${esc(e.message)}</p></div>`;
    }
  }

  // ---------------- Dashboard ----------------
  async function loadDashboard() {
    const sl = $("#source-status-list");
    sl.innerHTML = '<div class="skeleton row"></div><div class="skeleton row"></div><div class="skeleton row"></div>';
    try {
      const d = await API.sources();
      state.sources = d.sources || [];
      const top = state.sources.slice(0, 12);
      sl.innerHTML = top.length ? top.map(s => `
        <div class="row">
          <span class="name"><span class="dot ${esc(s.status||"unknown")}"></span><span>${esc(s.name)}</span></span>
          <span class="count">${s.count || 0}</span>
        </div>`).join("") : `<p class="muted">No sources.</p>`;
    } catch { sl.innerHTML = '<p class="muted">Source status unavailable.</p>'; }

    const live = $("#live-opps");
    live.innerHTML = '<div class="skeleton card"></div><div class="skeleton card"></div><div class="skeleton card"></div>';
    try {
      const data = await API.search(state.keyword, "", state.closingSoon);
      state.results = data.results || [];
      updateStats(data);
      const top = sortResults(state.results).slice(0, 6);
      if (!top.length) {
        live.innerHTML = `<p class="muted">No cybersecurity opportunities found.</p>`;
      } else {
        live.innerHTML = top.map(opportunityCard).join("");
        bindCards(live, top);
      }
    } catch (e) {
      live.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    }
  }

  function updateStats(data) {
    $("#stat-sources").textContent = (data && data.sources_total) || state.sources.length || "—";
    $("#stat-sources-sub").textContent = state.sources.length
      ? `${state.sources.filter(s => s.status === "online").length} online`
      : "—";
    const list = (data && data.results) || [];
    $("#stat-opps").textContent = list.length;
    $("#stat-opps-sub").textContent = `live results · ${data && data.keyword ? "kw: " + data.keyword : ""}`.trim();
    const open = list.filter(o => o.is_open).length;
    $("#stat-open").textContent = open;
    const soon = list.filter(o => o.days_left != null && o.days_left >= 0 && o.days_left <= 30).length;
    $("#stat-deadlines").textContent = soon;
  }

  // ---------------- Saved ----------------
  function renderSaved() {
    const el = $("#saved-list");
    if (!state.saved.length) {
      el.innerHTML = `<div class="panel"><p class="muted">No saved opportunities yet. Open an opportunity and click Save.</p></div>`;
      return;
    }
    el.innerHTML = state.saved.map(opportunityCard).join("");
    bindCards(el, state.saved);
  }

  // ---------------- Generic modal ----------------
  const modal = $("#modal");
  let modalOnSave = null;
  function openModal(title, fields, onSave, initial = {}) {
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = fields.map(f => {
      const v = initial[f.name] ?? "";
      if (f.type === "textarea")
        return `<label>${esc(f.label)}</label><textarea name="${esc(f.name)}">${esc(v)}</textarea>`;
      if (f.type === "select")
        return `<label>${esc(f.label)}</label><select name="${esc(f.name)}">${f.options.map(o => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
      return `<label>${esc(f.label)}</label><input name="${esc(f.name)}" type="${esc(f.type || "text")}" value="${esc(v)}" />`;
    }).join("");
    modalOnSave = () => {
      const data = {};
      $$("#modal-body [name]").forEach(inp => { data[inp.name] = inp.value; });
      onSave(data); closeModal();
    };
    modal.classList.add("open");
  }
  function closeModal() { modal.classList.remove("open"); modalOnSave = null; }
  $("#modal-close").addEventListener("click", closeModal);
  $("#modal-cancel").addEventListener("click", closeModal);
  $("#modal-save").addEventListener("click", () => { if (modalOnSave) modalOnSave(); });

  // ---------------- Research Projects ----------------
  const projectFields = [
    { name: "title", label: "Title" },
    { name: "status", label: "Status", type: "select", options: ["Active","Planning","On Hold","Completed"] },
    { name: "area", label: "Research area" },
    { name: "funding", label: "Funding" },
    { name: "institution", label: "Institution" },
    { name: "milestones", label: "Milestones", type: "textarea" },
    { name: "deadline", label: "Deadline", type: "date" },
    { name: "notes", label: "Notes", type: "textarea" },
  ];

  function renderProjects() {
    const el = $("#projects-list");
    if (!state.projects.length) {
      el.innerHTML = `<div class="panel"><p class="muted">No research projects yet. Add your first project to track milestones and funding.</p></div>`;
      return;
    }
    el.innerHTML = state.projects.map(p => `
      <div class="row-card" data-id="${esc(p.id)}">
        <div class="row-head">
          <h3>${esc(p.title || "(untitled project)")}</h3>
          <div class="row-actions">
            <button data-act="edit">Edit</button>
            <button data-act="del" class="danger">Delete</button>
          </div>
        </div>
        <div class="row-body">
          <div><strong>Status:</strong> ${esc(p.status || "—")} · <strong>Area:</strong> ${esc(p.area || "—")}</div>
          <div><strong>Funding:</strong> ${esc(p.funding || "—")} · <strong>Institution:</strong> ${esc(p.institution || "—")}</div>
          ${p.milestones ? `<div><strong>Milestones:</strong> ${esc(p.milestones)}</div>` : ""}
          ${p.deadline ? `<div><strong>Deadline:</strong> ${esc(p.deadline)}</div>` : ""}
          ${p.notes ? `<div><strong>Notes:</strong> ${esc(p.notes)}</div>` : ""}
        </div>
      </div>`).join("");
    $$("#projects-list .row-card").forEach(card => {
      const id = card.dataset.id;
      card.querySelector('[data-act="edit"]').onclick = () => projectModal(id);
      card.querySelector('[data-act="del"]').onclick = () => {
        state.projects = state.projects.filter(x => x.id !== id);
        saveJSON(LS.projects, state.projects); renderProjects(); toast("Deleted");
      };
    });
  }
  function projectModal(id) {
    const p = state.projects.find(x => x.id === id) || {};
    openModal(id ? "Edit project" : "Add project", projectFields, data => {
      if (id) Object.assign(p, data); else state.projects.push({ id: uid(), ...data });
      saveJSON(LS.projects, state.projects); renderProjects(); toast("Saved");
    }, p);
  }
  $("#add-project").addEventListener("click", () => projectModal(null));

  // ---------------- PhD ----------------
  const phdFields = [
    { name: "university", label: "University" },
    { name: "country", label: "Country" },
    { name: "professor", label: "Professor" },
    { name: "area", label: "Research area" },
    { name: "funding", label: "Funding" },
    { name: "tuition", label: "Tuition coverage" },
    { name: "stipend", label: "Stipend" },
    { name: "deadline", label: "Deadline", type: "date" },
    { name: "status", label: "Application status", type: "select", options: ["Researching","Preparing","Submitted","Interview","Offer","Rejected"] },
    { name: "contacted", label: "Supervisor contacted?", type: "select", options: ["No","Yes"] },
    { name: "submitted", label: "Application submitted?", type: "select", options: ["No","Yes"] },
  ];

  function renderPhd() {
    const el = $("#phd-list");
    if (!state.phd.length) {
      el.innerHTML = `<div class="panel"><p class="muted">No PhD opportunities yet.</p></div>`;
      return;
    }
    el.innerHTML = state.phd.map(p => `
      <div class="row-card" data-id="${esc(p.id)}">
        <div class="row-head">
          <h3>${esc(p.university || "University")} — ${esc(p.area || "PhD")}</h3>
          <div class="row-actions">
            <button data-act="edit">Edit</button>
            <button data-act="del" class="danger">Delete</button>
          </div>
        </div>
        <div class="row-body">
          <div><strong>Country:</strong> ${esc(p.country || "—")} · <strong>Professor:</strong> ${esc(p.professor || "—")}</div>
          <div><strong>Funding:</strong> ${esc(p.funding || "—")} · <strong>Tuition:</strong> ${esc(p.tuition || "—")} · <strong>Stipend:</strong> ${esc(p.stipend || "—")}</div>
          <div><strong>Deadline:</strong> ${esc(p.deadline || "—")} · <strong>Status:</strong> ${esc(p.status || "—")}</div>
          <div><strong>Supervisor contacted:</strong> ${esc(p.contacted || "No")} · <strong>Application submitted:</strong> ${esc(p.submitted || "No")}</div>
        </div>
      </div>`).join("");
    $$("#phd-list .row-card").forEach(card => {
      const id = card.dataset.id;
      card.querySelector('[data-act="edit"]').onclick = () => phdModal(id);
      card.querySelector('[data-act="del"]').onclick = () => {
        state.phd = state.phd.filter(x => x.id !== id);
        saveJSON(LS.phd, state.phd); renderPhd(); toast("Deleted");
      };
    });
  }
  function phdModal(id) {
    const p = state.phd.find(x => x.id === id) || {};
    openModal(id ? "Edit PhD" : "Add PhD", phdFields, data => {
      if (id) Object.assign(p, data); else state.phd.push({ id: uid(), ...data });
      saveJSON(LS.phd, state.phd); renderPhd(); toast("Saved");
    }, p);
  }
  $("#add-phd").addEventListener("click", () => phdModal(null));

  // ---------------- Tasks ----------------
  const taskFields = [
    { name: "title", label: "Task" },
    { name: "due", label: "Due date", type: "date" },
    { name: "priority", label: "Priority", type: "select", options: ["Low","Medium","High","Urgent"] },
    { name: "status", label: "Status", type: "select", options: ["Open","In Progress","Done"] },
    { name: "notes", label: "Notes", type: "textarea" },
  ];

  function renderTasks() {
    const el = $("#tasks-list");
    if (!state.tasks.length) {
      el.innerHTML = `<div class="panel"><p class="muted">No tasks yet. Add one to track proposals, EOIs and follow-ups.</p></div>`;
      return;
    }
    el.innerHTML = state.tasks.map(t => `
      <div class="row-card" data-id="${esc(t.id)}">
        <div class="row-head">
          <h3>${esc(t.title || "(untitled task)")}</h3>
          <div class="row-actions">
            <button data-act="done">${t.status === "Done" ? "Reopen" : "Mark done"}</button>
            <button data-act="edit">Edit</button>
            <button data-act="del" class="danger">Delete</button>
          </div>
        </div>
        <div class="row-body">
          <div><strong>Due:</strong> ${esc(t.due || "—")} · <strong>Priority:</strong> ${esc(t.priority || "Medium")} · <strong>Status:</strong> ${esc(t.status || "Open")}</div>
          ${t.notes ? `<div><strong>Notes:</strong> ${esc(t.notes)}</div>` : ""}
        </div>
      </div>`).join("");
    $$("#tasks-list .row-card").forEach(card => {
      const id = card.dataset.id;
      const t = state.tasks.find(x => x.id === id);
      card.querySelector('[data-act="edit"]').onclick = () => taskModal(id);
      card.querySelector('[data-act="del"]').onclick = () => {
        state.tasks = state.tasks.filter(x => x.id !== id);
        saveJSON(LS.tasks, state.tasks); renderTasks(); toast("Deleted");
      };
      card.querySelector('[data-act="done"]').onclick = () => {
        t.status = t.status === "Done" ? "Open" : "Done";
        saveJSON(LS.tasks, state.tasks); renderTasks();
      };
    });
  }
  function taskModal(id) {
    const t = state.tasks.find(x => x.id === id) || {};
    openModal(id ? "Edit task" : "Add task", taskFields, data => {
      if (id) Object.assign(t, data); else state.tasks.push({ id: uid(), ...data });
      saveJSON(LS.tasks, state.tasks); renderTasks(); toast("Saved");
    }, t);
  }
  $("#add-task").addEventListener("click", () => taskModal(null));

  // ---------------- Filter wiring ----------------
  function bindFilters() {
    $$("input[data-filter]").forEach(cb => {
      cb.addEventListener("change", () => {
        const group = cb.dataset.filter;
        const set = state.filters[group];
        if (cb.checked) set.add(cb.value); else set.delete(cb.value);
        const filtered = sortResults(state.results.filter(matchFilters));
        renderResults(filtered);
        $("#result-meta").textContent = `Found ${filtered.length} cybersecurity opportunities`;
      });
    });
    $("#clear-filters").addEventListener("click", () => {
      state.filters = { area: new Set(), type: new Set(), elig: new Set(), status: new Set() };
      $$("input[data-filter]").forEach(cb => cb.checked = false);
      const filtered = sortResults(state.results);
      renderResults(filtered);
      $("#result-meta").textContent = `Found ${filtered.length} cybersecurity opportunities`;
    });
    $("#filter-fab").addEventListener("click", () => {
      $("#filter-sidebar").classList.add("open");
      $("#filter-backdrop").classList.add("open");
    });
    $("#filter-backdrop").addEventListener("click", () => {
      $("#filter-sidebar").classList.remove("open");
      $("#filter-backdrop").classList.remove("open");
    });
  }

  // ---------------- Init ----------------
  function init() {
    $("#hamburger").addEventListener("click", () => $("#topnav").classList.toggle("open"));
    window.addEventListener("hashchange", route);

    $("#dash-search-form").addEventListener("submit", e => {
      e.preventDefault();
      state.keyword = $("#dash-search-input").value.trim() || "cybersecurity";
      location.hash = "#/opportunities";
    });
    $("#opp-search-form").addEventListener("submit", e => {
      e.preventDefault();
      state.keyword = $("#opp-search-input").value.trim() || "cybersecurity";
      runSearch();
    });
    $("#refresh-btn").addEventListener("click", loadDashboard);

    const closingBtn = $("#dash-closing-btn");
    closingBtn.addEventListener("click", () => {
      state.closingSoon = !state.closingSoon;
      closingBtn.textContent = state.closingSoon ? "Closing soon ✓" : "Closing soon";
      loadDashboard();
    });

    const sourceSel = $("#source-select");
    sourceSel.addEventListener("change", () => {
      state.source = sourceSel.value;
      runSearch();
    });

    const sortSel = $("#sort-select");
    sortSel.addEventListener("change", () => {
      state.sort = sortSel.value;
      const filtered = sortResults(state.results.filter(matchFilters));
      renderResults(filtered);
    });

    $("#wipe-local").addEventListener("click", () => {
      if (!confirm("Clear all locally saved data?")) return;
      Object.values(LS).forEach(k => localStorage.removeItem(k));
      state.saved = []; state.projects = []; state.phd = []; state.tasks = [];
      renderProjects(); renderPhd(); renderTasks(); renderSaved();
      toast("Local data cleared");
    });

    // Keyboard shortcuts
    document.addEventListener("keydown", e => {
      const t = e.target;
      const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
      if (typing) return;
      if (e.key === "/") {
        e.preventDefault();
        const input = location.hash.includes("opportunities") ? $("#opp-search-input") : $("#dash-search-input");
        if (input) input.focus();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        const input = $("#dash-search-input"); if (input) input.focus();
      }
    });
    $("#kbd-hint").addEventListener("click", () => {
      const input = $("#dash-search-input"); if (input) input.focus();
    });

    bindFilters();
    checkHealth();
    if (!location.hash) location.hash = "#/dashboard";
    route();
  }

  document.addEventListener("DOMContentLoaded", init);
})();