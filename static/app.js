"use strict";

// Nav Downloader front end: polls /api/state, renders the lists in place (keyed by
// JDownloader's uuids, so nothing flickers), and sends actions to /api/do.

const POLL_MS = 2000;
const LIMIT_CHOICES = [0, 1, 2, 5, 10, 20, 50, 100]; // MB/s, 0 = no limit
const MAX_CHOICES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]; // downloads at a time
// JDownloader moves "Start right away" links to Downloads by itself once they're
// checked (after its AutoConfirmDelay; setup-jd sets 1 s); until then they sit in the grabber.
const AUTOSTART_HINT_MS = 60000;
const FAILED = /offline|fail|error|not found|defect|abort|invalid|unavailable|too many|disk full|no account|already exists/i;
const CONTROLLER = {
  RUNNING: ["Downloading", "running"],
  PAUSE: ["Paused", "paused"],
  STOPPING: ["Stopping…", ""],
  STOPPED_STATE: ["Stopped", ""], // JDownloader's own label for "stopped"
  IDLE: ["Idle", ""],
};

const $ = (id) => document.getElementById(id);
let state = null;
let pollTimer = null;
let refreshing = false;
let authMode = "login";
// After a "Start right away" add: explain why its links sit in the grabber for a moment.
let autostartUntil = 0;
let autostartBefore = new Set(); // grabber packages that were there before that add
const expanded = new Set();

// ---------------------------------------------------------------- helpers

class ApiError extends Error {
  constructor(message, status, unreachable) {
    super(message);
    this.status = status;
    this.unreachable = unreachable;
  }
}

async function api(path, body) {
  const opts = { credentials: "same-origin" };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers = { "Content-Type": "application/json", "X-JDWeb": "1" };
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError("Can't reach the Nav Downloader server. Check your connection.", 0, true);
  }
  let data = {};
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (res.status === 401 && path !== "/api/login") {
    showAuth("login");
    throw new ApiError(data.error || "Please sign in.", 401);
  }
  if (!res.ok) throw new ApiError(data.error || `Request failed (${res.status})`, res.status, !!data.unreachable);
  return data;
}

function fmtBytes(n) {
  if (typeof n !== "number" || n < 0) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  const digits = i === 0 ? 0 : n < 10 ? 2 : n < 100 ? 1 : 0;
  return `${n.toFixed(digits)} ${units[i]}`;
}
const fmtSpeed = (n) => `${fmtBytes(n || 0)}/s`;

function fmtEta(s) {
  if (!(s > 0)) return "";
  if (s < 60) return `${Math.round(s)} s left`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min left`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h} h ${m % 60} min left` : `${Math.round(h / 24)} days left`;
}

function svgIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function iconButton(icon, label, cls = "btn icon ghost") {
  const b = el("button", cls);
  b.type = "button";
  b.setAttribute("aria-label", label);
  b.title = label;
  b.appendChild(svgIcon(icon));
  return b;
}

let toastTimer = null;
function toast(message, isError = false) {
  const t = $("toast");
  t.textContent = message;
  t.className = isError ? "toast error" : "toast";
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 5000 : 2600);
}

let bannerError = "";
function setBanner(error) {
  bannerError = error;
  const b = $("banner");
  const captchas = state && !error ? state.captchas : 0;
  const text = error || (captchas ? `${captchas === 1 ? "A download is" : `${captchas} downloads are`} waiting for a captcha. Solve it in My.JDownloader.` : "");
  b.textContent = text;
  b.hidden = !text;
}

