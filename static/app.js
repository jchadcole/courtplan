"use strict";
/* CourtPlan web UI: admin console, team portal and public live schedule. No build step. */

// ---------------------------------------------------------------- utilities
// replaceChildren() would render null as the text "null"; skip empty children like h() does.
const _replaceChildren = Element.prototype.replaceChildren;
Element.prototype.replaceChildren = function (...kids) {
  return _replaceChildren.apply(this, kids.flat(Infinity).filter(k => k !== null && k !== undefined && k !== false));
};
const $app = document.getElementById("app");
const $nav = document.getElementById("nav");
const S = { me: null, csrf: "", es: null, esKey: "" };

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const mount = (...els) => { $app.replaceChildren(...els); };

function toast(msg, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3500);
}

async function api(path, opts = {}) {
  const o = { method: opts.method || (opts.body ? "POST" : "GET"), headers: {}, credentials: "same-origin" };
  if (opts.body !== undefined) { o.body = JSON.stringify(opts.body); o.headers["Content-Type"] = "application/json"; }
  if (o.method !== "GET") o.headers["X-CSRF-Token"] = S.csrf;
  const r = await fetch(path, o);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    let msg = (data && data.detail) || r.statusText;
    if (Array.isArray(msg)) msg = msg.map(d => d.msg).join("; ");
    if (r.status === 401 && !path.startsWith("/api/auth")) { location.hash = "#/login"; }
    throw new Error(msg);
  }
  return data;
}

async function act(btn, fn, okMsg) {
  if (btn) btn.disabled = true;
  try { const r = await fn(); if (okMsg) toast(typeof okMsg === "function" ? okMsg(r) : okMsg); return r; }
  catch (e) { toast(e.message, true); }
  finally { if (btn) btn.disabled = false; }
}

function formData(form) {
  const o = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") o[el.name] = el.checked;
    else if (el.type === "number") o[el.name] = el.value === "" ? null : Number(el.value);
    else o[el.name] = el.value;
  }
  return o;
}

const field = (lbl, input) => h("div", { class: "field" }, h("label", {}, lbl), input);
const inp = (name, attrs = {}) => h("input", { name, ...attrs });
const sel = (name, options, value, attrs = {}) =>
  h("select", { name, ...attrs }, options.map(([v, l]) => h("option", { value: v, selected: String(v) === String(value ?? "") }, l)));

const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
function fmtDay(d) { const x = new Date(d + "T12:00:00"); return `${DOW[x.getDay()]} ${x.getMonth() + 1}/${x.getDate()}`; }
function fmtTime(iso) {
  if (!iso) return "TBD";
  let [hh, mm] = iso.slice(11, 16).split(":").map(Number);
  const ap = hh >= 12 ? "PM" : "AM"; hh = hh % 12 || 12;
  return `${hh}:${String(mm).padStart(2, "0")} ${ap}`;
}
const fmtWhen = iso => iso ? `${fmtDay(iso.slice(0, 10))} ${fmtTime(iso)}` : "TBD";
function daysOf(t) {
  const out = []; const d = new Date(t.start_date + "T12:00:00"); const end = new Date(t.end_date + "T12:00:00");
  while (d <= end) { out.push(d.toISOString().slice(0, 10)); d.setDate(d.getDate() + 1); }
  return out;
}
const pct = x => `${Math.round((x || 0) * 100)}%`;

function meter(frac, target) {
  const i = h("i", { class: frac > target + 0.05 ? "bad" : frac > target ? "warn" : "" });
  i.style.width = Math.min(100, Math.round(frac * 100)) + "%";
  return h("div", { class: "meter" }, i);
}

function modal(title, body, onClose) {
  const bg = h("div", { class: "modal-bg", onclick: e => { if (e.target === bg) close(); } });
  const close = () => { bg.remove(); onClose && onClose(); };
  bg.append(h("div", { class: "modal", role: "dialog", "aria-label": title },
    h("div", { class: "spread" }, h("h2", {}, title), h("button", { class: "link", onclick: close }, "Close")), body));
  document.body.append(bg);
  return close;
}

// live updates (Server-Sent Events)
function live(url, onEvent) {
  if (S.esKey === url) { S.onEvent = onEvent; return; }
  if (S.es) S.es.close();
  S.esKey = url; S.onEvent = onEvent;
  S.es = new EventSource(url);
  S.es.onmessage = m => { try { S.onEvent && S.onEvent(JSON.parse(m.data)); } catch (e) { console.error(e); } };
}
function stopLive() { if (S.es) S.es.close(); S.es = null; S.esKey = ""; }
function browserNotify(msg) {
  toast(msg);
  if ("Notification" in window && Notification.permission === "granted") new Notification("Schedule update", { body: msg });
}

