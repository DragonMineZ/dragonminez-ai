"use strict";
// Panel core: tiny DOM helper, API client, hash router. Modules call Panel.page({...}).
// Never use innerHTML with data — h() only creates text nodes from strings.

const Panel = (() => {
  const pages = [];
  // Sidebar layout. A null group = top-level links; ids not registered or not permitted are skipped.
  // Pages registered but missing here stay routable (aliases, hidden pages).
  const NAV = [
    [null, ["dashboard"]],
    ["Moderation", ["users", "audit", "joiner-alerts", "automod"]],
    ["Support & Community", ["tickets", "announce", "templates", "patreon"]],
    ["System", ["settings", "overview", "staff", "logs"]],
  ];
  const collapsed = new Set();  // nav groups the user closed this session
  let me = null;
  let guildCache = null;

  // ---- i18n: English is the key; modules register es-MX strings with Panel.i18n({...}). -------
  const LANG_KEY = "panel.lang";
  const LANGS = { en: "English", es: "Español (MX)" };
  const dict = {};
  let lang = (() => { try { return localStorage.getItem(LANG_KEY); } catch { return null; } })()
    || (navigator.language.toLowerCase().startsWith("es") ? "es" : "en");
  if (!LANGS[lang]) lang = "en";

  function i18n(entries) {
    Object.assign(dict, entries);
  }

  // t("Banned {name}", {name}) — placeholders survive translation so word order can change.
  function t(text, vars) {
    const out = (lang === "es" && dict[text]) || text;
    return vars ? out.replace(/\{(\w+)\}/g, (m, key) => (key in vars ? String(vars[key]) : m)) : out;
  }

  function locale() {
    return lang === "es" ? "es-MX" : "en-US";
  }

  function setLang(next) {
    lang = LANGS[next] ? next : "en";
    try { localStorage.setItem(LANG_KEY, lang); } catch { /* private mode */ }
    translateStatic();
    labelLangDock();
    if (me) {
      document.getElementById("me-tier").textContent = t(me.tier);
      route();
    }
  }

  function translateStatic() {
    document.documentElement.lang = locale();
    for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
    for (const el of document.querySelectorAll("[data-i18n-label]")) el.setAttribute("aria-label", t(el.dataset.i18nLabel));
  }

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
      else if (key === "class") el.className = value;
      else if (key === "value") el.value = value;
      else if (key === "checked") el.checked = Boolean(value);
      else el.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  async function api(path, { method = "GET", body } = {}) {
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (response.status === 401) { showLogin(); throw new Error(t("Session expired, log in again.")); }
    if (!response.ok) throw new Error((data && data.error) || t("Request failed ({status})", { status: response.status }));
    return data;
  }

  function toast(message, isError = false) {
    const el = h("div", { class: isError ? "toast error" : "toast", role: isError ? "alert" : "status" }, message);
    document.getElementById("toasts").append(el);
    setTimeout(() => el.remove(), isError ? 7000 : 3500);
  }

  // Wraps a button click: disables it, runs fn, toasts success/error.
  async function run(button, fn, successMessage) {
    if (button) button.disabled = true;
    try {
      const result = await fn();
      if (successMessage) toast(successMessage);
      return result;
    } catch (error) {
      toast(error.message, true);
      return undefined;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function can(permission) {
    return Boolean(me && me.permissions.includes(permission));
  }

  function time(iso) {
    if (!iso) return h("span", { class: "muted" }, "—");
    const date = new Date(iso);
    return h("time", { datetime: date.toISOString(), title: date.toLocaleString(locale()) }, relative(date));
  }

  function relative(date) {
    const seconds = Math.round((date.getTime() - Date.now()) / 1000);
    const units = [["year", 31536000], ["day", 86400], ["hour", 3600], ["minute", 60]];
    const format = new Intl.RelativeTimeFormat(locale(), { numeric: "auto", style: "short" });
    for (const [unit, size] of units) {
      if (Math.abs(seconds) >= size) return format.format(Math.trunc(seconds / size), unit);
    }
    return t("just now");
  }

  // u: {id, name, display_name, avatar} from user_json(), or just an id string.
  function user(u) {
    if (!u) return h("span", { class: "muted" }, "—");
    if (typeof u === "string") return h("span", { class: "mono" }, u);
    return h("span", { class: "user", title: `${u.name} (${u.id})` },
      u.avatar ? h("img", { src: u.avatar, alt: "" }) : null, u.display_name || u.name);
  }

  function badge(text, kind = "") {
    return h("span", { class: `badge ${kind}` }, text);
  }

  // columns: [{label, render: row => Node|string}]. Clickable tables get a hint and a hover chevron.
  function table(columns, rows, { onRowClick, empty = t("Nothing here."), hint = t("Click a row to open it.") } = {}) {
    if (!rows.length) return h("div", { class: "empty" }, empty);
    return h("div", { class: "table-wrap" }, onRowClick && hint ? h("div", { class: "muted small hint" }, hint) : null, h("table", {},
      h("thead", {}, h("tr", {}, columns.map((c) => h("th", {}, c.label)))),
      h("tbody", {}, rows.map((row) => h("tr", {
        class: onRowClick ? "clickable" : null,
        tabindex: onRowClick ? "0" : null,
        onclick: onRowClick ? () => onRowClick(row) : null,
        onkeydown: onRowClick ? (e) => { if (e.key === "Enter") onRowClick(row); } : null,
      }, columns.map((c) => h("td", {}, c.render(row))))))));
  }

  function field(labelText, input) {
    const control = input.matches("input, select, textarea") ? input : input.querySelector("input, select, textarea") || input;
    const id = control.id || `f-${Math.random().toString(36).slice(2)}`;
    control.id = id;
    return h("div", {}, h("label", { for: id }, labelText), input);
  }

  // Native <datalist> dropdown that still allows free typing. source: fixed list, or
  // async q => [{value, label}] re-queried as the user types. Returns a wrapper holding both.
  function suggest(input, source) {
    const list = h("datalist", { id: `dl-${Math.random().toString(36).slice(2)}` });
    input.setAttribute("list", list.id);
    input.setAttribute("autocomplete", "off");
    const fill = (items) => list.replaceChildren(...items.map((o) => (typeof o === "string"
      ? h("option", { value: o })
      : h("option", { value: o.value, label: o.label }))));
    if (Array.isArray(source)) fill(source);
    else {
      let timer;
      let seq = 0;
      input.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          const q = input.value.trim();
          const mine = ++seq;
          if (!q) { fill([]); return; }
          try {
            const items = await source(q);
            if (mine === seq) fill(items);
          } catch { /* suggestions are best-effort */ }
        }, 200);
      });
    }
    return h("span", { class: "suggest" }, input, list);
  }

  // Member name/ID box: lists matching members while typing; picking one fills in their ID.
  function userSuggest(input) {
    return suggest(input, async (q) => (await api(`/api/users/search?q=${encodeURIComponent(q)}`)).results
      .slice(0, 15).map((u) => ({ value: u.id, label: `${u.display_name} (@${u.name})` })));
  }

  // Modal with arbitrary body; resolves true when the primary action is clicked.
  function dialog(title, body, { confirmLabel = t("Confirm"), danger = false } = {}) {
    return new Promise((resolve) => {
      const el = h("dialog", {},
        h("h2", {}, title),
        h("div", { class: "stack" }, body),
        h("div", { class: "row end" },
          h("button", { class: "btn ghost", type: "button", onclick: () => el.close("cancel") }, t("Cancel")),
          h("button", { class: danger ? "btn danger" : "btn primary", type: "button", onclick: () => el.close("ok") }, confirmLabel)));
      el.addEventListener("close", () => { resolve(el.returnValue === "ok"); el.remove(); });
      document.body.append(el);
      el.showModal();
    });
  }

  // Click-to-preview member chip: shows who they are and their moderation history without leaving the page.
  function mention(id, label) {
    return h("button", {
      class: "mention", type: "button", title: String(id),
      onclick: (e) => { e.stopPropagation(); openUserCard(String(id)); },
    }, `@${label || id}`);
  }

  async function openUserCard(id) {
    const body = h("div", { class: "stack" }, h("span", { class: "muted" }, t("Loading…")));
    const el = h("dialog", { class: "user-card" }, body,
      h("div", { class: "row end" },
        h("button", { class: "btn ghost", type: "button", onclick: () => el.close() }, t("Close")),
        h("button", { class: "btn primary", type: "button", onclick: () => { el.close(); go(`#/users/${encodeURIComponent(id)}`); } }, t("Open profile"))));
    el.addEventListener("close", () => el.remove());
    document.body.append(el);
    el.showModal();
    try {
      const p = await api(`/api/users/${encodeURIComponent(id)}`);
      const cases = (p.sections.cases && p.sections.cases.data) || [];
      const counts = {};
      for (const c of cases) if (c.active !== false) counts[c.action] = (counts[c.action] || 0) + 1;
      body.replaceChildren(
        h("div", { class: "row" }, user(p.user), p.member ? null : badge(t("Not in server"), "warn"),
          p.banned ? badge(t("Banned"), "danger") : null, p.timed_out_until ? badge(t("Timed out"), "danger") : null),
        h("div", { class: "small muted" }, `${t("Account created")}: `, time(p.created_at), p.joined_at ? ` · ${t("Joined")}: ` : null, p.joined_at ? time(p.joined_at) : null),
        h("div", { class: "row" }, Object.keys(counts).length
          ? Object.entries(counts).map(([action, n]) => badge(`${n}× ${action}`, action === "warn" ? "warn" : ""))
          : h("span", { class: "muted small" }, t("No recent cases."))),
        cases.slice(0, 5).map((c) => h("div", { class: "small" }, badge(c.action), " ", c.reason || "—", " ", time(c.created_at))),
        Panel.actionBar ? Panel.actionBar(p, { compact: true, onDone: () => { el.close(); route(); } }) : null);
    } catch (error) {
      body.replaceChildren(h("span", { class: "muted" }, error.message));
    }
  }

  // Turns <@id> and bare Discord IDs (17-20 digits) in log text into clickable member chips.
  function richText(text) {
    const out = [];
    let last = 0;
    for (const m of String(text || "").matchAll(/<@!?(\d{17,20})>|\b(\d{17,20})\b/g)) {
      out.push(text.slice(last, m.index), mention(m[1] || m[2]));
      last = m.index + m[0].length;
    }
    out.push(String(text || "").slice(last));
    return out;
  }

  async function guild() {
    if (!guildCache) guildCache = await api("/api/guild");
    return guildCache;
  }

  async function channelName(id) {
    const c = (await guild()).channels.find((x) => x.id === String(id));
    return c ? `#${c.name}` : String(id);
  }

  async function roleName(id) {
    const r = (await guild()).roles.find((x) => x.id === String(id));
    return r ? `@${r.name}` : String(id);
  }

  // Discord users aren't in guild() (that's channels/roles only), so this hits /api/users/{id} directly.
  async function userName(id) {
    try { return `@${(await api(`/api/users/${id}`)).user.display_name}`; } catch { return String(id); }
  }

  function channelSelect(selected, { types = ["text", "news", "forum"], includeNone = false } = {}) {
    const select = h("select", {}, includeNone ? h("option", { value: "" }, t("— none —")) : null);
    guild().then((g) => {
      for (const c of g.channels.filter((x) => types.includes(x.type)).sort((a, b) => a.position - b.position)) {
        select.append(h("option", { value: c.id }, `#${c.name}${c.category ? ` (${c.category})` : ""}`));
      }
      if (selected) select.value = String(selected);
    });
    return select;
  }

  function page(def) {
    pages.push(def);
  }

  function go(hash) {
    location.hash = hash;
  }

  function showLogin() {
    document.getElementById("app").hidden = true;
    document.getElementById("login").hidden = false;
  }

  function navGroups() {
    return NAV.map(([group, ids]) => [group, ids.map((id) => pages.find((p) => p.id === id))
      .filter((p) => p && !p.hidden && can(p.perm))]).filter(([, members]) => members.length);
  }

  // Each NAV group folds into one dropdown. The panel's height animates via CSS grid-template-rows
  // (0fr <-> 1fr), so opening and closing both slide instead of an instant toggle.
  function renderNav(current) {
    const link = (p) => h("a", { href: `#/${p.id}`, "aria-current": p.id === current ? "page" : null }, t(p.title));
    const items = [];
    for (const [group, members] of navGroups()) {
      if (!group) { items.push(...members.map(link)); continue; }
      const open = members.some((x) => x.id === current) || !collapsed.has(group);
      const panel = h("div", { class: "nav-group-panel" }, h("div", { class: "nav-group-inner" }, members.map(link)));
      const toggle = h("button", { class: "nav-group-toggle", type: "button", "aria-expanded": String(open) }, t(group));
      toggle.addEventListener("click", () => {
        const next = toggle.getAttribute("aria-expanded") !== "true";
        toggle.setAttribute("aria-expanded", String(next));
        if (next) collapsed.delete(group); else collapsed.add(group);
      });
      items.push(h("div", { class: "nav-group" }, toggle, panel));
    }
    document.getElementById("nav").replaceChildren(...items);
  }

  // Mobile (<=760px) off-canvas sidebar; CSS hides the top bar and backdrop on desktop.
  function setDrawer(open) {
    document.getElementById("app").classList.toggle("nav-open", open);
    document.getElementById("menu-toggle").setAttribute("aria-expanded", String(open));
    document.getElementById("backdrop").hidden = !open;
  }

  function mountDrawer() {
    const toggle = document.getElementById("menu-toggle");
    const isOpen = () => toggle.getAttribute("aria-expanded") === "true";
    toggle.addEventListener("click", () => {
      setDrawer(!isOpen());
      if (isOpen()) { const first = document.querySelector("#nav a, #nav button"); if (first) first.focus(); }
    });
    document.getElementById("backdrop").addEventListener("click", () => setDrawer(false));
    document.getElementById("nav").addEventListener("click", (e) => { if (e.target.closest("a")) setDrawer(false); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && isOpen()) { setDrawer(false); toggle.focus(); }
    });
  }

  async function route() {
    const [id, ...args] = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
    const permitted = pages.filter((p) => can(p.perm));
    const target = permitted.find((p) => p.id === id)
      || navGroups().flatMap(([, members]) => members)[0] || permitted.find((p) => !p.hidden);
    setDrawer(false);
    renderNav(target && target.id);
    // A fresh container per navigation: a slow render from the previous page keeps appending to
    // its own detached container instead of this one.
    const view = h("div", { class: "page" });
    // Pages call view.append(...) directly; accept what h() accepts (arrays, null) instead of the
    // native behaviour of printing "[object HTMLDivElement],…" or "null".
    view.append = (...nodes) => Element.prototype.append.apply(view,
      nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false));
    const main = document.getElementById("view");
    main.replaceChildren(view);
    if (!target) { view.append(h("div", { class: "empty" }, t("No pages available for your tier."))); return; }
    document.title = `${t(target.title)} · BulmaAI Panel`;
    try {
      await target.render(view, args.filter(Boolean));
    } catch (error) {
      view.append(h("p", { class: "error" }, error.message));
    }
    if (view.isConnected) main.focus({ preventScroll: true });
  }

  // Floating language switch: parked mostly off-screen bottom-right, slides in when the pointer
  // gets near (or it gets focus/tapped). Both flags show; the current language's half is dimmed.
  const langDock = h("div", { class: "lang-dock" },
    h("span", { class: "lang-tip", "aria-hidden": "true" }),
    h("button", { class: "lang-fab", type: "button" }, h("span", { class: "flag us" }), h("span", { class: "flag es" })));
  const LANG_NEAR_PX = 150;

  function labelLangDock() {
    const text = lang === "es" ? "Cambia a inglés" : "Change to Spanish";
    langDock.dataset.lang = lang;
    langDock.firstChild.textContent = text;
    langDock.lastChild.setAttribute("aria-label", text);
  }

  function mountLangDock() {
    labelLangDock();
    const near = (on) => langDock.classList.toggle("near", on);
    const fab = langDock.lastChild;
    document.addEventListener("pointermove", (e) => {
      if (e.pointerType !== "mouse") return;
      near(Math.hypot(innerWidth - e.clientX, innerHeight - e.clientY) < LANG_NEAR_PX);
    }, { passive: true });
    fab.addEventListener("focus", () => { if (fab.matches(":focus-visible")) near(true); });
    fab.addEventListener("blur", () => near(false));
    document.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "mouse" && !langDock.contains(e.target)) near(false);
    });
    // Touch has no hover: the first tap on the peeking half pulls it out, the second switches.
    fab.addEventListener("click", () => {
      if (!langDock.classList.contains("near")) { near(true); return; }
      setLang(lang === "es" ? "en" : "es");
    });
    document.body.append(langDock);
  }

  async function boot() {
    mountLangDock();
    translateStatic();
    try {
      me = await api("/api/me");
    } catch {
      showLogin();
      return;
    }
    document.getElementById("login").hidden = true;
    document.getElementById("app").hidden = false;
    document.getElementById("guild-name").textContent = me.guild.name;
    document.getElementById("topbar-guild").textContent = me.guild.name;
    mountDrawer();
    if (me.guild.icon) document.getElementById("guild-icon").src = me.guild.icon;
    document.getElementById("me-avatar").src = me.user.avatar;
    document.getElementById("me-name").textContent = me.user.display_name;
    document.getElementById("me-tier").textContent = t(me.tier);
    document.getElementById("logout").addEventListener("click", async () => {
      await api("/auth/logout", { method: "POST" }).catch(() => {});
      location.reload();
    });
    window.addEventListener("hashchange", route);
    route();
  }

  i18n({
    "Session expired, log in again.": "La sesión expiró, vuelve a iniciar sesión.",
    "Request failed ({status})": "La solicitud falló ({status})",
    "just now": "justo ahora",
    "Nothing here.": "No hay nada aquí.",
    "Click a row to open it.": "Haz clic en una fila para abrirla.",
    "Cancel": "Cancelar",
    "Confirm": "Confirmar",
    "— none —": "— ninguno —",
    "No pages available for your tier.": "No hay páginas disponibles para tu rango.",
    "Log out": "Cerrar sesión",
    "Log in with Discord": "Iniciar sesión con Discord",
    "Remember me for 30 days": "Recordarme por 30 días",
    "DragonMineZ staff only.": "Solo para el staff de DragonMineZ.",
    "owner": "dueño",
    "admin": "admin",
    "moderator": "moderador",
    "helper": "helper",
    "Logs": "Registros",
    "Moderation": "Moderación",
    "Support & Community": "Soporte y comunidad",
    "System": "Sistema",
    "Menu": "Menú",
  });

  document.addEventListener("DOMContentLoaded", boot);

  return {
    h, api, toast, run, can, time, user, badge, mention, richText, table, field, dialog, guild, suggest, userSuggest,
    channelName, roleName, userName, channelSelect, page, go, me: () => me, refresh: route, openUserCard,
    t, i18n, locale, lang: () => lang,
  };
})();