function store(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private mode */ }
}
function recall(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

// ---------------------------------------------------------------- sign in
//
// One card, three forms: "login"; "setup" (first visit) and "reset" (forgot
// password), which both need a code the server printed to its log. Codes never
// reach the page, so using one proves access to the server.

const AUTH_TEXT = {
  login: ["Sign in", "Enter your password to manage downloads.", "Password", "Sign in"],
  setup: ["Create a password", "First visit: enter the setup code, then choose the password you'll use to open this page (at least 8 characters).", "New password", "Create password"],
  reset: ["Reset your password", "", "New password", "Set new password"],
};
let passwordManaged = false; // set with JDWEB_PASSWORD: no reset or change from here

function showAuth(mode, hint) {
  stopPolling();
  authMode = mode;
  const [title, defaultHint, passwordLabel, submit] = AUTH_TEXT[mode];
  $("app").hidden = true;
  $("auth").hidden = false;
  $("auth-title").textContent = title;
  $("auth-hint").textContent = hint || defaultHint;
  $("auth-code-row").hidden = mode === "login";
  $("auth-code-label").textContent = mode === "setup" ? "Setup code" : "Reset code";
  $("auth-confirm-row").hidden = mode === "login";
  $("auth-password-label").textContent = passwordLabel;
  $("auth-password").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("auth-submit").textContent = submit;
  $("auth-forgot").hidden = mode !== "login";
  $("auth-newcode").hidden = mode === "login";
  $("auth-back").hidden = mode !== "reset";
  $("auth-error").textContent = "";
  (mode === "login" ? $("auth-password") : $("auth-code")).focus();
}

// Asks the server to print a fresh setup or reset code to its log.
async function requestCode() {
  $("auth-error").textContent = "";
  try {
    const r = await api("/api/reset/request", {});
    if (r.setup && authMode !== "setup") showAuth("setup");
    $("auth-hint").textContent = r.waitSeconds
      ? `A code was written to the log moments ago. Use that one, or ask again in ${r.waitSeconds} s.`
      : r.setup ? "A new setup code was written to the server log."
      : "A reset code was written to the server log. It works for 15 minutes.";
  } catch (ex) {
    $("auth-error").textContent = ex.message;
  }
}

$("auth-forgot").addEventListener("click", () => {
  if (passwordManaged) {
    $("auth-error").textContent = "The password is set with JDWEB_PASSWORD. Change it there and restart the container.";
    return;
  }
  showAuth("reset", "Asking the server for a reset code…");
  requestCode();
});
$("auth-newcode").addEventListener("click", requestCode);
$("auth-back").addEventListener("click", () => showAuth("login"));

$("auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const password = $("auth-password").value;
  const code = $("auth-code").value.trim();
  const err = $("auth-error");
  err.textContent = "";
  if (authMode !== "login") {
    if (!code) { err.textContent = "Enter the code from the server log."; $("auth-code").focus(); return; }
    if (password.length < 8) { err.textContent = "Use at least 8 characters."; return; }
    if (password !== $("auth-confirm").value) { err.textContent = "The two passwords don't match."; return; }
  }
  const path = { login: "/api/login", setup: "/api/setup", reset: "/api/reset/confirm" }[authMode];
  const wasReset = authMode === "reset";
  $("auth-submit").disabled = true;
  try {
    await api(path, authMode === "login" ? { password } : { code, password });
    for (const id of ["auth-password", "auth-confirm", "auth-code"]) $(id).value = "";
    startApp();
    if (wasReset) toast("Password changed");
  } catch (ex) {
    err.textContent = ex.message;
    if (ex.status === 409) showAuth("login");
  } finally {
    $("auth-submit").disabled = false;
  }
});

// ---------------------------------------------------------------- connect help
//
// Shown instead of the lists while JDownloader's API can't be reached: at once if
// it hasn't answered since the page opened, otherwise after 15 s, so a JDownloader
// restart doesn't flash setup help.

const CONNECT_AFTER_MS = 15000;
const UNREACHABLE_WHY = {
  refused: "the connection was refused. The API is probably off, or Nav Downloader isn't on JDownloader's network",
  timeout: "it didn't answer in time",
  http: "it answered with an error",
  other: "the connection failed",
};
let everConnected = false;
let unreachableSince = 0;

function showConnect(show) {
  if ($("connect").hidden === !show) return;
  $("connect").hidden = !show;
  // The header's download controls can't work either; the account menu stays.
  for (const id of ["add-card", "dl-card", "status", "btn-main", "btn-stop", "btn-limit"]) $(id).hidden = show;
  if (show) {
    $("grabber").hidden = true;
    loadJdStatus();
  }
}

async function loadJdStatus() {
  try {
    const s = await api("/api/jd-status");
    $("connect-cmd").textContent = `docker run --rm --user 0 -v /path/to/jdownloader/config:/jd ${s.image} setup-jd /jd`;
    if (s.reachable) {
      refresh();
      return;
    }
    $("connect-status").textContent = `Nav Downloader can't reach JDownloader at ${s.api}: ${UNREACHABLE_WHY[s.error] || UNREACHABLE_WHY.other}.`;
  } catch (e) {
    if (e.status !== 401) $("connect-status").textContent = e.message;
  }
}
$("connect-check").addEventListener("click", loadJdStatus);

// ---------------------------------------------------------------- polling

function startApp() {
  $("auth").hidden = true;
  $("app").hidden = false;
  refresh();
  loadFolders();
  startPolling();
}
function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
}
function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !$("app").hidden) refresh();
});

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api("/api/state");
    everConnected = true;
    unreachableSince = 0;
    showConnect(false);
    setBanner("");
    render();
  } catch (e) {
    if (e.status === 401) return;
    if (e.status === 503) {
      unreachableSince = unreachableSince || Date.now();
      if (!everConnected || Date.now() - unreachableSince > CONNECT_AFTER_MS) {
        setBanner("");
        showConnect(true);
        return;
      }
    }
    setBanner(e.message);
  } finally {
    refreshing = false;
  }
}

async function act(payload, okMessage) {
  try {
    await api("/api/do", payload);
    if (okMessage) toast(okMessage);
  } catch (e) {
    if (e.status !== 401) toast(e.message, true);
  } finally {
    refresh();
  }
}

// ---------------------------------------------------------------- rendering

