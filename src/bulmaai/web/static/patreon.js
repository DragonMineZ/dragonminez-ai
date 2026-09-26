"use strict";

(() => {
  const { h, api, run, can, time, user, badge, table, field, dialog, toast } = Panel;

  // Search box + "active only" + paged table; fetchRows(q, active, page) -> {rows, has_more, page}.
  function searchableList(card, { title, placeholder, fetchRows, columns, empty, actions }) {
    const search = h("input", { type: "search", placeholder, "aria-label": `Search ${title.toLowerCase()}`, size: "36" });
    const active = h("input", { type: "checkbox" });
    const results = h("div");
    let page = 1;

    const load = async () => {
      const data = await fetchRows(encodeURIComponent(search.value.trim()), active.checked ? "1" : "", page);
      const prev = h("button", { class: "btn small ghost", type: "button", disabled: page <= 1 }, "Previous");
      const next = h("button", { class: "btn small ghost", type: "button", disabled: !data.has_more }, "Next");
      prev.addEventListener("click", () => { page -= 1; run(prev, load); });
      next.addEventListener("click", () => { page += 1; run(next, load); });
      results.replaceChildren(
        table(columns(load), data.rows, { empty }),
        h("div", { class: "row end" }, h("span", { class: "muted small" }, `Page ${data.page}`), prev, next));
    };

    const submit = h("button", { class: "btn small primary", type: "submit" }, "Search");
    const form = h("form", { class: "row" }, search, h("label", { class: "row" }, active, "Active only"), submit);
    form.addEventListener("submit", (e) => { e.preventDefault(); page = 1; run(submit, load); });
    active.addEventListener("change", () => { page = 1; run(null, load); });
    card.append(h("div", { class: "row spread" }, h("h2", {}, title), actions ? actions(load) : null), form, results);
    return load;
  }

  function prLink(url) {
    return url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, "PR") : h("span", { class: "muted" }, "—");
  }

  function grantButton(reload) {
    const button = h("button", { class: "btn small primary", type: "button" }, "Grant beta access");
    button.addEventListener("click", async () => {
      const userId = h("input", { type: "text", inputmode: "numeric", placeholder: "Discord user ID", size: "24" });
      const nick = h("input", { type: "text", placeholder: "Minecraft username", maxlength: "16", size: "24" });
      const ok = await dialog("Grant beta access", [
        h("p", { class: "muted" }, "Adds the username to the beta whitelist through the same GitHub PR + auto-merge as /beta-access, and records a self grant. Patreon checks are skipped, so the Patreon webhook won't revoke it if the user has no linked pledge."),
        field("Discord user ID", userId),
        field("Minecraft username", nick),
      ], { confirmLabel: "Grant" });
      if (!ok) return;
      await run(button, async () => {
        const result = await api("/api/patreon/grant", {
          method: "POST",
          body: { user_id: userId.value.trim(), minecraft_username: nick.value.trim() },
        });
        if (result.merged) toast(`Granted ${nick.value.trim()}`);
        else toast(`PR created but GitHub wouldn't auto-merge it; review it: ${result.pr_url}`, true);
        await reload();
      });
    });
    return button;
  }

  function revokeButton(grant, reload) {
    const gift = grant.kind === "gift";
    const button = h("button", { class: "btn small danger", type: "button" }, gift ? "Revoke gift" : "Revoke");
    button.addEventListener("click", async () => {
      const text = gift
        ? `Remove ${grant.minecraft_username} (gift from ${grant.owner_id}) from the beta whitelist?`
        : `Remove every active grant owned by ${grant.owner_id} (their own username and any gifts they gave) from the beta whitelist? Same path as an expired pledge.`;
      const ok = await dialog("Revoke beta access", h("p", {}, text), { confirmLabel: "Revoke", danger: true });
      if (!ok) return;
      await run(button, async () => {
        const body = gift ? { owner_id: grant.owner_id, beneficiary_id: grant.beneficiary_id } : { owner_id: grant.owner_id };
        await api("/api/patreon/revoke", { method: "POST", body });
        await reload();
      }, "Beta access revoked");
    });
    return button;
  }

  function grantColumns(reload) {
    const columns = [
      { label: "Minecraft", render: (g) => h("span", { class: "mono" }, g.minecraft_username) },
      { label: "Holder", render: (g) => h("div", {}, user(g.beneficiary), h("div", { class: "muted small" }, g.beneficiary_username)) },
      { label: "Kind", render: (g) => (g.kind === "gift" ? h("div", {}, badge("gift", "accent"), h("div", { class: "muted small" }, "from ", user(g.owner))) : badge("self")) },
      { label: "Status", render: (g) => (g.active ? badge("active", "ok") : badge("inactive")) },
      { label: "Created", render: (g) => time(g.created_at) },
      { label: "Updated", render: (g) => time(g.updated_at) },
      { label: "Source", render: (g) => prLink(g.source_pr_url) },
    ];
    if (can("patreon.manage")) columns.push({ label: "", render: (g) => (g.active ? revokeButton(g, reload) : null) });
    return columns;
  }

  function linkColumns() {
    return [
      { label: "Discord", render: (l) => h("div", {}, user(l.user), h("div", { class: "muted small" }, l.discord_username)) },
      { label: "Patreon name", render: (l) => l.patreon_full_name || h("span", { class: "muted" }, "—") },
      { label: "Status", render: (l) => l.patron_status || h("span", { class: "muted" }, "—") },
      { label: "Entitled", render: (l) => (l.entitlement_active ? badge("yes", "ok") : badge("no", "warn")) },
      { label: "Tiers", render: (l) => h("div", { class: "row" }, l.tier_ids.map((t) => badge(t))) },
      { label: "Last charge", render: (l) => time(l.last_charge_date) },
      { label: "Linked", render: (l) => time(l.linked_at) },
      { label: "Updated", render: (l) => time(l.updated_at) },
    ];
  }

  Panel.page({
    id: "patreon",
    title: "Patreon",
    perm: "patreon.view",
    async render(view) {
      const grantsCard = h("div", { class: "card" });
      const linksCard = h("div", { class: "card" });
      view.append(h("h1", {}, "Patreon & beta whitelist"), grantsCard, linksCard);
      const loadGrants = searchableList(grantsCard, {
        title: "Whitelist grants",
        placeholder: "Minecraft name, Discord name or user ID…",
        empty: "No grants found.",
        columns: grantColumns,
        actions: can("patreon.manage") ? grantButton : null,
        fetchRows: async (q, active, page) => {
          const data = await api(`/api/patreon/grants?q=${q}&active=${active}&page=${page}`);
          return { rows: data.grants, has_more: data.has_more, page: data.page };
        },
      });
      const loadLinks = searchableList(linksCard, {
        title: "Patreon links",
        placeholder: "Discord name, Patreon name or user ID…",
        empty: "No linked Patreon accounts found.",
        columns: linkColumns,
        fetchRows: async (q, active, page) => {
          const data = await api(`/api/patreon/links?q=${q}&active=${active}&page=${page}`);
          return { rows: data.links, has_more: data.has_more, page: data.page };
        },
      });
      await Promise.all([loadGrants(), loadLinks()]);
    },
  });
})();
