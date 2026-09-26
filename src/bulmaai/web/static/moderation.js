"use strict";

(() => {
  const { h, api, run, can, time, user, badge, table, field, dialog } = Panel;

  const ACTION_KIND = { warn: "warn", timeout: "warn", kick: "danger", ban: "danger", delete: "danger", alert: "warn", unban: "ok", untimeout: "ok" };

  function duration(seconds) {
    if (!seconds) return "";
    for (const [unit, size] of [["d", 86400], ["h", 3600], ["m", 60]]) {
      if (seconds >= size && seconds % size === 0) return `${seconds / size}${unit}`;
    }
    return `${Math.round(seconds / 60)}m`;
  }

  function actionBadge(action) {
    return badge(action, ACTION_KIND[action] || "");
  }

  function openProfile(id) {
    Panel.go(`#/users/${encodeURIComponent(id)}`);
  }

  function caseColumns({ withUser }) {
    return [
      { label: "#", render: (c) => h("span", { class: "mono" }, String(c.id)) },
      { label: "When", render: (c) => time(c.created_at) },
      withUser ? { label: "User", render: (c) => user(c.user || c.user_id) } : null,
      { label: "Action", render: (c) => h("div", { class: "row" }, actionBadge(c.action), c.duration_seconds ? badge(duration(c.duration_seconds)) : null) },
      { label: "Reason", render: (c) => c.reason || h("span", { class: "muted" }, "—") },
      { label: "By", render: (c) => (c.source === "automod" ? badge("automod", "accent") : user(c.moderator || c.moderator_id)) },
    ].filter(Boolean);
  }

  // ---- Users: search -------------------------------------------------------------------------

  async function renderSearch(view) {
    const input = h("input", { type: "search", placeholder: "Name, display name or user ID…", "aria-label": "Search users", size: "40" });
    const results = h("div");
    const submit = h("button", { class: "btn primary", type: "submit" }, "Search");

    const search = async () => {
      const q = input.value.trim();
      if (!q) { results.replaceChildren(); return; }
      const data = await api(`/api/users/search?q=${encodeURIComponent(q)}`);
      const byId = /^\d{15,20}$/.test(q) && !data.results.some((r) => r.id === q)
        ? h("p", {}, h("a", { href: `#/users/${q}` }, `Open profile for ID ${q}`), h("span", { class: "muted" }, " (not in the member cache, e.g. banned or left)"))
        : null;
      results.replaceChildren(byId || "", table([
        { label: "User", render: (r) => user(r) },
        { label: "ID", render: (r) => h("span", { class: "mono" }, r.id) },
        { label: "Joined", render: (r) => time(r.joined_at) },
      ], data.results, { onRowClick: (r) => openProfile(r.id), empty: "No members match." }));
    };

    const form = h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); run(submit, search); } }, input, submit);
    view.append(h("h1", {}, "Users"), h("div", { class: "card stack" }, form, results));
    input.focus();
  }

  // ---- Users: profile ------------------------------------------------------------------------

  function section(title, result, render) {
    if (!result) return null; // section not visible to this tier
    return h("div", { class: "card" }, h("h2", {}, title),
      result.error ? h("p", { class: "error" }, result.error) : render(result.data));
  }

  function stat(label, value) {
    return h("div", { class: "stat" }, h("div", { class: "value" }, value), h("div", { class: "label" }, label));
  }

  const ACTIONS = [
    { id: "warn", label: "Warn", perm: "mod.warn", member: true, help: "The user gets a DM with the reason." },
    { id: "note", label: "Add note", perm: "mod.warn", help: "Internal only; the user isn't notified." },
    { id: "timeout", label: "Timeout", perm: "mod.timeout", member: true, show: (p) => !p.timed_out_until, minutes: true },
    { id: "untimeout", label: "Remove timeout", perm: "mod.timeout", member: true, show: (p) => Boolean(p.timed_out_until), optionalReason: true },
    { id: "kick", label: "Kick", perm: "mod.kick", member: true, danger: true },
    { id: "ban", label: "Ban", perm: "mod.ban", danger: true, show: (p) => p.banned !== true, hours: true },
    { id: "unban", label: "Unban", perm: "mod.ban", show: (p) => p.banned !== false && !p.member },
  ];

  async function runAction(button, profile, action) {
    const name = profile.user.display_name || profile.user.name;
    const reason = h("textarea", { rows: "3", maxlength: "400", placeholder: action.optionalReason ? "Optional" : "Required" });
    const minutes = action.minutes ? h("input", { type: "number", min: "1", max: "40320", value: "60" }) : null;
    const hours = action.hours ? h("input", { type: "number", min: "0", max: "168", value: "0" }) : null;
    const ok = await dialog(`${action.label}: ${name}`, [
      action.help ? h("p", { class: "muted" }, action.help) : null,
      minutes ? field("Duration in minutes (max 40320 = 28 days)", minutes) : null,
      hours ? field("Delete their messages from the last N hours (0–168)", hours) : null,
      field("Reason", reason),
    ], { confirmLabel: action.label, danger: Boolean(action.danger) });
    if (!ok) return;
    const body = { reason: reason.value.trim() };
    if (minutes) body.minutes = parseInt(minutes.value, 10);
    if (hours) body.delete_message_hours = parseInt(hours.value, 10) || 0;
    if (!body.reason && !action.optionalReason) { Panel.toast("A reason is required.", true); return; }
    const result = await run(button, () => api(`/api/users/${profile.user.id}/${action.id}`, { method: "POST", body }), `${action.label}: done`);
    if (!result) return;
    if (result.dm_sent === false) Panel.toast("Couldn't DM the user (DMs closed); the warning is still recorded.", true);
    Panel.refresh();
  }

  function actionBar(profile) {
    const buttons = ACTIONS
      .filter((a) => can(a.perm) && (!a.member || profile.member) && (!a.show || a.show(profile)))
      .map((a) => {
        const button = h("button", { class: a.danger ? "btn danger" : "btn", type: "button" }, a.label);
        button.addEventListener("click", () => runAction(button, profile, a));
        return button;
      });
    return buttons.length ? h("div", { class: "row" }, buttons) : null;
  }

  async function renderProfile(view, id) {
    const p = await api(`/api/users/${encodeURIComponent(id)}`);
    const s = p.sections;
    const flags = [
      p.user.bot ? badge("bot") : null,
      p.tier !== "none" ? badge(`staff: ${p.tier}`, "accent") : null,
      p.member ? null : badge("not a member", "warn"),
      p.banned === true ? badge("banned", "danger") : null,
      p.banned === null ? badge("ban status unknown", "warn") : null,
      p.timed_out_until ? h("span", { class: "badge warn" }, "timed out until ", time(p.timed_out_until)) : null,
    ];

    view.append(
      h("p", {}, h("a", { href: "#/users" }, "← Users")),
      h("div", { class: "card stack" },
        h("div", { class: "row spread" }, h("h1", {}, user(p.user)), h("span", { class: "mono muted" }, p.user.id)),
        h("div", { class: "row" }, flags),
        h("div", { class: "grid" },
          stat("Account created", time(p.created_at)),
          stat("Joined server", time(p.joined_at))),
        p.ban_reason ? h("p", {}, h("span", { class: "muted" }, "Ban reason: "), p.ban_reason) : null,
        p.roles.length ? h("div", { class: "row" }, p.roles.map((r) => badge(r.name))) : null,
        actionBar(p)),
      section("Activity", s.activity, (a) => h("div", { class: "grid" },
        stat("Level", String(a.level)),
        stat("XP", `${a.xp} / ${a.next_level_xp}`),
        stat("Last XP award", time(a.last_award_at)))),
      section("Dev jar", s.dev_jar, (d) => h("div", { class: "grid" },
        stat("Downloads", String(d.downloads)),
        stat("Last download", time(d.last_download_at)))),
      section("Mod cases", s.cases, (cases) => table(caseColumns({ withUser: false }), cases, { empty: "No cases." })),
      section("Tickets", s.tickets, (tickets) => table([
        { label: "Closed", render: (t) => time(t.closed_at) },
        { label: "Title", render: (t) => t.title || t.channel_name || "—" },
        { label: "Messages", render: (t) => String(t.message_count) },
        { label: "Status", render: (t) => (t.resolved ? badge("resolved", "ok") : badge("unresolved")) },
      ], tickets, {
        empty: "No ticket transcripts.",
        onRowClick: Panel.can("tickets.view") ? (t) => Panel.go(`#/tickets/transcript/${t.id}`) : undefined,
      })),
      section("Bug reports", s.bug_reports, (bugs) => table([
        { label: "Created", render: (b) => time(b.created_at) },
        { label: "Title", render: (b) => b.title || h("span", { class: "mono" }, b.thread_id) },
        { label: "Status", render: (b) => badge(b.status) },
        { label: "Issue", render: (b) => (b.issue_number ? `${b.repo}#${b.issue_number}` : "—") },
      ], bugs, { empty: "No bug reports." })),
      section("Patreon", s.patreon, (link) => (link
        ? h("div", { class: "row" },
          link.name || "",
          link.entitled ? badge("entitled", "ok") : badge("not entitled", "warn"),
          link.status ? badge(link.status) : null,
          h("span", { class: "muted small" }, "Last charge: "), time(link.last_charge_date))
        : h("p", { class: "muted" }, "No Patreon account linked."))));
  }

  Panel.page({
    id: "users",
    title: "Users",
    perm: "users.view",
    async render(view, args) {
      if (args[0]) await renderProfile(view, args[0]);
      else await renderSearch(view);
    },
  });

  // ---- Cases ---------------------------------------------------------------------------------

  Panel.page({
    id: "cases",
    title: "Mod cases",
    perm: "mod.cases.view",
    async render(view) {
      const userInput = h("input", { type: "text", inputmode: "numeric", placeholder: "User ID", size: "22" });
      const actionSelect = h("select", {}, h("option", { value: "" }, "All actions"),
        ["warn", "note", "timeout", "untimeout", "kick", "ban", "unban", "alert", "delete"].map((a) => h("option", { value: a }, a)));
      const sourceSelect = h("select", {}, h("option", { value: "" }, "All sources"),
        h("option", { value: "panel" }, "panel"), h("option", { value: "automod" }, "automod"));
      const apply = h("button", { class: "btn primary", type: "submit" }, "Apply");
      const more = h("button", { class: "btn", type: "button", hidden: true }, "Load more");
      const results = h("div");
      let cases = [];
      let nextBefore = null;

      const load = async (append) => {
        const params = new URLSearchParams();
        if (userInput.value.trim()) params.set("user_id", userInput.value.trim());
        if (actionSelect.value) params.set("action", actionSelect.value);
        if (sourceSelect.value) params.set("source", sourceSelect.value);
        if (append && nextBefore) params.set("before_id", String(nextBefore));
        const data = await api(`/api/cases?${params}`);
        cases = append ? cases.concat(data.cases) : data.cases;
        nextBefore = data.next_before_id;
        more.hidden = !nextBefore;
        results.replaceChildren(table(caseColumns({ withUser: true }), cases, { onRowClick: (c) => openProfile(c.user_id), empty: "No cases match." }));
      };

      more.addEventListener("click", () => run(more, () => load(true)));
      const form = h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); run(apply, () => load(false)); } },
        userInput, actionSelect, sourceSelect, apply);
      view.append(
        h("h1", {}, "Mod cases"),
        h("div", { class: "card stack" }, form, results, h("div", { class: "row" }, more)));
      await load(false);
    },
  });
})();