function render() {
  renderHeader();
  const downloads = state.downloads || [];
  const grabber = state.grabber || [];

  $("dl-count").textContent = downloads.length ? `(${downloads.length})` : "";
  $("dl-empty").hidden = downloads.length > 0;
  $("dl-clear-finished").hidden = !downloads.some((p) => p.finished);
  $("dl-max-label").textContent = state.maxDownloads ? `${state.maxDownloads} at a time` : "At a time";
  renderList($("dl-list"), downloads, "dl");

  const autostarting = Date.now() < autostartUntil
    && (state.collecting || grabber.some((p) => !autostartBefore.has(p.uuid)));
  $("grabber").hidden = !grabber.length && !state.collecting;
  $("grabber-count").textContent = grabber.length ? `(${grabber.length})` : "";
  $("grabber-collecting").hidden = !state.collecting && !autostarting;
  $("grabber-collecting-text").textContent = !autostarting ? "Checking links…"
    : state.collecting ? "Checking links… they'll start downloading by themselves."
    : "Moving checked links to Downloads…";
  $("grabber-download-all").disabled = !grabber.length;
  $("grabber-clear").disabled = !grabber.length;
  renderList($("grabber-list"), grabber, "grab");
}

function renderHeader() {
  const c = state.controller;
  const [label, cls] = CONTROLLER[c] || [c || "Unknown", ""];
  const chip = $("state-chip");
  chip.textContent = label;
  chip.className = `chip ${cls}`;

  const active = (state.downloads || []).reduce((n, p) => n + (p.links || []).filter((l) => l.running).length, 0);
  const speed = $("speed");
  speed.replaceChildren();
  if (c === "RUNNING" || state.speed > 0) {
    speed.append(`↓ ${fmtSpeed(state.speed)}`);
    if (active) speed.append(el("span", "hide-sm", ` · ${active} active`));
  }

  const main = $("btn-main");
  const [icon, text] = c === "RUNNING" ? ["pause", "Pause"] : c === "PAUSE" ? ["play", "Resume"] : ["play", "Start"];
  main.replaceChildren(svgIcon(icon), el("span", "", text));
  main.dataset.mode = c === "RUNNING" ? "pause" : c === "PAUSE" ? "resume" : "start";
  $("btn-stop").disabled = !(c === "RUNNING" || c === "PAUSE");

  $("limit-label").textContent = state.limit.enabled ? fmtSpeed(state.limit.bytes) : "No limit";
}

function itemClass(x) {
  if (x.enabled === false) return "disabled";
  if (FAILED.test(x.status || "")) return "failed";
  if (x.finished) return "finished";
  if (x.running || x.speed > 0) return "running";
  return "";
}

function stateText(x) {
  if (x.status) return x.status;
  if (x.finished) return "Finished";
  if (x.enabled === false) return "Paused";
  if (x.running || x.speed > 0) return "Downloading";
  return "Queued";
}

function percent(x) {
  if (x.finished) return 100;
  return x.bytesTotal > 0 ? Math.min(100, ((x.bytesLoaded || 0) / x.bytesTotal) * 100) : 0;
}

function downloadMeta(x, isPackage) {
  const parts = [];
  const running = x.running || x.speed > 0;
  if (x.finished || !(x.bytesTotal > 0)) parts.push(fmtBytes(x.bytesTotal));
  else parts.push(`${fmtBytes(x.bytesLoaded || 0)} of ${fmtBytes(x.bytesTotal)}`);
  if (running && x.speed > 0) parts.push(fmtSpeed(x.speed));
  if (running && x.eta > 0) parts.push(fmtEta(x.eta));
  if (isPackage && (x.childCount || 0) > 1) parts.push(`${x.childCount} files`);
  return parts;
}

// One <li> per package, reused across polls; children rendered only when expanded.
function renderList(listEl, packages, kind) {
  const cache = listEl._cache || (listEl._cache = new Map());
  const seen = new Set();
  let prev = null;
  for (const p of packages) {
    const key = `${kind}:${p.uuid}`;
    seen.add(key);
    let li = cache.get(key);
    if (!li) {
      li = buildPackage(kind, key);
      cache.set(key, li);
    }
    li._data = p;
    if (kind === "dl") updateDownloadPackage(li, p);
    else updateGrabberPackage(li, p);
    const next = prev ? prev.nextSibling : listEl.firstChild;
    if (next !== li) listEl.insertBefore(li, next);
    prev = li;
  }
  for (const [key, li] of cache) {
    if (!seen.has(key)) {
      li.remove();
      cache.delete(key);
      expanded.delete(key);
    }
  }
}

function buildRow(withExpander) {
  const row = el("div", "row");
  if (withExpander) {
    const exp = el("button", "expander");
    exp.type = "button";
    exp.setAttribute("aria-label", "Show files");
    exp.setAttribute("aria-expanded", "false");
    exp.appendChild(svgIcon("chev"));
    row.appendChild(exp);
  }
  const info = el("div", "info");
  info.append(el("div", "name"), el("div", "bar"), el("div", "meta"));
  info.querySelector(".bar").appendChild(el("span"));
  row.append(info, el("div", "actions"));
  return row;
}

