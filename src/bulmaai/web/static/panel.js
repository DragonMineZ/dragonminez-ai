"use strict";
// Panel core: tiny DOM helper, API client, hash router. Modules call Panel.page({...}).
// Never use innerHTML with data — h() only creates text nodes from strings.

const Panel = (() => {
  const pages = [];
  let me = null;
  let guildCache = null;

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
    if (response.status === 401) { showLogin(); throw new Error("Session expired, log in again."); }
    if (!response.ok) throw new Error((data && data.error) || `Request failed (${response.status})`);
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
    return h("time", { datetime: date.toISOString(), title: date.toLocaleString() }, relative(date));
  }

  function relative(date) {
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    const abs = Math.abs(seconds);
    const units = [["y", 31536000], ["d", 86400], ["h", 3600], ["m", 60]];
    for (const [unit, size] of units) {
      if (abs >= size) return seconds >= 0 ? `${Math.floor(abs / size)}${unit} ago` : `in ${Math.floor(abs / size)}${unit}`;
    }
    return "just now";
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

  // columns: [{label, render: row => Node|string}]
  function table(columns, rows, { onRowClick, empty = "Nothing here." } = {}) {
    if (!rows.length) return h("div", { class: "empty" }, empty);
    return h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, columns.map((c) => h("th", {}, c.label)))),
      h("tbody", {}, rows.map((row) => h("tr", {
        class: onRowClick ? "clickable" : null,
        tabindex: onRowClick ? "0" : null,
        onclick: onRowClick ? () => onRowClick(row) : null,
        onkeydown: onRowClick ? (e) => { if (e.key === "Enter") onRowClick(row); } : null,
      }, columns.map((c) => h("td", {}, c.render(row))))))));
  }

  function field(labelText, input) {
    const id = input.id || `f-${Math.random().toString(36).slice(2)}`;
    input.id = id;
    return h("div", {}, h("label", { for: id }, labelText), input);
  }

  // Modal with arbitrary body; resolves true when the primary action is clicked.
  function dialog(title, body, { confirmLabel = "Confirm", danger = false } = {}) {
    return new Promise((resolve) => {
      const el = h("dialog", {},
        h("h2", {}, title),
        h("div", { class: "stack" }, body),
        h("div", { class: "row end" },
          h("button", { class: "btn ghost", type: "button", onclick: () => el.close("cancel") }, "Cancel"),
          h("button", { class: danger ? "btn danger" : "btn primary", type: "button", onclick: () => el.close("ok") }, confirmLabel)));
      el.addEventListener("close", () => { resolve(el.returnValue === "ok"); el.remove(); });
      document.body.append(el);
      el.showModal();
    });
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

  function channelSelect(selected, { types = ["text", "news", "forum"], includeNone = false } = {}) {
    const select = h("select", {}, includeNone ? h("option", { value: "" }, "— none —") : null);
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

  function renderNav(current) {
    const nav = document.getElementById("nav");
    nav.replaceChildren(...pages.filter((p) => !p.hidden && can(p.perm)).map((p) =>
      h("a", { href: `#/${p.id}`, "aria-current": p.id === current ? "page" : null }, p.title)));
  }

  async function route() {
    const [id, ...args] = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
    const visible = pages.filter((p) => can(p.perm));
    const target = visible.find((p) => p.id === id) || visible[0];
    renderNav(target && target.id);
    const view = document.getElementById("view");
    view.replaceChildren();
    if (!target) { view.append(h("div", { class: "empty" }, "No pages available for your tier.")); return; }
    document.title = `${target.title} · BulmaAI Panel`;
    try {
      await target.render(view, args.filter(Boolean));
    } catch (error) {
      view.append(h("p", { class: "error" }, error.message));
    }
    view.focus({ preventScroll: true });
  }

  async function boot() {
    try {
      me = await api("/api/me");
    } catch {
      showLogin();
      return;
    }
    document.getElementById("login").hidden = true;
    document.getElementById("app").hidden = false;
    document.getElementById("guild-name").textContent = me.guild.name;
    if (me.guild.icon) document.getElementById("guild-icon").src = me.guild.icon;
    document.getElementById("me-avatar").src = me.user.avatar;
    document.getElementById("me-name").textContent = me.user.display_name;
    document.getElementById("me-tier").textContent = me.tier;
    document.getElementById("logout").addEventListener("click", async () => {
      await api("/auth/logout", { method: "POST" }).catch(() => {});
      location.reload();
    });
    window.addEventListener("hashchange", route);
    route();
  }

  document.addEventListener("DOMContentLoaded", boot);

  return {
    h, api, toast, run, can, time, user, badge, table, field, dialog, guild,
    channelName, roleName, channelSelect, page, go, me: () => me, refresh: route,
  };
})();