// ---------------------------------------------------------------- router
const routes = [
  [/^#\/login$/, viewLogin],
  [/^#\/register(?:\/(\w+))?$/, viewRegister],
  [/^#\/p\/([\w-]+)$/, viewPublic],
  [/^#\/t\/new$/, viewNewTournament],
  [/^#\/t\/(\d+)(?:\/(\w+))?$/, viewTournament],
  [/^#\/team\/(\d+)(?:\/(\w+))?$/, viewTeam],
  [/^#\/?$/, viewHome],
];

async function loadMe() {
  const r = await api("/api/me");
  S.me = r.user ? r : null; S.csrf = r.csrf || "";
}

function renderNav() {
  $nav.replaceChildren();
  if (!S.me) {
    $nav.append(h("a", { href: "#/login" }, "Sign in"), h("a", { href: "#/register" }, "Create account"));
    return;
  }
  $nav.append(
    h("a", { href: "#/" }, "My tournaments"),
    h("span", { class: "muted small" }, S.me.user.name),
    h("button", { onclick: async () => { await api("/api/auth/logout", { method: "POST" }); S.me = null; location.hash = "#/login"; } }, "Sign out"));
}

async function route() {
  const hash = location.hash || "#/";
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (!m) continue;
    const isPublic = fn === viewPublic || fn === viewLogin || fn === viewRegister;
    if (fn !== viewPublic) stopLive();
    try {
      if (!S.me && !isPublic) { location.hash = "#/login"; return; }
      renderNav();
      await fn(...m.slice(1));
    } catch (e) { mount(h("div", { class: "card" }, h("h2", {}, "Something went wrong"), h("p", {}, e.message))); }
    return;
  }
  mount(h("div", { class: "card" }, "Page not found."));
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- auth
function viewLogin() {
  const form = h("form", { class: "stack" },
    field("Email", inp("email", { type: "email", required: true, autocomplete: "username" })),
    field("Password", inp("password", { type: "password", required: true, autocomplete: "current-password" })),
    h("button", { class: "primary", type: "submit" }, "Sign in"),
    h("p", { class: "small muted" }, "New coach or team manager? ", h("a", { href: "#/register" }, "Create an account"), " with the team code your tournament sent you."));
  form.onsubmit = async e => {
    e.preventDefault();
    const btn = form.querySelector("button");
    const r = await act(btn, () => api("/api/auth/login", { body: formData(form) }));
    if (r) { await loadMe(); location.hash = "#/"; route(); }
  };
  mount(h("div", { class: "auth card" }, h("h1", {}, "Sign in"), h("p", { class: "muted" }, "Schedules, preferences and live updates for your tournament."), form));
}

function viewRegister(code) {
  const form = h("form", { class: "stack" },
    field("Your name", inp("name", { required: true, autocomplete: "name" })),
    field("Email", inp("email", { type: "email", required: true, autocomplete: "email" })),
    field("Password (10+ characters)", inp("password", { type: "password", required: true, minlength: 10, autocomplete: "new-password" })),
    field("Team code (optional)", inp("invite_code", { value: code || "", placeholder: "e.g. K7P2Q9XM4A" })),
    h("button", { class: "primary", type: "submit" }, "Create account"));
  form.onsubmit = async e => {
    e.preventDefault();
    const body = formData(form); if (!body.invite_code) delete body.invite_code;
    const r = await act(form.querySelector("button"), () => api("/api/auth/register", { body }));
    if (r) { await loadMe(); if (r.superadmin) toast("You're the first user, so you're the platform administrator."); location.hash = "#/"; route(); }
  };
  mount(h("div", { class: "auth card" }, h("h1", {}, "Create account"), form));
}

// ---------------------------------------------------------------- home
async function viewHome() {
  await loadMe(); renderNav();
  const me = S.me;
  const joinForm = h("form", { class: "row" }, inp("code", { placeholder: "Team code", required: true }), h("button", { type: "submit" }, "Join team"));
  joinForm.onsubmit = async e => {
    e.preventDefault();
    const r = await act(joinForm.querySelector("button"), () => api("/api/teams/join", { body: formData(joinForm) }), r => `Joined ${r.name}`);
    if (r) viewHome();
  };
  const tcards = me.tournaments.map(t => h("a", { class: "card", href: `#/t/${t.id}` },
    h("div", { class: "spread" }, h("h3", {}, t.name), h("span", { class: "pill brand" }, t.role)),
    h("div", { class: "muted small" }, `${fmtDay(t.start_date)} – ${fmtDay(t.end_date)}`)));
  const teamCards = me.teams.map(t => h("a", { class: "card", href: `#/team/${t.id}` },
    h("h3", {}, t.name), h("div", { class: "muted small" }, `${t.division} · ${t.tournament}`)));
  mount(
    h("div", { class: "hero" }, h("h1", {}, `Welcome, ${me.user.name.split(" ")[0]}`),
      h("p", { class: "muted" }, "Build conflict-free multi-venue schedules, collect team preferences, and push live changes to coaches and parents.")),
    (me.tournaments.length || me.can_create) ? h("section", {},
      h("div", { class: "spread" }, h("h2", {}, "Tournaments you run"), me.can_create ? h("a", { class: "btn primary", href: "#/t/new" }, "New tournament") : null),
      me.tournaments.length ? h("div", { class: "grid g3" }, tcards) : h("p", { class: "muted" }, "No tournaments yet.")) : null,
    h("section", {}, h("h2", {}, "Your teams"),
      teamCards.length ? h("div", { class: "grid g3" }, teamCards) : h("p", { class: "muted" }, "You don't manage any teams yet. Enter the code from your tournament director."),
      h("div", { class: "card" }, joinForm)));
}

function viewNewTournament() {
  const form = h("form", { class: "stack" },
    h("div", { class: "fields" },
      field("Name", inp("name", { required: true })), field("Location", inp("location")),
      field("Start date", inp("start_date", { type: "date", required: true })), field("End date", inp("end_date", { type: "date", required: true }))),
    h("button", { class: "primary", type: "submit" }, "Create tournament"));
  form.onsubmit = async e => {
    e.preventDefault();
    const r = await act(form.querySelector("button"), () => api("/api/tournaments", { body: formData(form) }));
    if (r) { location.hash = `#/t/${r.id}/setup`; }
  };
  mount(h("div", { class: "card" }, h("h1", {}, "New tournament"), h("p", { class: "muted" }, "Rules default to AAU best practice: 50-minute games on the hour, 60 minutes rest, a 75-minute travel buffer between venues and 85% court utilization. You can change them next."), form));
}

// ---------------------------------------------------------------- tournament (staff)
const TABS = [["overview", "Overview"], ["setup", "Venues & rules"], ["teams", "Teams"], ["schedule", "Schedule"], ["live", "Live control"], ["staff", "Staff & refs"], ["publish", "Publish"]];

async function viewTournament(tid, tab) {
  const data = await api(`/api/t/${tid}`);
  if (data.role === "team") { location.hash = `#/team/${data.my_teams[0]}`; return; }
  const isAdmin = data.role === "admin" || data.role === "superadmin";
  tab = tab || (data.role === "director" ? "live" : "overview");
  const t = data.tournament;
  const tabs = h("nav", { class: "tabs" }, TABS.filter(([k]) => isAdmin || ["overview", "schedule", "live"].includes(k))
    .map(([k, l]) => h("a", { href: `#/t/${tid}/${k}`, class: k === tab ? "active" : "" }, l)));
  const body = h("div", {});
  mount(h("div", { class: "spread" }, h("div", {}, h("h1", {}, t.name),
    h("div", { class: "muted" }, `${fmtDay(t.start_date)} – ${fmtDay(t.end_date)}${t.location ? " · " + t.location : ""}`)),
    h("div", { class: "row" }, t.published ? h("span", { class: "pill ok" }, "Published") : h("span", { class: "pill" }, "Draft"),
      h("span", { class: "pill brand" }, data.role))), tabs, body);
  const ctx = { tid, data, isAdmin, reload: () => viewTournament(tid, tab), body };
  live(`/api/t/${tid}/stream`, ev => {
    if (ev.type === "notification") toast(ev.message);
    if (ev.type === "games_changed" && ["schedule", "live", "overview"].includes(tab)) ctx.refresh && ctx.refresh();
  });
  await ({ overview: tabOverview, setup: tabSetup, teams: tabTeams, schedule: tabSchedule, live: tabLive, staff: tabStaff, publish: tabPublish }[tab] || tabOverview)(ctx);
}

async function tabOverview(ctx) {
  const { data, tid, body } = ctx;
  const courts = data.courts.length, teams = data.teams.length;
  const multi = (data.coaches || []).filter(c => c.team_count > 1).length;
  const withPrefs = data.teams.filter(t => t.pref_count > 0).length;
  const rep = await api(`/api/t/${tid}/report`).catch(() => null);
  const hard = rep ? rep.issues.filter(i => i.severity === "hard") : [];
  const stats = data.last_run && data.last_run.stats;
  const steps = [
    [data.venues.length > 0 && courts > 0, "Add venues and courts", "setup"],
    [teams > 1, "Add or import teams", "teams"],
    [!!stats, "Generate the schedule", "schedule"],
    [stats && hard.length === 0 && !rep.unscheduled, "Resolve every hard conflict", "schedule"],
    [!!data.tournament.published, "Publish the live schedule", "publish"],
  ];
  const stat = (v, l) => h("div", { class: "card stat" }, h("div", { class: "v" }, v), h("div", { class: "l" }, l));
  body.replaceChildren(
    h("div", { class: "grid g4" }, stat(data.venues.length, "venues"), stat(courts, "courts"), stat(data.divisions.length, "divisions"),
      stat(teams, "teams"), stat(multi, "coaches with 2+ teams"), stat(`${withPrefs}/${teams}`, "teams with preferences")),
    h("div", { class: "grid g2" },
      h("div", { class: "card" }, h("h2", {}, "Checklist"),
        h("ol", {}, steps.map(([done, label, tab]) => h("li", {}, h("a", { href: `#/t/${tid}/${tab}` }, label), " ", done ? h("span", { class: "pill ok" }, "done") : h("span", { class: "pill" }, "to do"))))),
      h("div", { class: "card" }, h("h2", {}, "Schedule health"),
        !stats ? h("p", { class: "muted" }, "No schedule yet.") : h("div", { class: "stack" },
          h("div", {}, `${stats.games_scheduled} of ${stats.games_total} games placed · last run ${data.last_run.created_at} UTC`),
          h("div", {}, hard.length ? h("span", { class: "pill bad" }, `${hard.length} hard conflicts`) : h("span", { class: "pill ok" }, "No hard conflicts")),
          ...Object.entries(stats.venue_utilization || {}).map(([v, u]) => h("div", {}, h("div", { class: "spread small" }, h("span", {}, v), h("span", {}, pct(u))), meter(u, stats.target_utilization))),
          stats.briefing ? h("div", { class: "briefing small" }, stats.briefing.text) : null))));
}

async function tabSetup(ctx) {
  const { data, tid, body, isAdmin } = ctx;
  const t = data.tournament;
  const rules = h("form", { class: "stack" },
    h("div", { class: "fields" },
      field("Name", inp("name", { value: t.name })), field("Location", inp("location", { value: t.location })),
      field("Start date", inp("start_date", { type: "date", value: t.start_date })), field("End date", inp("end_date", { type: "date", value: t.end_date })),
      field("First game", inp("day_start", { type: "time", value: t.day_start })), field("Last game must end by", inp("day_end", { type: "time", value: t.day_end }))),
    h("h3", {}, "Scheduling rules"),
    h("div", { class: "fields" },
      field("Game length (min)", inp("game_minutes", { type: "number", value: t.game_minutes, min: 20 })),
      field("Rolling buffer per game (min)", inp("buffer_minutes", { type: "number", value: t.buffer_minutes, min: 0 })),
      field("Minimum team rest (min)", inp("min_rest_minutes", { type: "number", value: t.min_rest_minutes, min: 0 })),
      field("Travel buffer between venues (min)", inp("travel_buffer_minutes", { type: "number", value: t.travel_buffer_minutes, min: 0 })),
      field("Target court utilization", inp("target_utilization", { type: "number", value: t.target_utilization, step: 0.01, min: 0.5, max: 1 })),
      field("Max games per team per day", inp("max_games_per_day", { type: "number", value: t.max_games_per_day, min: 1 }))),
    h("label", { class: "row" }, inp("prefs_locked", { type: "checkbox", checked: !!t.prefs_locked }), " Lock team preferences (teams can no longer change them)"),
    h("p", { class: "small muted" }, `Slots run every ${t.game_minutes + t.buffer_minutes} minutes. About ${Math.round((1 - t.target_utilization) * 100)}% of slots are held back as catch-up time so one late game doesn't cascade through the day.`),
    h("div", { class: "row end" }, h("button", { class: "primary", type: "submit" }, "Save rules")));
  rules.onsubmit = async e => { e.preventDefault(); if (await act(rules.querySelector("button"), () => api(`/api/t/${tid}`, { method: "PATCH", body: formData(rules) }), "Saved")) ctx.reload(); };

  const vform = h("form", { class: "fields" },
    field("Venue name", inp("name", { required: true })), field("Address", inp("address")),
    field("Courts", inp("courts", { type: "number", value: 4, min: 1 })),
    field("Opens (optional)", inp("open_time", { type: "time" })), field("Closes (optional)", inp("close_time", { type: "time" })),
    h("div", { class: "field" }, h("label", {}, " "), h("button", { class: "primary", type: "submit" }, "Add venue")));
  vform.onsubmit = async e => {
    e.preventDefault(); const b = formData(vform); if (!b.open_time) delete b.open_time; if (!b.close_time) delete b.close_time;
    if (await act(vform.querySelector("button"), () => api(`/api/t/${tid}/venues`, { body: b }), "Venue added")) ctx.reload();
  };
  const venues = data.venues.map(v => {
    const cs = data.courts.filter(c => c.venue_id === v.id);
    const addCourt = h("button", { onclick: async e => { if (await act(e.target, () => api(`/api/venues/${v.id}/courts`, { body: { name: `Court ${cs.length + 1}` } }))) ctx.reload(); } }, "+ court");
    return h("div", { class: "card" }, h("div", { class: "spread" }, h("h3", {}, v.name),
      h("button", { class: "danger", onclick: async e => { if (confirm(`Delete ${v.name} and its courts?`) && await act(e.target, () => api(`/api/venues/${v.id}`, { method: "DELETE" }))) ctx.reload(); } }, "Delete")),
      h("div", { class: "muted small" }, [v.address, v.open_time && `${v.open_time}–${v.close_time || t.day_end}`].filter(Boolean).join(" · ")),
      h("div", { class: "row" }, cs.map(c => h("span", { class: "pill" }, c.name, " ", h("button", { class: "link small", title: "Remove court", onclick: async e => { if (await act(e.target, () => api(`/api/courts/${c.id}`, { method: "DELETE" }))) ctx.reload(); } }, "×"))), addCourt));
  });

  const venueOpts = [["", "Auto (balance by capacity)"], ...data.venues.map(v => [v.id, v.name])];
  const dform = h("form", { class: "row" }, inp("name", { placeholder: "e.g. 14U Boys", required: true }),
    h("button", { type: "submit" }, "Add division"));
  dform.onsubmit = async e => { e.preventDefault(); if (await act(dform.querySelector("button"), () => api(`/api/t/${tid}/divisions`, { body: formData(dform) }))) ctx.reload(); };
  const divRows = data.divisions.map(d => h("tr", {},
    h("td", {}, d.name), h("td", {}, data.teams.filter(x => x.division_id === d.id).length),
    h("td", {}, sel("pool_size", [3, 4, 5, 6].map(n => [n, `${n} teams (${n - 1} games each)`]), d.pool_size, { onchange: e => api(`/api/divisions/${d.id}`, { method: "PATCH", body: { pool_size: Number(e.target.value) } }).then(() => toast("Saved")) })),
    h("td", {}, sel("home_venue_id", venueOpts, d.home_venue_id, { onchange: e => api(`/api/divisions/${d.id}`, { method: "PATCH", body: { home_venue_id: e.target.value ? Number(e.target.value) : null } }).then(() => toast("Saved")) })),
    h("td", {}, h("button", { class: "link danger", onclick: async e => { if (confirm(`Delete ${d.name} and its teams?`) && await act(e.target, () => api(`/api/divisions/${d.id}`, { method: "DELETE" }))) ctx.reload(); } }, "Delete"))));
  body.replaceChildren(
    h("div", { class: "card" }, h("h2", {}, "Event & rules"), rules),
    h("div", { class: "card" }, h("h2", {}, "Venues & courts"), h("p", { class: "small muted" }, "Divisions stay anchored to one venue where possible, so teams aren't sent across town between games."), vform),
    h("div", { class: "grid g3" }, venues),
    h("div", { class: "card" }, h("h2", {}, "Divisions"), dform,
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Division", "Teams", "Pool size", "Home venue", ""].map(x => h("th", {}, x)))), h("tbody", {}, divRows)))));
}

async function tabTeams(ctx) {
  const { data, tid, body } = ctx;
  const divName = Object.fromEntries(data.divisions.map(d => [d.id, d.name]));
  const coachTeams = Object.fromEntries((data.coaches || []).map(c => [c.id, c.team_count]));
  const add = h("form", { class: "fields" },
    field("Division", sel("division_id", data.divisions.map(d => [d.id, d.name]))),
    field("Team name", inp("name", { required: true })), field("Club", inp("club")),
    field("Coach name", inp("coach_name")), field("Coach email", inp("coach_email", { type: "email" })),
    field("Rating (RPI, optional)", inp("rating", { type: "number", step: "any" })),
    h("div", { class: "field" }, h("label", {}, " "), h("button", { class: "primary", type: "submit" }, "Add team")));
  add.onsubmit = async e => { e.preventDefault(); const b = formData(add); b.division_id = Number(b.division_id); if (b.rating === null) delete b.rating; if (await act(add.querySelector("button"), () => api(`/api/t/${tid}/teams`, { body: b }), "Team added")) ctx.reload(); };
  const ta = h("textarea", { placeholder: "division,team,club,coach_name,coach_email,rating\n14U Boys,Lightning 14U,Lightning,Sam Lee,sam@club.org,0.612" });
  const imp = h("button", { onclick: async e => {
    const r = await act(e.target, () => api(`/api/t/${tid}/import`, { body: { csv: ta.value } }));
    if (r) { toast(`Imported ${r.added} teams${r.errors.length ? `, ${r.errors.length} rows skipped` : ""}`); ctx.reload(); }
  } }, "Import CSV");
  const filter = inp("q", { placeholder: "Filter teams, clubs, coaches…" });
  const tbody = h("tbody", {});
  const draw = () => {
    const q = filter.value.toLowerCase();
    tbody.replaceChildren(...data.teams.filter(t => !q || [t.name, t.club, t.coach_name, divName[t.division_id]].join(" ").toLowerCase().includes(q)).map(t => h("tr", {},
      h("td", {}, h("a", { href: `#/team/${t.id}` }, t.name), t.club ? h("div", { class: "small muted" }, t.club) : null),
      h("td", {}, divName[t.division_id]),
      h("td", {}, t.rating ?? h("span", { class: "muted" }, "—")),
      h("td", {}, t.coach_name || h("span", { class: "muted" }, "—"), coachTeams[t.coach_id] > 1 ? h("span", { class: "pill warn", title: "This coach has several teams; the scheduler keeps their games apart." }, ` ${coachTeams[t.coach_id]} teams`) : null),
      h("td", {}, h("span", { class: "code" }, t.invite_code)),
      h("td", {}, t.manager_count ? h("span", { class: "pill ok" }, `${t.manager_count} signed up`) : h("span", { class: "pill" }, "not joined")),
      h("td", {}, t.pref_count ? h("span", { class: "pill info" }, `${t.pref_count} prefs`) : ""),
      h("td", {}, h("button", { class: "link danger", onclick: async e => { if (confirm(`Delete ${t.name}?`) && await act(e.target, () => api(`/api/teams/${t.id}`, { method: "DELETE" }))) ctx.reload(); } }, "Delete")))));
  };
  filter.oninput = draw; draw();
  const multiCoaches = (data.coaches || []).filter(c => c.team_count > 1 || c.min_gap_minutes);
  const coachCard = h("div", { class: "card" }, h("h2", {}, "Coach rule profiles"),
    h("p", { class: "small muted" }, `Coaches with several teams are never double-booked, and get the ${data.tournament.travel_buffer_minutes}-minute travel buffer when their teams play at different venues. Set a custom minimum gap for a coach who needs more time between their teams' games, wherever they are.`),
    multiCoaches.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Coach", "Teams", "Minimum gap between their teams' games"].map(x => h("th", {}, x)))),
      h("tbody", {}, multiCoaches.map(c => {
        const gap = inp("g", { type: "number", min: 0, max: 600, value: c.min_gap_minutes ?? "", placeholder: "default" });
        return h("tr", {}, h("td", {}, c.name, h("div", { class: "small muted" }, c.email || "")),
          h("td", {}, data.teams.filter(t => t.coach_id === c.id).map(t => t.name).join(", ")),
          h("td", {}, h("div", { class: "row" }, gap, h("span", { class: "small muted" }, "min"), h("button", { onclick: async e => {
            if (await act(e.target, () => api(`/api/coaches/${c.id}`, { method: "PATCH", body: { min_gap_minutes: gap.value === "" ? null : Number(gap.value) } }), "Saved. Regenerate or repair to apply.")) {}
          } }, "Save"))));
      })))) : h("p", { class: "muted" }, "No coaches with more than one team yet."));
  body.replaceChildren(
    h("div", { class: "grid g2" },
      h("div", { class: "card" }, h("h2", {}, "Add a team"), data.divisions.length ? add : h("p", { class: "muted" }, "Create a division first under Venues & rules, or import a CSV.")),
      h("div", { class: "card stack" }, h("h2", {}, "Bulk import"), h("p", { class: "small muted" }, "Paste CSV with a header row. Divisions are created automatically, coaches are matched by email so one coach with several teams is detected, and the optional rating (RPI or prior results) is used to build balanced pools."), ta, h("div", { class: "row end" }, imp))),
    h("div", { class: "card" }, h("div", { class: "spread" }, h("h2", {}, `Teams (${data.teams.length})`), h("div", {}, filter)),
      h("p", { class: "small muted" }, "Send each coach their team code. They sign up at ", h("span", { class: "code" }, `${location.origin}/#/register/CODE`), " to manage preferences and get live updates."),
      h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Team", "Division", "Rating", "Coach", "Code", "Account", "Prefs", ""].map(x => h("th", {}, x)))), tbody))),
    coachCard);
}

function standingsView(standings, division, highlight) {
  const divs = Object.entries(standings).filter(([d]) => !division || d === division);
  if (!divs.length) return h("p", { class: "muted" }, "No standings yet.");
  return h("div", {}, divs.map(([d, pools]) => h("div", {}, h("h3", {}, d), h("div", { class: "grid g2" },
    Object.entries(pools).sort().map(([p, rows]) => h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, [`Pool ${p}`, "W", "L", "PF", "PA", "+/-"].map(x => h("th", {}, x)))),
      h("tbody", {}, rows.map(r => h("tr", { class: r.team_id === highlight ? "me" : "" }, h("td", {}, r.team), h("td", {}, r.w), h("td", {}, r.l), h("td", {}, r.pf), h("td", {}, r.pa), h("td", {}, r.diff > 0 ? "+" + r.diff : r.diff)))))))))));
}

function issueList(issues, limit = 50) {
  const hard = issues.filter(i => i.severity === "hard"), soft = issues.filter(i => i.severity === "soft");
  const counts = {}; soft.forEach(i => counts[i.type] = (counts[i.type] || 0) + 1);
  return h("div", {},
    hard.length ? hard.slice(0, limit).map(i => h("div", { class: "issue hard" }, h("strong", {}, i.type.replace(/_/g, " ")), ": ", i.message)) : h("div", { class: "issue soft" }, h("strong", {}, "No hard conflicts."), " Rest, travel and coach rules all hold."),
    soft.length ? h("details", {}, h("summary", { class: "small" }, `${soft.length} soft notes: ` + Object.entries(counts).map(([k, n]) => `${n} ${k.replace(/_/g, " ")}`).join(", ")),
      soft.slice(0, 80).map(i => h("div", { class: "issue soft small" }, i.message))) : null);
}

function gameCard(g, onClick) {
  const flags = g.flags || "";
  const cls = ["game", flags.includes("hard:") ? "hard" : flags.includes("soft:") ? "soft" : "", g.status === "final" ? "final" : g.status === "in_progress" ? "live" : "", g.original_start_at && g.start_at !== g.original_start_at ? "moved" : ""].join(" ");
  return h("div", { class: cls, onclick: () => onClick && onClick(g), title: flags.replace(/,/g, "\n") },
    h("div", { class: "teams" }, g.team_a_name, " vs ", g.team_b_name),
    h("div", { class: "muted" }, `${g.division} · Pool ${g.pool}`, g.ref_crew ? ` · ${g.ref_crew}` : ""),
    h("div", { class: "when small" }, fmtTime(g.start_at), g.status === "final" ? ` · Final ${g.score_a}-${g.score_b}` : g.status === "in_progress" ? " · Live" : ""));
}

function scheduleGrid(games, data, day, venueId, onClick) {
  const courts = data.courts.filter(c => !venueId || c.venue_id === Number(venueId));
  const gs = games.filter(g => g.start_at && g.start_at.startsWith(day) && courts.some(c => c.id === g.court_id));
  // Rows follow the planned slot, so a game running late stays in its row and shows its new time.
  const slotLen = data.tournament.game_minutes + data.tournament.buffer_minutes;
  const slotOf = g => {
    const o = g.original_start_at;
    const late = o ? (new Date(g.start_at) - new Date(o)) / 60000 : 0;
    return (o && o.startsWith(day) && late > 0 && late < slotLen ? o : g.start_at).slice(11, 16);
  };
  const times = [...new Set(gs.map(slotOf))].sort();
  if (!gs.length) return h("p", { class: "muted" }, "No games on this day.");
  const vname = Object.fromEntries(data.venues.map(v => [v.id, v.name]));
  return h("div", { class: "tablewrap" }, h("table", { class: "sched" },
    h("thead", {}, h("tr", {}, h("th", {}, ""), courts.map(c => h("th", {}, h("div", { class: "small muted" }, vname[c.venue_id]), c.name)))),
    h("tbody", {}, times.map(tm => h("tr", {}, h("td", { class: "time" }, fmtTime(day + "T" + tm)),
      courts.map(c => { const g = gs.find(x => x.court_id === c.id && slotOf(x) === tm); return h("td", {}, g ? gameCard(g, onClick) : h("div", { class: "empty-slot" })); }))))));
}

function editGame(ctx, g) {
  const { data, isAdmin } = ctx;
  const f = h("form", { class: "stack" },
    h("div", { class: "fields" },
      field("Status", sel("status", [["scheduled", "Scheduled"], ["in_progress", "In progress"], ["final", "Final"]], g.status === "unscheduled" ? "scheduled" : g.status)),
      field(g.team_a_name, inp("score_a", { type: "number", min: 0, value: g.score_a ?? "" })),
      field(g.team_b_name, inp("score_b", { type: "number", min: 0, value: g.score_b ?? "" }))),
    isAdmin ? h("div", { class: "fields" },
      field("Court", sel("court_id", data.courts.map(c => [c.id, `${data.venues.find(v => v.id === c.venue_id).name} · ${c.name}`]), g.court_id)),
      field("Start", inp("start_at", { type: "datetime-local", value: g.start_at || `${data.tournament.start_date}T${data.tournament.day_start}` }))) : null,
    g.flags ? h("div", { class: "issue hard small" }, g.flags.replace(/,/g, ", ")) : null,
    h("div", { class: "row end" }, h("button", { class: "primary", type: "submit" }, "Save")));
  let close;
  f.onsubmit = async e => {
    e.preventDefault(); const b = formData(f);
    if (b.score_a === null) delete b.score_a; if (b.score_b === null) delete b.score_b;
    if (b.court_id) b.court_id = Number(b.court_id);
    if (b.start_at === g.start_at) delete b.start_at;
    if (b.court_id === g.court_id) delete b.court_id;
    const r = await act(f.querySelector("button"), () => api(`/api/games/${g.id}`, { method: "PATCH", body: b }));
    if (r) { r.issues.filter(i => i.severity === "hard").length ? toast("Saved, but this game now has a conflict: " + r.issues[0].message, true) : toast("Saved"); close(); ctx.refresh && ctx.refresh(); }
  };
  close = modal(`${g.team_a_name} vs ${g.team_b_name}`, h("div", {}, h("p", { class: "muted" }, `${g.division} · Pool ${g.pool} · ${fmtWhen(g.start_at)} · ${g.venue || ""} ${g.court || ""}`), f));
}

async function tabSchedule(ctx) {
  const { data, tid, body, isAdmin } = ctx;
  const days = daysOf(data.tournament);
  const st = { day: days[0], venue: "", division: "", view: "grid" };
  const result = h("div", {});
  const holder = h("div", {});
  const gen = h("button", { class: "primary", onclick: async e => {
    if (data.last_run && !confirm("Regenerate the whole schedule? Manual edits will be replaced.")) return;
    e.target.textContent = "Optimizing…";
    const r = await act(e.target, () => api(`/api/t/${tid}/schedule/generate`, { body: { time_limit: Number(limit.value) } }));
    e.target.textContent = "Generate schedule";
    if (r) { showRun(r.stats, r.issues); refresh(); }
  } }, data.last_run ? "Regenerate schedule" : "Generate schedule");
  const limit = sel("limit", [[4, "Quick (4s)"], [10, "Thorough (10s)"], [25, "Deep (25s)"]], 10);
  const repair = h("button", { onclick: async e => {
    const r = await act(e.target, () => api(`/api/t/${tid}/schedule/repair`, { body: {} }));
    if (r) { toast(r.message || `Moved ${r.moved} games. ${r.remaining_hard} hard conflicts remain.`); refresh(); }
  } }, "Repair conflicts");
  function showRun(stats, issues) {
    result.replaceChildren(h("div", { class: "card" },
      h("h2", {}, "Last optimization"),
      h("div", { class: "grid g4" },
        ...[[`${stats.games_scheduled}/${stats.games_total}`, "games placed"], [pct(stats.utilization), `court use (target ${pct(stats.target_utilization)})`],
          [stats.team_venue_switches, "mid-day venue changes"], [stats.games_away_from_division_venue, "games off home venue"], [stats.flex_slots_used, "catch-up slots used"], [`${stats.seconds}s`, `${stats.restarts} restarts`]]
          .map(([v, l]) => h("div", { class: "stat" }, h("div", { class: "v" }, v), h("div", { class: "l" }, l)))),
      stats.briefing ? h("div", {}, h("h3", {}, stats.briefing.engine === "claude" ? "AI briefing" : "Briefing"), h("div", { class: "briefing" }, stats.briefing.text)) : null,
      h("h3", {}, "Venue utilization"),
      ...Object.entries(stats.venue_utilization || {}).map(([v, u]) => h("div", {}, h("div", { class: "spread small" }, h("span", {}, v, stats.home_venues ? h("span", { class: "muted" }, " · " + Object.entries(stats.home_venues).filter(([, hv]) => hv === v).map(([d]) => d).join(", ")) : ""), h("span", {}, pct(u))), meter(u, stats.target_utilization))),
      issues ? h("div", {}, h("h3", {}, "Conflicts"), issueList(issues)) : null));
  }
  async function refresh() {
    const [{ games }, rep] = await Promise.all([api(`/api/t/${tid}/games`), api(`/api/t/${tid}/report`)]);
    if (rep.last_run && !result.childNodes.length) showRun(rep.last_run, rep.issues);
    const shown = games.filter(g => !st.division || g.division_id === Number(st.division));
    const unsched = games.filter(g => g.status === "unscheduled");
    const hardN = rep.issues.filter(i => i.severity === "hard").length;
    const controls = h("div", { class: "row" },
      sel("day", days.map(d => [d, fmtDay(d)]), st.day, { onchange: e => { st.day = e.target.value; refresh(); } }),
      sel("venue", [["", "All venues"], ...data.venues.map(v => [v.id, v.name])], st.venue, { onchange: e => { st.venue = e.target.value; refresh(); } }),
      sel("division", [["", "All divisions"], ...data.divisions.map(d => [d.id, d.name])], st.division, { onchange: e => { st.division = e.target.value; refresh(); } }),
      h("a", { class: "btn", href: `/api/t/${tid}/export.csv` }, "Export CSV"), h("button", { onclick: () => window.print() }, "Print"));
    holder.replaceChildren(h("div", { class: "card" },
      h("div", { class: "spread" }, h("h2", {}, "Schedule"), h("div", { class: "row" }, hardN ? h("span", { class: "pill bad" }, `${hardN} hard conflicts`) : games.length ? h("span", { class: "pill ok" }, "Conflict-free") : null, unsched.length ? h("span", { class: "pill bad" }, `${unsched.length} unplaced`) : null)),
      controls, h("p", { class: "small muted" }, "Click a game to enter scores or move it. Red cards break a hard rule, amber cards miss a soft preference."),
      games.length ? scheduleGrid(shown, data, st.day, st.venue, g => editGame(ctx, g)) : h("p", { class: "muted" }, "Generate a schedule to see it here."),
      unsched.length ? h("div", {}, h("h3", {}, "Unplaced games"), h("div", { class: "grid g3" }, unsched.map(g => gameCard(g, g => isAdmin && editGame(ctx, g))))) : null));
  }
  ctx.refresh = refresh;
  body.replaceChildren(
    isAdmin ? h("div", { class: "card" }, h("div", { class: "spread" },
      h("div", {}, h("h2", {}, "Build the schedule"), h("p", { class: "muted small" }, `${data.teams.length} teams across ${data.divisions.length} divisions and ${data.courts.length} courts. The optimizer enforces rest, travel and multi-team-coach rules as hard limits, keeps divisions at their home venue, and leaves catch-up slots open.`)),
      h("div", { class: "row" }, limit, gen, repair))) : null,
    result, holder);
  await refresh();
}

async function tabLive(ctx) {
  const { data, tid, body } = ctx;
  const days = daysOf(data.tournament);
  const today = new Date().toISOString().slice(0, 10);
  const st = { day: days.includes(today) ? today : days[0], venue: data.venues[0] ? String(data.venues[0].id) : "" };
  const holder = h("div", {});
  const note = h("form", { class: "row" }, inp("message", { placeholder: "Announcement to every team and the public page", required: true, maxlength: 500 }), h("button", { type: "submit" }, "Send"));
  note.onsubmit = async e => { e.preventDefault(); if (await act(note.querySelector("button"), () => api(`/api/t/${tid}/notify`, { body: formData(note) }), "Sent")) note.reset(); };
  async function refresh() {
    const [{ games }, rep] = await Promise.all([api(`/api/t/${tid}/games`), api(`/api/t/${tid}/report`)]);
    const courts = data.courts.filter(c => !st.venue || c.venue_id === Number(st.venue));
    const hard = rep.issues.filter(i => i.severity === "hard");
    const rows = courts.map(c => {
      const cg = games.filter(g => g.court_id === c.id && g.start_at && g.start_at.startsWith(st.day));
      const cur = cg.find(g => g.status === "in_progress") || cg.find(g => g.status === "scheduled");
      const late = cur && cur.original_start_at ? (new Date(cur.start_at) - new Date(cur.original_start_at)) / 60000 : 0;
      const mins = h("select", {}, [5, 10, 15, 20, 30, 45, 60, -10].map(m => h("option", { value: m }, m > 0 ? `+${m} min late` : `${m} min (caught up)`)));
      return h("tr", {},
        h("td", {}, h("strong", {}, c.name)),
        h("td", {}, cur ? h("div", {}, `${fmtTime(cur.start_at)} · ${cur.team_a_name} vs ${cur.team_b_name}`, h("div", { class: "small muted" }, cur.ref_crew || "")) : h("span", { class: "muted" }, "Done for the day")),
        h("td", {}, late > 0 ? h("span", { class: "pill warn" }, `${late} min behind`) : h("span", { class: "pill ok" }, "On time")),
        h("td", {}, `${cg.filter(g => g.status === "final").length}/${cg.length} final`),
        h("td", {}, cur ? h("div", { class: "row" }, mins, h("button", { onclick: async e => {
          const r = await act(e.target, () => api(`/api/courts/${c.id}/delay`, { body: { minutes: Number(mins.value), from_at: cur.start_at } }));
          if (r) { toast(`${r.moved} games shifted; affected teams notified.${r.hard_issues.length ? ` ${r.hard_issues.length} downstream conflicts flagged.` : ""}`, r.hard_issues.length > 0); refresh(); }
        } }, "Apply"), h("button", { onclick: () => editGame(ctx, cur) }, "Score")) : null));
    });
    holder.replaceChildren(
      h("div", { class: "card" }, h("div", { class: "spread" }, h("h2", {}, "Courts"), h("div", { class: "row" },
        sel("day", days.map(d => [d, fmtDay(d)]), st.day, { onchange: e => { st.day = e.target.value; refresh(); } }),
        sel("venue", [["", "All venues"], ...data.venues.map(v => [v.id, v.name])], st.venue, { onchange: e => { st.venue = e.target.value; refresh(); } }))),
        h("p", { class: "small muted" }, "When a court falls behind, record it here. Later games on that court shift only as much as the buffers can't absorb, the teams are notified, and any rest or coach conflicts it causes are flagged below."),
        h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Court", "Now / next", "Status", "Progress", "Report delay"].map(x => h("th", {}, x)))), h("tbody", {}, rows)))),
      h("div", { class: "card" }, h("div", { class: "spread" }, h("h2", {}, "Downstream impact"), ctx.isAdmin && hard.length ? h("button", { class: "primary", onclick: async e => {
        const r = await act(e.target, () => api(`/api/t/${tid}/schedule/repair`, { body: { from_at: `${st.day}T00:00` } }));
        if (r) { toast(`Moved ${r.moved} games. ${r.remaining_hard} conflicts remain.`); refresh(); }
      } }, "Auto-repair conflicts") : null), issueList(rep.issues, 30)),
      h("div", { class: "card" }, h("h2", {}, "Announce"), note));
  }
  ctx.refresh = refresh;
  body.replaceChildren(holder);
  await refresh();
}

async function tabStaff(ctx) {
  const { data, tid, body } = ctx;
  const vname = Object.fromEntries(data.venues.map(v => [v.id, v.name]));
  const add = h("form", { class: "fields" },
    field("Email (must have an account)", inp("email", { type: "email", required: true })),
    field("Role", sel("role", [["director", "Site director"], ["admin", "Tournament admin"]])),
    field("Venue (directors)", sel("venue_id", [["", "All venues"], ...data.venues.map(v => [v.id, v.name])])),
    h("div", { class: "field" }, h("label", {}, " "), h("button", { class: "primary", type: "submit" }, "Add")));
  add.onsubmit = async e => { e.preventDefault(); const b = formData(add); b.venue_id = b.venue_id ? Number(b.venue_id) : null; if (await act(add.querySelector("button"), () => api(`/api/t/${tid}/members`, { body: b }), "Added")) ctx.reload(); };
  const crew = h("form", { class: "row" }, inp("name", { placeholder: "Crew name", required: true }),
    sel("venue_id", [["", "Float (auto-assign)"], ...data.venues.map(v => [v.id, v.name])]), h("button", { type: "submit" }, "Add crew"));
  crew.onsubmit = async e => { e.preventDefault(); const b = formData(crew); b.venue_id = b.venue_id ? Number(b.venue_id) : null; if (await act(crew.querySelector("button"), () => api(`/api/t/${tid}/crews`, { body: b }))) ctx.reload(); };
  body.replaceChildren(
    h("div", { class: "card" }, h("h2", {}, "Staff"), h("p", { class: "small muted" }, "Site directors run one venue: they report delays, enter scores and send announcements, but can't change the schedule."), add,
      h("table", {}, h("tbody", {}, data.members.map(m => h("tr", {}, h("td", {}, m.name), h("td", {}, m.email), h("td", {}, h("span", { class: "pill brand" }, m.role)), h("td", {}, m.venue_id ? vname[m.venue_id] : ""),
        h("td", {}, h("button", { class: "link danger", onclick: async e => { if (await act(e.target, () => api(`/api/t/${tid}/members/${m.user_id}`, { method: "DELETE" }))) ctx.reload(); } }, "Remove"))))))),
    h("div", { class: "card" }, h("h2", {}, "Referee crews"), h("p", { class: "small muted" }, "Crews are zoned: each stays at one venue and works one court for a 4-hour block, so no one crosses town mid-day."), crew,
      h("table", {}, h("tbody", {}, data.crews.map(c => h("tr", {}, h("td", {}, c.name), h("td", {}, c.venue_id ? vname[c.venue_id] : h("span", { class: "muted" }, "floating")),
        h("td", {}, h("button", { class: "link danger", onclick: async e => { if (await act(e.target, () => api(`/api/crews/${c.id}`, { method: "DELETE" }))) ctx.reload(); } }, "Delete"))))))));
}

async function tabPublish(ctx) {
  const { data, tid, body } = ctx;
  const t = data.tournament;
  const url = `${location.origin}/#/p/${t.public_slug}`;
  body.replaceChildren(h("div", { class: "grid g2" },
    h("div", { class: "card stack" }, h("h2", {}, "Public live schedule"),
      h("p", { class: "muted" }, "Parents and fans can follow the live schedule without an account. It updates instantly when a court runs late or a game moves. Team preferences, referee assignments and internal flags are never shown."),
      h("div", {}, h("a", { href: url, target: "_blank", rel: "noopener" }, url)),
      h("div", { class: "row" }, t.published
        ? h("button", { onclick: async e => { if (await act(e.target, () => api(`/api/t/${tid}/publish`, { body: { published: false } }), "Unpublished")) ctx.reload(); } }, "Unpublish")
        : h("button", { class: "primary", onclick: async e => { if (await act(e.target, () => api(`/api/t/${tid}/publish`, { body: { published: true } }), "Published")) ctx.reload(); } }, "Publish schedule"))),
    h("div", { class: "card" }, h("h2", {}, "QR code for venues"), h("p", { class: "small muted" }, "Print this at each gym entrance."), h("img", { class: "qr", src: `/api/t/${tid}/qr.svg`, alt: "QR code linking to the live schedule" }))));
}

// ---------------------------------------------------------------- team portal
const PREF_LABEL = { unavailable: "Can't play", not_before: "No games before", not_after: "Done by", preferred_venue: "Prefer venue" };

function prefText(p, venues) {
  const day = p.day ? fmtDay(p.day) : "Every day";
  if (p.kind === "unavailable") return `${day}: can't play ${p.start || p.end ? `${p.start || "start"}–${p.end || "end"}` : "all day"}`;
  if (p.kind === "not_before") return `${day}: no games before ${p.start}`;
  if (p.kind === "not_after") return `${day}: finished by ${p.end}`;
  if (p.kind === "preferred_venue") return `${day}: prefer ${(venues.find(v => v.id === p.venue_id) || {}).name || "venue"}`;
  return p.kind;
}

async function viewTeam(teamId, tab) {
  tab = tab || "schedule";
  const pr = await api(`/api/teams/${teamId}/prefs`);
  const tid = pr.team.tournament_id;
  const [data, { games }, notes] = await Promise.all([api(`/api/t/${tid}`), api(`/api/t/${tid}/games`), api(`/api/t/${tid}/notifications`)]);
  const team = data.teams.find(t => t.id === Number(teamId));
  const div = data.divisions.find(d => d.id === team.division_id);
  const mine = games.filter(g => g.team_a === team.id || g.team_b === team.id);
  const body = h("div", {});
  const myTeams = (S.me.teams || []).filter(x => x.tournament_id === tid);
  mount(
    h("div", { class: "spread" }, h("div", {}, h("h1", {}, team.name), h("div", { class: "muted" }, `${div.name} · ${data.tournament.name}${team.coach_name ? " · Coach " + team.coach_name : ""}`)),
      h("div", { class: "row" }, myTeams.length > 1 ? sel("t", myTeams.map(x => [x.id, x.name]), teamId, { onchange: e => location.hash = `#/team/${e.target.value}/${tab}` }) : null,
        data.role !== "team" ? h("a", { class: "btn", href: `#/t/${tid}/teams` }, "Back to admin") : null)),
    h("nav", { class: "tabs" }, [["schedule", "Our games"], ["prefs", "Scheduling preferences"], ["all", "Full schedule"], ["standings", "Standings"], ["updates", `Updates (${notes.notifications.length})`]]
      .map(([k, l]) => h("a", { href: `#/team/${teamId}/${k}`, class: tab === k ? "active" : "" }, l))),
    body);
  live(`/api/t/${tid}/stream`, ev => {
    if (ev.type === "notification") browserNotify(ev.message);
    if (ev.type === "games_changed" && tab !== "prefs") viewTeam(teamId, tab);
  });
  if (tab === "schedule") {
    const ask = "Notification" in window && Notification.permission === "default"
      ? h("button", { onclick: () => Notification.requestPermission().then(p => toast(p === "granted" ? "Notifications on" : "Notifications blocked")) }, "Turn on alerts") : null;
    body.replaceChildren(h("div", { class: "card" }, h("div", { class: "spread" }, h("h2", {}, "Our games"), ask),
      !mine.length ? h("p", { class: "muted" }, "The schedule hasn't been released yet. Add your preferences now so the scheduler can plan around them.") :
        mine.map(g => {
          const opp = g.team_a === team.id ? g.team_b_name : g.team_a_name;
          const moved = g.original_start_at && g.start_at !== g.original_start_at;
          return h("div", { class: "list-game" },
            h("div", { class: "when" }, fmtWhen(g.start_at), moved ? h("div", { class: "pill warn" }, `moved from ${fmtTime(g.original_start_at)}`) : null),
            h("div", {}, h("div", { class: "vs" }, "vs ", opp), h("div", { class: "muted small" }, `${g.venue || "TBD"} · ${g.court || ""} · Pool ${g.pool}`)),
            g.status === "final" ? h("span", { class: "pill ok" }, `Final ${g.team_a === team.id ? g.score_a + "-" + g.score_b : g.score_b + "-" + g.score_a}`) : g.status === "in_progress" ? h("span", { class: "pill info" }, "Live") : h("span", { class: "pill" }, "Scheduled"));
        })));
  } else if (tab === "prefs") {
    renderPrefs(body, teamId, pr, data);
  } else if (tab === "all") {
    const sDiv = sel("d", [["", "All divisions"], ...data.divisions.map(d => [d.id, d.name])], div.id);
    const list = h("div", {});
    const draw = () => {
      const gs = games.filter(g => g.start_at && (!sDiv.value || g.division_id === Number(sDiv.value)));
      list.replaceChildren(...gs.map(g => h("div", { class: "list-game" }, h("div", { class: "when" }, fmtWhen(g.start_at)),
        h("div", {}, h("div", { class: "vs" }, `${g.team_a_name} vs ${g.team_b_name}`), h("div", { class: "muted small" }, `${g.division} · ${g.venue} · ${g.court}`)),
        g.status === "final" ? h("span", { class: "pill ok" }, `${g.score_a}-${g.score_b}`) : h("span", {}))));
    };
    sDiv.onchange = draw; draw();
    body.replaceChildren(h("div", { class: "card" }, h("div", { class: "spread" }, h("h2", {}, "Full schedule"), sDiv), list));
  } else if (tab === "standings") {
    const { standings } = await api(`/api/t/${tid}/standings`);
    body.replaceChildren(h("div", { class: "card" }, h("h2", {}, "Pool standings"), h("p", { class: "small muted" }, "Ranked by wins, then head-to-head, then point differential."), standingsView(standings, div.name, team.id)));
  } else {
    body.replaceChildren(h("div", { class: "card" }, h("h2", {}, "Updates"), notes.notifications.length ? notes.notifications.map(n => h("div", { class: "list-game" }, h("div", { class: "small muted" }, n.created_at + " UTC"), h("div", {}, n.message), h("span", {}))) : h("p", { class: "muted" }, "No updates yet.")));
  }
}

function renderPrefs(body, teamId, pr, data) {
  const t = data.tournament, venues = data.venues, days = daysOf(t);
  const locked = pr.team.prefs_locked && data.role === "team";
  const listEl = h("div", {});
  const drawList = prefs => listEl.replaceChildren(prefs.length ? h("table", {}, h("tbody", {}, prefs.map(p => h("tr", {},
    h("td", {}, prefText(p, venues), p.note ? h("div", { class: "small muted" }, p.note) : null),
    h("td", {}, p.hard ? h("span", { class: "pill bad" }, "must") : h("span", { class: "pill" }, "nice to have"), p.source === "ai" ? h("span", { class: "pill info" }, " AI") : null),
    h("td", {}, locked ? null : h("button", { class: "link danger", onclick: async e => { if (await act(e.target, () => api(`/api/prefs/${p.id}`, { method: "DELETE" }))) reload(); } }, "Remove")))))) : h("p", { class: "muted" }, "No preferences yet."));
  const reload = async () => { const r = await api(`/api/teams/${teamId}/prefs`); drawList(r.preferences); };
  drawList(pr.preferences);

  const ta = h("textarea", { placeholder: "e.g. We drive in Saturday morning so nothing before 11am. Our coach also has the 15U team, and we'd like to be finished by 4pm Sunday.", maxlength: 2000 }, pr.team.pref_notes || "");
  const sugg = h("div", { class: "stack" });
  const parse = h("button", { class: "primary", onclick: async e => {
    e.target.textContent = "Reading…";
    const r = await act(e.target, () => api(`/api/teams/${teamId}/prefs/parse`, { body: { text: ta.value } }));
    e.target.textContent = "Turn into preferences";
    if (!r) return;
    sugg.replaceChildren(h("div", { class: "small muted" }, r.summary, r.engine === "claude" ? " (read by Claude)" : ""),
      ...r.preferences.map(p => {
        const b = h("button", { onclick: async ev => { if (await act(ev.target, () => api(`/api/teams/${teamId}/prefs`, { body: { ...p, source: "ai" } }), "Added")) { ev.target.closest(".suggest").remove(); reload(); } } }, "Add");
        return h("div", { class: "suggest spread" }, h("div", {}, h("strong", {}, prefText(p, venues)), " ", p.hard ? h("span", { class: "pill bad" }, "must") : h("span", { class: "pill" }, "nice to have")), b);
      }));
  } }, "Turn into preferences");

  const kind = sel("kind", Object.entries(PREF_LABEL), "unavailable");
  const form = h("form", { class: "stack" }, h("div", { class: "fields" },
    field("Type", kind), field("Day", sel("day", [["", "Every day"], ...days.map(d => [d, fmtDay(d)])])),
    field("From", inp("start", { type: "time" })), field("Until", inp("end", { type: "time" })),
    field("Venue", sel("venue_id", [["", "—"], ...venues.map(v => [v.id, v.name])]))),
    h("label", { class: "row" }, inp("hard", { type: "checkbox" }), " This is a must (we truly can't play), not just a wish"),
    field("Note", inp("note", { maxlength: 300, placeholder: "Why? Helps the director if there's a tradeoff." })),
    h("div", { class: "row end" }, h("button", { type: "submit" }, "Add preference")));
  form.onsubmit = async e => {
    e.preventDefault(); const b = formData(form);
    b.venue_id = b.venue_id ? Number(b.venue_id) : null;
    for (const k of ["day", "start", "end"]) if (!b[k]) b[k] = null;
    if (await act(form.querySelector("button"), () => api(`/api/teams/${teamId}/prefs`, { body: b }), "Added")) { form.reset(); reload(); }
  };
  body.replaceChildren(
    locked ? h("div", { class: "issue soft" }, "Preferences are locked because the schedule is being finalized. Contact the tournament director for changes.") : null,
    h("div", { class: "grid g2" },
      h("div", { class: "card" }, h("h2", {}, "Current preferences"), h("p", { class: "small muted" }, "Musts are never broken by the scheduler. Nice-to-haves are honored when possible."), listEl),
      locked ? null : h("div", { class: "card stack" }, h("h2", {}, "Describe your constraints"), h("p", { class: "small muted" }, "Write it the way you'd text the director. We'll turn it into scheduling rules for you to confirm."), ta, h("div", { class: "row end" }, parse), sugg)),
    locked ? null : h("div", { class: "card" }, h("h2", {}, "Or add one manually"), form));
}

// ---------------------------------------------------------------- public view
async function viewPublic(slug) {
  renderNav();
  const st = { q: "", division: "" };
  const holder = h("div", {});
  async function load() {
    const d = await api(`/api/public/${slug}`);
    const days = [...new Set(d.games.map(g => g.start_at.slice(0, 10)))];
    const divs = [...new Map(d.games.map(g => [g.division_id, g.division])).entries()];
    const q = h("input", { placeholder: "Search your team…", value: st.q });
    const ds = sel("d", [["", "All divisions"], ...divs], st.division);
    const list = h("div", {});
    const draw = () => {
      st.q = q.value; st.division = ds.value;
      const gs = d.games.filter(g => (!st.division || g.division_id === Number(st.division)) && (!st.q || (g.team_a_name + " " + g.team_b_name).toLowerCase().includes(st.q.toLowerCase())));
      list.replaceChildren(...days.map(day => {
        const dg = gs.filter(g => g.start_at.startsWith(day));
        return dg.length ? h("div", { class: "card" }, h("h2", {}, fmtDay(day)), dg.map(g => h("div", { class: "list-game" },
          h("div", { class: "when" }, fmtTime(g.start_at), g.original_start_at && g.original_start_at !== g.start_at ? h("div", { class: "pill warn" }, `was ${fmtTime(g.original_start_at)}`) : null),
          h("div", {}, h("div", { class: "vs" }, `${g.team_a_name} vs ${g.team_b_name}`), h("div", { class: "muted small" }, `${g.division} · ${g.venue} · ${g.court}`)),
          g.status === "final" ? h("span", { class: "pill ok" }, `Final ${g.score_a}-${g.score_b}`) : g.status === "in_progress" ? h("span", { class: "pill info" }, "Live") : h("span", {})))) : null;
      }));
    };
    q.oninput = draw; ds.onchange = draw; draw();
    holder.replaceChildren(
      h("div", { class: "hero" }, h("h1", {}, d.tournament.name), h("div", { class: "muted" }, `${fmtDay(d.tournament.start_date)} – ${fmtDay(d.tournament.end_date)} · Live schedule, updates automatically`)),
      d.announcements.length ? h("div", { class: "card" }, h("h2", {}, "Announcements"), d.announcements.map(a => h("div", { class: "issue soft" }, a.message))) : null,
      h("div", { class: "row" }, q, ds), list,
      h("div", { class: "card" }, h("h2", {}, "Pool standings"), standingsView(d.standings, st.division ? (divs.find(([id]) => String(id) === st.division) || [])[1] : "")));
  }
  mount(holder);
  await load();
  live(`/api/public/${slug}/stream`, ev => { if (ev.type === "games_changed") load(); if (ev.type === "notification") { toast(ev.message); load(); } });
}

// ---------------------------------------------------------------- boot
fetch("/api/version").then(r => r.json()).then(v => { document.getElementById("app-version").textContent = `v${v.version}`; }).catch(() => {});
(async () => { try { await loadMe(); } catch (e) { S.me = null; } route(); })();