function buildPackage(kind, key) {
  const li = el("li", "item");
  li.dataset.key = key;
  const row = buildRow(true);
  const children = el("ul", "children");
  children.hidden = true;
  li.append(row, children);
  row.querySelector(".expander").addEventListener("click", () => {
    if (expanded.has(key)) expanded.delete(key);
    else expanded.add(key);
    if (kind === "dl") updateDownloadPackage(li, li._data);
    else updateGrabberPackage(li, li._data);
  });
  if (kind === "grab") {
    row.querySelector(".bar").remove();
    row.classList.add("grab");
  }
  return li;
}

function setMeta(metaEl, parts, stateLabel, badges = []) {
  metaEl.replaceChildren();
  if (stateLabel) {
    metaEl.appendChild(el("span", "state", stateLabel));
    if (parts.length) metaEl.appendChild(document.createTextNode(" · "));
  }
  metaEl.appendChild(document.createTextNode(parts.join(" · ")));
  for (const [cls, text] of badges) metaEl.appendChild(el("span", `badge ${cls}`, text));
}

function setExpander(li, key, count) {
  const exp = li.querySelector(".expander");
  const open = expanded.has(key) && count > 0;
  exp.classList.toggle("blank", count === 0);
  exp.setAttribute("aria-expanded", String(open));
  li.querySelector(".children").hidden = !open;
  return open;
}

// --- download list

function updateDownloadPackage(li, p) {
  const links = p.links || [];
  const running = links.some((l) => l.running) || p.speed > 0;
  const view = { ...p, running: p.running || running };
  li.className = `item ${itemClass(view)}`;
  const row = li.firstChild;
  row.querySelector(".name").textContent = p.name || "(unnamed package)";
  row.querySelector(".name").title = p.name || "";
  row.querySelector(".bar > span").style.width = `${percent(p)}%`;
  const label = running ? (p.status && !/download/i.test(p.status) ? p.status : "") : stateText(view);
  setMeta(row.querySelector(".meta"), downloadMeta(view, true), label);

  // Buttons are rebuilt only when their state changes: replacing them on every
  // poll would swallow a tap that lands mid-refresh. Handlers read li._data.
  const actions = row.querySelector(".actions");
  const sig = `${!!p.finished}|${p.enabled !== false}`;
  if (actions.dataset.sig !== sig) {
    actions.dataset.sig = sig;
    actions.replaceChildren();
    if (!p.finished) {
      const paused = p.enabled === false;
      const toggle = iconButton(paused ? "play" : "pause", paused ? "Resume" : "Pause");
      toggle.addEventListener("click", () => act({ action: "dl_enable", enabled: li._data.enabled === false, packageIds: [li._data.uuid] }));
      actions.appendChild(toggle);
    }
    const more = iconButton("more", "More actions");
    more.addEventListener("click", () => openItemMenu("dl", li._data, true));
    actions.appendChild(more);
  }

  if (setExpander(li, li.dataset.key, links.length)) renderDownloadLinks(li.querySelector(".children"), links);
}

function renderDownloadLinks(ul, links) {
  const cache = ul._cache || (ul._cache = new Map());
  const seen = new Set();
  let prev = null;
  for (const l of links) {
    const key = String(l.uuid);
    seen.add(key);
    let li = cache.get(key);
    if (!li) {
      li = el("li", "item");
      li.appendChild(buildRow(false));
      const more = iconButton("more", "More actions");
      more.addEventListener("click", () => openItemMenu("dl", li._data, false));
      li.querySelector(".actions").appendChild(more);
      cache.set(key, li);
    }
    li._data = l;
    li.className = `item ${itemClass(l)}`;
    const row = li.firstChild;
    row.querySelector(".name").textContent = l.name || l.url || "(unnamed file)";
    row.querySelector(".name").title = l.name || "";
    row.querySelector(".bar > span").style.width = `${percent(l)}%`;
    const running = l.running || l.speed > 0;
    setMeta(row.querySelector(".meta"), downloadMeta(l, false), running ? "" : stateText(l));
    const next = prev ? prev.nextSibling : ul.firstChild;
    if (next !== li) ul.insertBefore(li, next);
    prev = li;
  }
  for (const [key, li] of cache) if (!seen.has(key)) { li.remove(); cache.delete(key); }
}

// --- link grabber

// JDownloader adds a per-package sub-folder; start the picker on the folder above it.
function parentOfPackageFolder(p) {
  const save = trimSlash(p.saveTo || "");
  return save.endsWith(`/${p.name}`) ? save.slice(0, -(p.name.length + 1)) : save;
}

function availabilityBadges(p) {
  const badges = [];
  if (p.onlineCount) badges.push(["ok", `${p.onlineCount} online`]);
  if (p.offlineCount) badges.push(["bad", `${p.offlineCount} offline`]);
  const unknown = (p.unknownCount || 0) + (p.tempUnknownCount || 0);
  if (unknown) badges.push(["unk", `${unknown} unchecked`]);
  return badges;
}

