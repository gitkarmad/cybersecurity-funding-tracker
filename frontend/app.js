/* Cybersecurity Research & Funding Tracker — frontend logic */
(() => {
  "use strict";

  const API = {
    search: (kw, source) => {
      const p = new URLSearchParams();
      if (kw) p.set("keyword", kw);
      if (source) p.set("source", source);
      return fetch(`/api/search?${p.toString()}`).then(r => r.json());
    },
    sources: () => fetch("/api/sources").then(r => r.json()),
    health: () => fetch("/api/health").then(r => r.json()),
  };

  const LS = {
    saved: "csft.saved",
    projects: "csft.projects",
    phd: "csft.phd",
    tasks: "csft.tasks",
  };

  const state = {
    keyword: "cybersecurity",
    source: "",
    sources: [],
    results: [],
    filters: { area: new Set(), type: new Set(), elig: new Set(), status: new Set() },
    saved: loadJSON(LS.saved, []),
    projects: loadJSON(LS.projects, []),
    phd: loadJSON(LS.phd, []),
    tasks: loadJSON(LS.tasks, []),
  };

  // -------------------- utils --------------------
  function loadJSON(k, fallback) {
    try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : fallback; }
    catch { return fallback; }
  }
  function saveJSON(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} }
  function $(sel, root = document) { return root.querySelector(sel); }
  function $$(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }
  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }
  function fmtDate(d) {
    if (!d) return "—";
    const dt = new Date(d);
    if (isNaN(dt.getTime())) return esc(d);
    return dt.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
  }
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove("show"), 2400);
  }
  function uid() { return Math.random().toString(36).slice(2, 10); }

  // -------------------- routing --------------------
  const routes = ["dashboard", "opportunities", "sources", "saved", "research", "phd", "tasks", "settings"];
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

  // -------------------- health/sources --------------------
  async function checkHealth() {
    try {
      await API.health();
      $("#api-status").className = "status-dot online";
    } catch {
      $("#api-status").className = "status-dot offline";
    }
  }

  async function loadSources() {
    const list = $("#sources-list");
    list.innerHTML = '<div class="skeleton card"></div><div class="skeleton card"></div>';
    try {
      const data = await API.sources();
      state.sources = data.sources || [];
      list.innerHTML = state.sources.map(s => `
        <div class="card">
          <div class="src">${esc(s.name)}</div>
          <p class="desc">${esc(s.country)} · ${esc(s.method)}</p>
          <div class="meta">
            <span class="badge ${s.status === "online" ? "green" : s.status === "error" ? "orange" : "blue"}">● ${esc(s.status || "unknown")}</span>
            <span class="badge">${s.count || 0} cybersecurity opps</span>
            <span class="badge">Last checked: ${s.last_checked ? new Date(s.last_checked).toLocaleTimeString() : "—"}</span>
          </div>
          <div class="actions"><a href="${esc(s.base_url)}" target="_blank" rel="noopener">Open Source</a></div>
        </div>
      `).join("");
    } catch (e) {
      list.innerHTML = `<p class="muted">Could not load sources: ${esc(e.message)}</p>`;
    }
  }

  // -------------------- search --------------------
  function matchFilters(o) {
    const f = state.filters;
    const titleDesc = (o.title + " " + o.description).toLowerCase();
    if (f.area.size) {
      const ok = [...f.area].some(a => titleDesc.includes(a.toLowerCase()) || o.matched_terms.join(" ").toLowerCase().includes(a.toLowerCase()));
      if (!ok) return false;
    }
    if (f.type.size) {
      const ok = [...f.type].some(t => o.funding_type.toLowerCase().includes(t.toLowerCase()));
      if (!ok) return false;
    }
    if (f.elig.size) {
      const ok = [...f.elig].some(e => o.bhutan_relevance.toLowerCase().includes(e.toLowerCase()));
      if (!ok) return false;
    }
    if (f.status.size) {
      const d = o.deadline ? new Date(o.deadline) : null;
      const now = new Date();
      const isOpen = !d || d > now;
      const isClosing = d && d > now && (d - now) / 86400000 <= 30;
      const ok = [...f.status].some(s => (s === "Open" && isOpen) || (s === "Closing Soon" && isClosing));
      if (!ok) return false;
    }
    return true;
  }

  function opportunityCard(o) {
    const isSaved = state.saved.some(x => x.id === o.id);
    const rel = o.cybersecurity_relevance >= 70 ? "green" : o.cybersecurity_relevance >= 40 ? "blue" : "orange";
    const terms = (o.matched_terms || []).slice(0, 4).join(" · ");
    return `
      <div class="card" data-id="${esc(o.id)}">
        <div class="src">${esc(o.source)}</div>
        <h3 class="title">${esc(o.title)}</h3>
        <p class="desc">${esc((o.description || "").slice(0, 220))}${o.description && o.description.length > 220 ? "…" : ""}</p>
        <div class="meta">
          <span class="badge">${esc(o.funding_type)}</span>
          <span class="badge">${esc(o.country)}</span>
          <span class="badge ${rel}">Relevance ${o.cybersecurity_relevance}%</span>
          <span class="badge">${esc(o.bhutan_relevance)}</span>
        </div>
        ${terms ? `<div class="meta"><span class="badge blue">Matched: ${esc(terms)}</span></div>` : ""}
        <div class="meta"><span class="badge">Deadline: ${fmtDate(o.deadline)}</span></div>
        <div class="actions">
          <a href="${esc(o.url)}" target="_blank" rel="noopener" class="primary">View Official Source</a>
          <button class="save-btn ${isSaved ? "saved" : ""}" data-id="${esc(o.id)}">${isSaved ? "Saved ✓" : "Save"}</button>
        </div>
      </div>`;
  }

  function renderResults(list) {
    const el = $("#opp-list");
    if (!list.length) {
      el.innerHTML = '<p class="muted">No cybersecurity opportunities matched. Try a different keyword.</p>';
      return;
    }
    el.innerHTML = list.map(opportunityCard).join("");
    bindSaveButtons(el);
  }

  function bindSaveButtons(root) {
    $$(".save-btn", root).forEach(btn => {
      btn.addEventListener("click", () => {
        const id = btn.dataset.id;
        const opp = state.results.find(o => o.id === id) || state.saved.find(o => o.id === id);
        if (!opp) return;
        const idx = state.saved.findIndex(x => x.id === id);
        if (idx >= 0) {
          state.saved.splice(idx, 1);
          toast("Removed from saved");
        } else {
          state.saved.push(opp);
          toast("Saved");
        }
        saveJSON(LS.saved, state.saved);
        bindSaveButtons(root);
        btn.classList.toggle("saved");
        btn.textContent = state.saved.some(x => x.id === id) ? "Saved ✓" : "Save";
      });
    });
  }

  async function runSearch() {
    const el = $("#opp-list");
    const meta = $("#result-meta");
    meta.textContent = `Searching ${state.sources.length || "30"} sources…`;
    el.innerHTML = '<div class="skeleton card"></div><div class="skeleton card"></div><div class="skeleton card"></div><div class="skeleton card"></div>';
    try {
      const data = await API.search(state.keyword, state.source);
      state.results = data.results || [];
      const filtered = state.results.filter(matchFilters);
      meta.textContent = `Found ${filtered.length} cybersecurity opportunities${data.errors && data.errors.length ? ` (${data.errors.length} source errors)` : ""}`;
      renderResults(filtered);
      updateStats(data);
    } catch (e) {
      meta.textContent = "Search failed.";
      el.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    }
  }

  // -------------------- dashboard --------------------
  async function loadDashboard() {
    // Sources status
    const sl = $("#source-status-list");
    sl.innerHTML = '<div class="skeleton row"></div><div class="skeleton row"></div><div class="skeleton row"></div>';
    try {
      const sdata = await API.sources();
      state.sources = sdata.sources || [];
      sl.innerHTML = state.sources.slice(0, 12).map(s => `
        <div class="row">
          <span class="name"><span class="dot ${esc(s.status || "unknown")}"></span>${esc(s.name)}</span>
          <span class="count">${s.count || 0}</span>
        </div>
      `).join("");
    } catch { sl.innerHTML = '<p class="muted">Source status unavailable.</p>'; }

    // Live opportunities
    const live = $("#live-opps");
    live.innerHTML = '<div class="skeleton card"></div><div class="skeleton card"></div><div class="skeleton card"></div>';
    try {
      const data = await API.search(state.keyword, "");
      state.results = data.results || [];
      updateStats(data);
      const top = state.results.slice(0, 6);
      live.innerHTML = top.length ? top.map(opportunityCard).join("") : '<p class="muted">No cybersecurity opportunities found.</p>';
      bindSaveButtons(live);
    } catch (e) {
      live.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    }
  }

  function updateStats(data) {
    $("#stat-sources").textContent = (data && data.sources_total) || state.sources.length || "30";
    $("#stat-opps").textContent = (data && data.count) || 0;
    const now = new Date();
    const open = (data && data.results || []).filter(o => !o.deadline || new Date(o.deadline) > now).length;
    const soon = (data && data.results || []).filter(o => o.deadline && (new Date(o.deadline) - now) > 0 && (new Date(o.deadline) - now) / 86400000 <= 30).length;
    $("#stat-open").textContent = open;
    $("#stat-deadlines").textContent = soon;
  }

  // -------------------- saved --------------------
  function renderSaved() {
    const el = $("#saved-list");
    if (!state.saved.length) { el.innerHTML = '<p class="muted">No saved opportunities yet.</p>'; return; }
    el.innerHTML = state.saved.map(opportunityCard).join("");
    bindSaveButtons(el);
  }

  // -------------------- generic modal --------------------
  const modal = $("#modal");
  let modalOnSave = null;
  function openModal(title, fields, onSave, initial = {}) {
    $("#modal-title").textContent = title;
    const body = $("#modal-body");
    body.innerHTML = fields.map(f => {
      const v = initial[f.name] ?? "";
      if (f.type === "textarea") return `<label>${esc(f.label)}</label><textarea name="${esc(f.name)}">${esc(v)}</textarea>`;
      if (f.type === "select") return `<label>${esc(f.label)}</label><select name="${esc(f.name)}">${f.options.map(o => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
      return `<label>${esc(f.label)}</label><input name="${esc(f.name)}" type="${esc(f.type || "text")}" value="${esc(v)}" />`;
    }).join("");
    modalOnSave = () => {
      const data = {};
      $$("#modal-body [name]").forEach(inp => { data[inp.name] = inp.value; });
      onSave(data);
      closeModal();
    };
    modal.classList.add("open");
  }
  function closeModal() { modal.classList.remove("open"); modalOnSave = null; }
  $("#modal-close").addEventListener("click", closeModal);
  $("#modal-cancel").addEventListener("click", closeModal);
  $("#modal-save").addEventListener("click", () => { if (modalOnSave) modalOnSave(); });

  // -------------------- research projects --------------------
  function renderProjects() {
    const el = $("#projects-list");
    if (!state.projects.length) { el.innerHTML = '<p class="muted">No research projects yet.</p>'; return; }
    el.innerHTML = state.projects.map(p => `
      <div class="row-card" data-id="${esc(p.id)}">
        <div class="row-head">
          <h3>${esc(p.title)}</h3>
          <div class="row-actions">
            <button data-act="edit">Edit</button>
            <button data-act="del" class="danger">Delete</button>
          </div>
        </div>
        <div class="row-body">
          <div><strong>Status:</strong> ${esc(p.status || "—")} · <strong>Area:</strong> ${esc(p.area || "—")}</div>
          <div><strong>Funding:</strong> ${esc(p.funding || "—")} · <strong>Institution:</strong> ${esc(p.institution || "—")}</div>
          <div><strong>Milestones:</strong> ${esc(p.milestones || "—")}</div>
          <div><strong>Deadlines:</strong> ${esc(p.deadline || "—")}</div>
          <div><strong>Notes:</strong> ${esc(p.notes || "—")}</div>
        </div>
      </div>`).join("");
    $$("#projects-list .row-card").forEach(card => {
      const id = card.dataset.id;
      card.querySelector('[data-act="edit"]').onclick = () => projectModal(id);
      card.querySelector('[data-act="del"]').onclick = () => {
        state.projects = state.projects.filter(p => p.id !== id);
        saveJSON(LS.projects, state.projects); renderProjects(); toast("Deleted");
      };
    });
  }
  function projectModal(id) {
    const p = state.projects.find(x => x.id === id) || {};
    openModal(id ? "Edit Project" : "Add Project", [
      { name: "title", label: "Title" },
      { name: "status", label: "Status", type: "select", options: ["Active", "Planning", "On Hold", "Completed"] },
      { name: "area", label: "Research Area" },
      { name: "funding", label: "Funding" },
      { name: "institution", label: "Institution" },
      { name: "milestones", label: "Milestones", type: "textarea" },
      { name: "deadline", label: "Deadline", type: "date" },
      { name: "notes", label: "Notes", type: "textarea" },
    ], data => {
      if (id) Object.assign(p, data);
      else state.projects.push({ id: uid(), ...data });
      saveJSON(LS.projects, state.projects); renderProjects(); toast("Saved");
    }, p);
  }
  $("#add-project").addEventListener("click", () => projectModal(null));

  // -------------------- PhD tracker --------------------
  function renderPhd() {
    const el = $("#phd-list");
    if (!state.phd.length) { el.innerHTML = '<p class="muted">No PhD opportunities yet.</p>'; return; }
    el.innerHTML = state.phd.map(p => `
      <div class="row-card" data-id="${esc(p.id)}">
        <div class="row-head">
          <h3>${esc(p.university)} — ${esc(p.area || "PhD")}</h3>
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
        state.phd = state.phd.filter(p => p.id !== id);
        saveJSON(LS.phd, state.phd); renderPhd(); toast("Deleted");
      };
    });
  }
  function phdModal(id) {
    const p = state.phd.find(x => x.id === id) || {};
    openModal(id ? "Edit PhD" : "Add PhD", [
      { name: "university", label: "University" },
      { name: "country", label: "Country" },
      { name: "professor", label: "Professor" },
      { name: "area", label: "Research Area" },
      { name: "funding", label: "Funding" },
      { name: "tuition", label: "Tuition Coverage" },
      { name: "stipend", label: "Stipend" },
      { name: "deadline", label: "Deadline", type: "date" },
      { name: "status", label: "Application Status", type: "select", options: ["Researching", "Preparing", "Submitted", "Interview", "Offer", "Rejected"] },
      { name: "contacted", label: "Supervisor contacted?", type: "select", options: ["No", "Yes"] },
      { name: "submitted", label: "Application submitted?", type: "select", options: ["No", "Yes"] },
    ], data => {
      if (id) Object.assign(p, data);
      else state.phd.push({ id: uid(), ...data });
      saveJSON(LS.phd, state.phd); renderPhd(); toast("Saved");
    }, p);
  }
  $("#add-phd").addEventListener("click", () => phdModal(null));

  // -------------------- Tasks --------------------
  function renderTasks() {
    const el = $("#tasks-list");
    if (!state.tasks.length) { el.innerHTML = '<p class="muted">No tasks yet.</p>'; return; }
    el.innerHTML = state.tasks.map(t => `
      <div class="row-card" data-id="${esc(t.id)}">
        <div class="row-head">
          <h3>${esc(t.title)}</h3>
          <div class="row-actions">
            <button data-act="done">${t.status === "Done" ? "Undo" : "Done"}</button>
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
    openModal(id ? "Edit Task" : "Add Task", [
      { name: "title", label: "Task" },
      { name: "due", label: "Due date", type: "date" },
      { name: "priority", label: "Priority", type: "select", options: ["Low", "Medium", "High", "Urgent"] },
      { name: "status", label: "Status", type: "select", options: ["Open", "In Progress", "Done"] },
      { name: "notes", label: "Notes", type: "textarea" },
    ], data => {
      if (id) Object.assign(t, data);
      else state.tasks.push({ id: uid(), ...data });
      saveJSON(LS.tasks, state.tasks); renderTasks(); toast("Saved");
    }, t);
  }
  $("#add-task").addEventListener("click", () => taskModal(null));

  // -------------------- filters --------------------
  function bindFilters() {
    $$("input[data-filter]").forEach(cb => {
      cb.addEventListener("change", () => {
        const group = cb.dataset.filter;
        const val = cb.value;
        const set = state.filters[group];
        if (cb.checked) set.add(val); else set.delete(val);
        const filtered = state.results.filter(matchFilters);
        renderResults(filtered);
        $("#result-meta").textContent = `Found ${filtered.length} cybersecurity opportunities`;
      });
    });
    $("#clear-filters").addEventListener("click", () => {
      state.filters = { area: new Set(), type: new Set(), elig: new Set(), status: new Set() };
      $$("input[data-filter]").forEach(cb => cb.checked = false);
      renderResults(state.results);
      $("#result-meta").textContent = `Found ${state.results.length} cybersecurity opportunities`;
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

  // -------------------- wire up --------------------
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

    $("#wipe-local").addEventListener("click", () => {
      if (!confirm("Clear all locally saved data?")) return;
      Object.values(LS).forEach(k => localStorage.removeItem(k));
      state.saved = []; state.projects = []; state.phd = []; state.tasks = [];
      renderProjects(); renderPhd(); renderTasks(); renderSaved(); toast("Local data cleared");
    });

    bindFilters();
    checkHealth();
    if (!location.hash) location.hash = "#/dashboard";
    route();
  }

  document.addEventListener("DOMContentLoaded", init);
})();