function updateGrabberPackage(li, p) {
  const links = p.links || [];
  li.className = "item";
  const row = li.firstChild;
  row.querySelector(".name").textContent = p.name || "(unnamed package)";
  row.querySelector(".name").title = p.name || "";
  const parts = [`${p.childCount || links.length} file${(p.childCount || links.length) === 1 ? "" : "s"}`];
  if (p.bytesTotal > 0) parts.push(fmtBytes(p.bytesTotal));
  if (p.saveTo) parts.push(`→ ${folderLabel(p.saveTo)}`);
  setMeta(row.querySelector(".meta"), parts, "", availabilityBadges(p));

  const actions = row.querySelector(".actions");
  if (!actions.children.length) { // built once; handlers read li._data
    const dl = el("button", "btn primary small", "Download");
    dl.type = "button";
    dl.addEventListener("click", () => act({ action: "grabber_download", packageIds: [li._data.uuid] }, "Moved to downloads"));
    const folder = iconButton("folder", "Change where it saves");
    folder.addEventListener("click", () => openPicker("grabber", parentOfPackageFolder(li._data), [li._data.uuid]));
    const rm = iconButton("x", "Remove from link grabber");
    rm.addEventListener("click", () => act({ action: "grabber_remove", packageIds: [li._data.uuid] }));
    actions.append(dl, folder, rm);
  }

  if (setExpander(li, li.dataset.key, links.length)) {
    const ul = li.querySelector(".children");
    const cache = ul._cache || (ul._cache = new Map());
    const seen = new Set();
    let prev = null;
    for (const l of links) {
      const key = String(l.uuid);
      seen.add(key);
      let item = cache.get(key);
      if (!item) {
        item = el("li", "item");
        const r = buildRow(false);
        r.querySelector(".bar").remove();
        const x = iconButton("x", "Remove this file");
        x.addEventListener("click", () => act({ action: "grabber_remove", linkIds: [item._data.uuid] }));
        r.querySelector(".actions").appendChild(x);
        item.appendChild(r);
        cache.set(key, item);
      }
      item._data = l;
      const r = item.firstChild;
      r.querySelector(".name").textContent = l.name || l.url || "(unnamed file)";
      r.querySelector(".name").title = l.name || "";
      const avail = l.availability || "UNKNOWN";
      const badge = avail === "ONLINE" ? ["ok", "Online"] : avail === "OFFLINE" ? ["bad", "Offline"] : ["unk", "Checking"];
      setMeta(r.querySelector(".meta"), [l.bytesTotal > 0 ? fmtBytes(l.bytesTotal) : l.host || ""].filter(Boolean), "", [badge]);
      const next = prev ? prev.nextSibling : ul.firstChild;
      if (next !== item) ul.insertBefore(item, next);
      prev = item;
    }
    for (const [key, item] of cache) if (!seen.has(key)) { item.remove(); cache.delete(key); }
  }
}

// ---------------------------------------------------------------- item menu + remove

function idsFor(x, isPackage) {
  return isPackage ? { packageIds: [x.uuid] } : { linkIds: [x.uuid] };
}

function openItemMenu(kind, x, isPackage) {
  const dlg = $("dlg-item");
  $("item-title").textContent = x.name || "Download";
  $("item-sub").textContent = [isPackage ? "Package" : "File", stateText(x), fmtBytes(x.bytesTotal)].filter(Boolean).join(" · ");
  const menu = $("item-menu");
  menu.replaceChildren();
  const add = (label, hint, fn, cls = "") => {
    const b = el("button", cls);
    b.type = "button";
    b.append(el("span", "", label));
    if (hint) b.append(el("small", "", hint));
    b.addEventListener("click", () => { dlg.close(); fn(); });
    menu.appendChild(b);
  };
  const ids = idsFor(x, isPackage);
  if (!x.finished) {
    if (x.enabled === false) add("Resume", "", () => act({ action: "dl_enable", enabled: true, ...ids }));
    else add("Pause", "", () => act({ action: "dl_enable", enabled: false, ...ids }));
    add("Start now", "skips the queue", () => act({ action: "dl_force", ...ids }, "Starting now"));
  }
  add("Restart from scratch", "discards what's downloaded", () => act({ action: "dl_reset", ...ids }, "Restarted"));
  add("Remove…", "", () => openRemove(x, isPackage), "danger");
  dlg.showModal();
}

let pendingRemove = null;
function openRemove(x, isPackage) {
  pendingRemove = { x, isPackage };
  $("remove-text").textContent = `“${x.name || "This download"}” will be removed from the list.`;
  $("remove-files").checked = false;
  $("dlg-remove").showModal();
}
$("dlg-remove").addEventListener("close", () => {
  const dlg = $("dlg-remove");
  if (dlg.returnValue === "remove" && pendingRemove) {
    const { x, isPackage } = pendingRemove;
    const deleteFiles = $("remove-files").checked;
    act({ action: "dl_remove", deleteFiles, ...idsFor(x, isPackage) }, deleteFiles ? "Removed and files deleted" : "Removed from list");
  }
  pendingRemove = null;
  dlg.returnValue = "";
});

// ---------------------------------------------------------------- header actions

$("btn-main").addEventListener("click", () => {
  const mode = $("btn-main").dataset.mode;
  if (mode === "pause") act({ action: "pause", value: true });
  else if (mode === "resume") act({ action: "pause", value: false });
  else act({ action: "start" });
});
$("btn-stop").addEventListener("click", () => act({ action: "stop" }));

// Speed limit
let limitChoice = 0;
function markLimitChoice() {
  for (const b of $("limit-choices").children) b.setAttribute("aria-pressed", String(Number(b.dataset.mb) === limitChoice));
}
for (const mb of LIMIT_CHOICES) {
  const b = el("button", "", mb ? `${mb} MB/s` : "None");
  b.type = "button";
  b.dataset.mb = mb;
  b.addEventListener("click", () => { limitChoice = mb; $("limit-custom").value = ""; markLimitChoice(); });
  $("limit-choices").appendChild(b);
}
$("limit-custom").addEventListener("input", () => { limitChoice = -1; markLimitChoice(); });
$("btn-limit").addEventListener("click", () => {
  const mb = state && state.limit.enabled ? state.limit.bytes / (1024 * 1024) : 0;
  limitChoice = LIMIT_CHOICES.includes(mb) ? mb : -1;
  $("limit-custom").value = limitChoice === -1 ? +mb.toFixed(2) : "";
  markLimitChoice();
  $("dlg-limit").showModal();
});
$("dlg-limit").addEventListener("close", () => {
  const dlg = $("dlg-limit");
  if (dlg.returnValue !== "save") return;
  dlg.returnValue = "";
  const mb = limitChoice === -1 ? parseFloat($("limit-custom").value) : limitChoice;
  if (!(mb > 0)) return act({ action: "speed_limit", enabled: false }, "Speed limit off");
  act({ action: "speed_limit", enabled: true, bytes: Math.round(mb * 1024 * 1024) }, `Limited to ${fmtSpeed(mb * 1024 * 1024)}`);
});

// Downloads at a time (JDownloader's MaxSimultaneDownloads; the rest queue)
let maxChoice = 3;
function markMaxChoice() {
  for (const b of $("max-choices").children) b.setAttribute("aria-pressed", String(Number(b.dataset.n) === maxChoice));
}
for (const n of MAX_CHOICES) {
  const b = el("button", "", String(n));
  b.type = "button";
  b.dataset.n = n;
  b.addEventListener("click", () => { maxChoice = n; markMaxChoice(); });
  $("max-choices").appendChild(b);
}
$("dl-max").addEventListener("click", () => {
  maxChoice = (state && state.maxDownloads) || 3;
  markMaxChoice();
  $("dlg-max").showModal();
});
$("dlg-max").addEventListener("close", () => {
  const dlg = $("dlg-max");
  if (dlg.returnValue !== "save") return;
  dlg.returnValue = "";
  if (state && maxChoice === state.maxDownloads) return;
  act({ action: "max_downloads", value: maxChoice }, `${maxChoice} download${maxChoice === 1 ? "" : "s"} at a time, the rest wait in the queue`);
});

// Account
$("btn-menu").addEventListener("click", () => {
  for (const id of ["pw-current", "pw-new", "pw-repeat"]) $(id).value = "";
  $("pw-error").textContent = "";
  $("pw-managed").hidden = !passwordManaged;
  $("pw-fields").hidden = passwordManaged;
  $("pw-change").hidden = passwordManaged;
  $("dlg-account").showModal();
});
$("account-form").addEventListener("submit", async (e) => {
  const action = e.submitter && e.submitter.value;
  if (action === "logout") {
    e.preventDefault();
    $("dlg-account").close();
    try { await api("/api/logout", {}); } catch { /* signed out locally anyway */ }
    showAuth("login");
    return;
  }
  if (action !== "change") return; // Close: let the dialog close
  e.preventDefault();
  const err = $("pw-error");
  const next = $("pw-new").value;
  if (next.length < 8) { err.textContent = "Use at least 8 characters."; return; }
  if (next !== $("pw-repeat").value) { err.textContent = "The new passwords don't match."; return; }
  try {
    await api("/api/password", { current: $("pw-current").value, new: next });
    $("dlg-account").close();
    toast("Password changed");
  } catch (ex) {
    err.textContent = ex.message;
  }
});

// ---------------------------------------------------------------- folders ("Save to")
//
// /api/folders returns what nas/jdweb-folders.py publishes every minute: one
// root per writable mount of the JDownloader app, with its sub-folders as
// relative paths. Paths are JDownloader's own (inside its container).

let folders = null;
let chosenFolder = recall("jdweb.folder") || ""; // "" = JDownloader's default folder
let pendingNewFolder = ""; // "New folder inside" for the next Add only
const picker = { path: null, mode: "add", packageIds: null };

const trimSlash = (p) => p.replace(/\/+$/, "");

function rootFor(path) {
  let best = null;
  for (const r of (folders && folders.roots) || []) {
    const base = trimSlash(r.path);
    if ((path === base || path.startsWith(`${base}/`)) && (!best || base.length > trimSlash(best.path).length)) best = r;
  }
  return best;
}
const relOf = (root, path) => (path === trimSlash(root.path) ? "" : path.slice(trimSlash(root.path).length + 1));

function folderKnown(path) {
  const r = rootFor(path);
  if (!r) return false;
  const rel = relOf(r, path);
  return rel === "" || r.dirs.includes(rel);
}

function folderLabel(path) {
  if (!path) {
    const d = ((folders && folders.roots) || []).find((r) => r.default);
    return d ? d.label : "Default folder";
  }
  const r = rootFor(trimSlash(path));
  if (!r) return path;
  const rel = relOf(r, trimSlash(path));
  return rel ? `${r.label} › ${rel.split("/").join(" › ")}` : r.label;
}

function renderFolderButton() {
  $("folder-label").textContent = folderLabel(chosenFolder);
  $("folder-btn").title = chosenFolder || (folders && folders.default) || "";
}

async function loadFolders() {
  try {
    folders = await api("/api/folders");
  } catch {
    return; // keep the last list
  }
  if (chosenFolder && !folderKnown(chosenFolder)) {
    chosenFolder = "";
    store("jdweb.folder", "");
  }
  renderFolderButton();
}

function childrenOf(path) {
  const r = rootFor(path);
  if (!r) return [];
  const rel = relOf(r, path);
  const prefix = rel ? `${rel}/` : "";
  return r.dirs.filter((d) => d.startsWith(prefix) && !d.slice(prefix.length).includes("/"));
}

function folderItem(label, sub, hasChildren, onClick) {
  const li = el("li");
  const b = el("button");
  b.type = "button";
  const txt = el("span", "txt");
  txt.append(el("span", "", label));
  if (sub) txt.append(el("small", "", sub));
  const ico = svgIcon("folder");
  ico.setAttribute("class", "ico");
  b.append(ico, txt);
  if (hasChildren) {
    const chev = svgIcon("chev");
    chev.setAttribute("class", "chev");
    b.append(chev);
  }
  b.addEventListener("click", onClick);
  li.appendChild(b);
  return li;
}

function renderPicker() {
  const list = $("folder-list");
  const crumbs = $("folder-crumbs");
  const empty = $("folder-empty");
  const query = $("folder-search").value.trim().toLowerCase();
  const roots = (folders && folders.roots) || [];
  list.replaceChildren();
  crumbs.replaceChildren();
  empty.hidden = true;

  if (query) {
    crumbs.hidden = true;
    let shown = 0;
    for (const r of roots) {
      for (const rel of r.dirs) {
        if (shown >= 80) break;
        if (!rel.toLowerCase().includes(query)) continue;
        const full = `${trimSlash(r.path)}/${rel}`;
        const name = rel.split("/").pop();
        list.appendChild(folderItem(name, folderLabel(full), false, () => {
          $("folder-search").value = "";
          picker.path = full;
          renderPicker();
        }));
        shown++;
      }
    }
    if (!shown) { empty.textContent = "No folder matches that."; empty.hidden = false; }
    $("folder-choose").disabled = true;
    return;
  }

  crumbs.hidden = false;
  const crumb = (text, path, current) => {
    const b = el("button", "", text);
    b.type = "button";
    if (current) b.setAttribute("aria-current", "true");
    else b.addEventListener("click", () => { picker.path = path; renderPicker(); });
    crumbs.appendChild(b);
  };
  crumb("All locations", null, picker.path === null);

  if (picker.path === null) {
    for (const r of roots) {
      list.appendChild(folderItem(r.default ? `${r.label} (default)` : r.label, r.host, true, () => {
        picker.path = trimSlash(r.path);
        renderPicker();
      }));
    }
    if (!roots.length) {
      empty.textContent = "No folder list yet. JDownloader will use its default folder.";
      empty.hidden = false;
    }
  } else {
    const r = rootFor(picker.path);
    const rel = relOf(r, picker.path);
    const parts = rel ? rel.split("/") : [];
    crumbs.append(el("span", "sep", "›"));
    crumb(r.label, trimSlash(r.path), !parts.length);
    parts.forEach((part, i) => {
      crumbs.append(el("span", "sep", "›"));
      crumb(part, `${trimSlash(r.path)}/${parts.slice(0, i + 1).join("/")}`, i === parts.length - 1);
    });
    for (const child of childrenOf(picker.path)) {
      const full = `${trimSlash(r.path)}/${child}`;
      list.appendChild(folderItem(child.split("/").pop(), "", childrenOf(full).length > 0, () => {
        picker.path = full;
        renderPicker();
      }));
    }
    if (!list.children.length) { empty.textContent = "No sub-folders here."; empty.hidden = false; }
  }
  $("folder-choose").disabled = picker.path === null;
}

async function openPicker(mode, startPath, packageIds = null) {
  picker.mode = mode;
  picker.packageIds = packageIds;
  $("folder-title").textContent = mode === "grabber" ? "Move package to" : "Save to";
  $("folder-search").value = "";
  $("folder-new").value = "";
  await loadFolders();
  const start = startPath ? trimSlash(startPath) : "";
  const fallback = ((folders && folders.roots) || []).find((r) => r.default);
  picker.path = start && folderKnown(start) ? start
    : start && rootFor(start) ? trimSlash(rootFor(start).path)
    : fallback ? trimSlash(fallback.path) : null;
  renderPicker();
  $("dlg-folder").showModal();
}

$("folder-btn").addEventListener("click", () => openPicker("add", chosenFolder || (folders && folders.default)));
$("folder-search").addEventListener("input", renderPicker);
$("folder-form").addEventListener("submit", (e) => {
  if (!e.submitter || e.submitter.value !== "choose") return; // Cancel closes normally
  const path = picker.path;
  const newFolder = $("folder-new").value.trim();
  if (newFolder && (newFolder.startsWith(".") || /[/\\]/.test(newFolder))) {
    e.preventDefault();
    toast("A new folder name can't contain / or start with a dot.", true);
    return;
  }
  if (picker.mode === "grabber") {
    act({ action: "grabber_folder", packageIds: picker.packageIds, folder: path, newFolder }, `Will save to ${folderLabel(path)}${newFolder ? ` › ${newFolder}` : ""}`);
    return;
  }
  const r = rootFor(path);
  chosenFolder = r && r.default && relOf(r, path) === "" ? "" : path;
  pendingNewFolder = newFolder;
  store("jdweb.folder", chosenFolder);
  renderFolderButton();
  if (newFolder) $("folder-label").textContent += ` › ${newFolder} (new)`;
});

// ---------------------------------------------------------------- add links

const autostart = $("add-autostart");
autostart.checked = recall("jdweb.autostart") !== "0";
autostart.addEventListener("change", () => store("jdweb.autostart", autostart.checked ? "1" : "0"));
$("add-more").addEventListener("click", () => {
  const extra = $("add-extra");
  extra.hidden = !extra.hidden;
  $("add-more").textContent = extra.hidden ? "More options" : "Fewer options";
});
$("add-links").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("add-form").requestSubmit();
});
$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const links = $("add-links").value.trim();
  if (!links) { $("add-links").focus(); return; }
  const count = links.split(/\s+/).filter((s) => /^[a-z][a-z0-9+.-]*:\/\//i.test(s)).length;
  $("add-submit").disabled = true;
  const before = new Set(((state && state.grabber) || []).map((p) => p.uuid)); // before a poll can show the new ones
  try {
    await api("/api/do", {
      action: "add",
      links,
      autostart: autostart.checked,
      packageName: $("add-package").value,
      extractPassword: $("add-password").value,
      folder: chosenFolder,
      newFolder: pendingNewFolder,
    });
    $("add-links").value = "";
    $("add-package").value = "";
    $("add-password").value = "";
    const where = `${folderLabel(chosenFolder)}${pendingNewFolder ? ` › ${pendingNewFolder}` : ""}`;
    pendingNewFolder = "";
    renderFolderButton();
    const what = count ? `${count} link${count === 1 ? "" : "s"}` : "Links";
    autostartUntil = autostart.checked ? Date.now() + AUTOSTART_HINT_MS : 0;
    autostartBefore = before;
    toast(autostart.checked ? `${what} added, starting once checked (${where})` : `${what} added to the link grabber (${where})`);
  } catch (ex) {
    if (ex.status !== 401) toast(ex.message, true);
  } finally {
    $("add-submit").disabled = false;
    refresh();
    setTimeout(refresh, 1500);
  }
});

// ---------------------------------------------------------------- list actions

$("grabber-download-all").addEventListener("click", () => act({ action: "grabber_download" }, "Moved to downloads"));
$("grabber-clear").addEventListener("click", () => {
  if (confirm("Remove everything from the link grabber?")) act({ action: "grabber_clear" });
});
$("dl-clear-finished").addEventListener("click", () => act({ action: "dl_clear_finished" }, "Finished downloads cleared (files kept)"));

// ---------------------------------------------------------------- start

(async function boot() {
  try {
    const s = await api("/api/session");
    passwordManaged = !!s.passwordManaged;
    if (s.authenticated) startApp();
    else showAuth(s.setup ? "setup" : "login");
  } catch (e) {
    showAuth("login");
    $("auth-error").textContent = e.message;
  }
})();
