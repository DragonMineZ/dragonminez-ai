"use strict";

(() => {
  const { h, api, run, can, time, user, badge, table, field, dialog, toast, t, i18n, locale, go } = Panel;

  // Search box + "active only" + paged table; fetchRows(q, active, page) -> {rows, has_more, page}.
  function searchableList(card, { title, placeholder, fetchRows, columns, empty, actions, onRowClick }) {
    const search = h("input", { type: "search", placeholder, "aria-label": t("Search {title}", { title: title.toLowerCase() }), size: "36" });
    const active = h("input", { type: "checkbox" });
    const results = h("div");
    let page = 1;

    const load = async () => {
      const data = await fetchRows(encodeURIComponent(search.value.trim()), active.checked ? "1" : "", page);
      const prev = h("button", { class: "btn small ghost", type: "button", disabled: page <= 1 }, t("Previous"));
      const next = h("button", { class: "btn small ghost", type: "button", disabled: !data.has_more }, t("Next"));
      prev.addEventListener("click", () => { page -= 1; run(prev, load); });
      next.addEventListener("click", () => { page += 1; run(next, load); });
      results.replaceChildren(
        table(columns(load), data.rows, { empty, onRowClick }),
        h("div", { class: "row end" }, h("span", { class: "muted small" }, `${t("Page")} ${data.page}`), prev, next));
    };

    const submit = h("button", { class: "btn small primary", type: "submit" }, t("Search"));
    const form = h("form", { class: "row" }, search, h("label", { class: "row" }, active, t("Active only")), submit);
    form.addEventListener("submit", (e) => { e.preventDefault(); page = 1; run(submit, load); });
    active.addEventListener("change", () => { page = 1; run(null, load); });
    card.append(h("div", { class: "row spread" }, h("h2", {}, title), actions ? actions(load) : null), form, results);
    return load;
  }

  function stat(label, value) {
    return h("div", { class: "stat" }, h("div", { class: "value" }, value), h("div", { class: "label" }, label));
  }

  function prLink(url) {
    return url
      ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", onclick: (e) => e.stopPropagation() }, "GitHub")
      : h("span", { class: "muted" }, "—");
  }

  // prefillUserId: opened from a person's own detail view, so the ID field is already filled in.
  function grantButton(reload, prefillUserId) {
    const button = h("button", { class: "btn small primary", type: "button" }, t("Grant beta access"));
    button.addEventListener("click", async () => {
      const userId = h("input", { type: "text", inputmode: "numeric", placeholder: t("Discord user ID"), size: "24", value: prefillUserId || "" });
      const nick = h("input", { type: "text", placeholder: t("Minecraft username"), maxlength: "16", size: "24" });
      const ok = await dialog(t("Grant beta access"), [
        h("p", { class: "muted" }, t("Adds the username to the beta whitelist with the same GitHub commit as /patreon beta-access, and records a self grant. Patreon checks are skipped, so the Patreon webhook won't revoke it if the user has no linked pledge.")),
        field(t("Discord user ID"), userId),
        field(t("Minecraft username"), nick),
      ], { confirmLabel: t("Grant") });
      if (!ok) return;
      await run(button, async () => {
        const result = await api("/api/patreon/grant", {
          method: "POST",
          body: { user_id: userId.value.trim(), minecraft_username: nick.value.trim() },
        });
        toast(t("Granted {name}", { name: nick.value.trim() }));
        await reload();
      });
    });
    return button;
  }

  function revokeButton(grant, reload) {
    const gift = grant.kind === "gift";
    const button = h("button", { class: "btn small danger", type: "button" }, gift ? t("Revoke gift") : t("Revoke"));
    button.addEventListener("click", async (e) => {
      e.stopPropagation();
      const text = gift
        ? t("Remove {name} (gift from {owner}) from the beta whitelist?", { name: grant.minecraft_username, owner: grant.owner_id })
        : t("Remove every active grant owned by {owner} (their own username and any gifts they gave) from the beta whitelist? Same path as an expired pledge.", { owner: grant.owner_id });
      const ok = await dialog(t("Revoke beta access"), h("p", {}, text), { confirmLabel: t("Revoke"), danger: true });
      if (!ok) return;
      await run(button, async () => {
        const body = gift ? { owner_id: grant.owner_id, beneficiary_id: grant.beneficiary_id } : { owner_id: grant.owner_id };
        await api("/api/patreon/revoke", { method: "POST", body });
        await reload();
      }, t("Beta access revoked"));
    });
    return button;
  }

  function grantColumns(reload) {
    const columns = [
      { label: t("Minecraft"), render: (g) => h("span", { class: "mono" }, g.minecraft_username) },
      { label: t("Holder"), render: (g) => h("div", {}, user(g.beneficiary), h("div", { class: "muted small" }, g.beneficiary_username)) },
      { label: t("Kind"), render: (g) => (g.kind === "gift" ? h("div", {}, badge(t("gift"), "accent"), h("div", { class: "muted small" }, `${t("from")} `, user(g.owner))) : badge(t("self"))) },
      { label: t("Status"), render: (g) => (g.active ? badge(t("active"), "ok") : badge(t("inactive"))) },
      { label: t("Created"), render: (g) => time(g.created_at) },
      { label: t("Updated"), render: (g) => time(g.updated_at) },
      { label: t("Source"), render: (g) => prLink(g.source_pr_url) },
    ];
    if (can("patreon.manage")) columns.push({ label: "", render: (g) => (g.active ? revokeButton(g, reload) : null) });
    return columns;
  }

  function tierBadges(tierIds, tierNames) {
    if (!tierIds || !tierIds.length) return null;
    return h("div", { class: "row" }, tierIds.map((id) => badge((tierNames && tierNames[id]) || id)));
  }

  function linkColumns() {
    return [
      { label: t("Discord"), render: (l) => h("div", {}, user(l.user), h("div", { class: "muted small" }, l.discord_username)) },
      { label: t("Patreon name"), render: (l) => l.patreon_full_name || h("span", { class: "muted" }, "—") },
      { label: t("Status"), render: (l) => l.patron_status || h("span", { class: "muted" }, "—") },
      { label: t("Entitled"), render: (l) => (l.entitlement_active ? badge(t("yes"), "ok") : badge(t("no"), "warn")) },
      { label: t("Tiers"), render: (l) => tierBadges(l.tier_ids, null) || h("span", { class: "muted" }, "—") },
      { label: t("Last charge"), render: (l) => time(l.last_charge_date) },
      { label: t("Linked"), render: (l) => time(l.linked_at) },
      { label: t("Updated"), render: (l) => time(l.updated_at) },
    ];
  }

  // ---- Person detail view ---------------------------------------------------------------------

  function formatCents(cents) {
    if (cents === null || cents === undefined) return h("span", { class: "muted" }, "—");
    // Patreon's v2 API doesn't expose a currency code alongside these fields; the campaign is USD.
    return new Intl.NumberFormat(locale(), { style: "currency", currency: "USD" }).format(cents / 100);
  }

  function cadenceLabel(cadence) {
    if (!cadence) return h("span", { class: "muted" }, "—");
    if (cadence === "month") return t("Monthly");
    if (cadence === "per_post") return t("Per post");
    return cadence;
  }

  function linkCard(link, live) {
    if (!link) return h("div", { class: "card" }, h("h2", {}, t("Patreon link")), h("p", { class: "muted" }, t("No linked Patreon account for this user.")));
    return h("div", { class: "card stack" },
      h("h2", {}, t("Patreon link")),
      h("div", { class: "grid" },
        stat(t("Patreon name"), link.patreon_full_name || h("span", { class: "muted" }, "—")),
        stat(t("Patron status"), link.patron_status || h("span", { class: "muted" }, "—")),
        stat(t("Entitled"), link.entitlement_active ? badge(t("yes"), "ok") : badge(t("no"), "warn")),
        stat(t("Last charge"), time(link.last_charge_date)),
        stat(t("Linked"), time(link.linked_at)),
        stat(t("Updated"), time(link.updated_at))),
      tierBadges(link.tier_ids, live && live.tier_names) || h("p", { class: "muted" }, t("No tiers.")));
  }

  function liveCard(live, liveError) {
    if (liveError) return h("div", { class: "card" }, h("h2", {}, t("Live Patreon data")), h("p", { class: "error" }, t("Live Patreon data unavailable: {error}", { error: liveError })));
    if (!live) return null;
    return h("div", { class: "card" }, h("h2", {}, t("Live Patreon data")), h("div", { class: "grid" },
      stat(t("Pledge amount"), formatCents(live.currently_entitled_amount_cents)),
      stat(t("Lifetime support"), formatCents(live.lifetime_support_cents)),
      stat(t("Pledge started"), time(live.pledge_relationship_start)),
      stat(t("Last charge status"), live.last_charge_status || h("span", { class: "muted" }, "—")),
      stat(t("Next charge"), time(live.next_charge_date)),
      stat(t("Pledge cadence"), cadenceLabel(live.pledge_cadence))));
  }

  async function renderPerson(view, id) {
    const data = await api(`/api/patreon/people/${encodeURIComponent(id)}`);
    const reload = () => Panel.refresh();

    const grantsCard = h("div", { class: "card" },
      h("div", { class: "row spread" }, h("h2", {}, t("Whitelist grants")), can("patreon.manage") ? grantButton(reload, id) : null),
      table(grantColumns(reload), data.grants, { empty: t("No whitelist grants for this user.") }));

    view.append(
      h("p", {}, h("a", { href: "#/patreon" }, `← ${t("Patreon")}`)),
      h("div", { class: "card stack" },
        h("div", { class: "row spread" }, h("h1", {}, user(data.user)), h("span", { class: "mono muted" }, id)),
        h("p", {}, h("a", { href: `#/users/${encodeURIComponent(id)}` }, t("Open user profile ›")))),
      linkCard(data.link, data.live),
      data.link ? liveCard(data.live, data.live_error) : null,
      grantsCard);
  }

  // ---- List view -------------------------------------------------------------------------------

  async function renderList(view) {
    const grantsCard = h("div", { class: "card" });
    const linksCard = h("div", { class: "card" });
    view.append(h("h1", {}, t("Patreon & beta whitelist")), grantsCard, linksCard);
    const loadGrants = searchableList(grantsCard, {
      title: t("Whitelist grants"),
      placeholder: t("Minecraft name, Discord name or user ID…"),
      empty: t("No grants found."),
      columns: grantColumns,
      actions: can("patreon.manage") ? grantButton : null,
      onRowClick: (g) => go(`#/patreon/${encodeURIComponent(g.beneficiary_id)}`),
      fetchRows: async (q, active, page) => {
        const data = await api(`/api/patreon/grants?q=${q}&active=${active}&page=${page}`);
        return { rows: data.grants, has_more: data.has_more, page: data.page };
      },
    });
    const loadLinks = searchableList(linksCard, {
      title: t("Patreon links"),
      placeholder: t("Discord name, Patreon name or user ID…"),
      empty: t("No linked Patreon accounts found."),
      columns: linkColumns,
      onRowClick: (l) => go(`#/patreon/${encodeURIComponent(l.discord_user_id)}`),
      fetchRows: async (q, active, page) => {
        const data = await api(`/api/patreon/links?q=${q}&active=${active}&page=${page}`);
        return { rows: data.links, has_more: data.has_more, page: data.page };
      },
    });
    await Promise.all([loadGrants(), loadLinks()]);
  }

  i18n({
    "Search {title}": "Buscar {title}",
    "Previous": "Anterior",
    "Next": "Siguiente",
    "Page": "Página",
    "Active only": "Solo activos",
    "Search": "Buscar",
    "Grant beta access": "Dar acceso beta",
    "Adds the username to the beta whitelist with the same GitHub commit as /patreon beta-access, and records a self grant. Patreon checks are skipped, so the Patreon webhook won't revoke it if the user has no linked pledge.":
      "Agrega el nombre de usuario a la lista blanca beta con el mismo commit de GitHub que /patreon beta-access, y registra un grant propio. Se omiten las verificaciones de Patreon, así que el webhook de Patreon no lo revocará si el usuario no tiene un pledge vinculado.",
    "Discord user ID": "ID de usuario de Discord",
    "Minecraft username": "Usuario de Minecraft",
    "Grant": "Otorgar",
    "Granted {name}": "Se otorgó a {name}",
    "Revoke gift": "Revocar regalo",
    "Revoke": "Revocar",
    "Revoke beta access": "Revocar acceso beta",
    "Remove {name} (gift from {owner}) from the beta whitelist?": "¿Quitar a {name} (regalo de {owner}) de la lista blanca beta?",
    "Remove every active grant owned by {owner} (their own username and any gifts they gave) from the beta whitelist? Same path as an expired pledge.":
      "¿Quitar todos los grants activos de {owner} (su propio usuario y cualquier regalo que haya dado) de la lista blanca beta? Es el mismo proceso que un pledge vencido.",
    "Beta access revoked": "Acceso beta revocado",
    "Minecraft": "Minecraft",
    "Holder": "Titular",
    "Kind": "Tipo",
    "Status": "Estado",
    "Created": "Creado",
    "Updated": "Actualizado",
    "Source": "Origen",
    "Discord": "Discord",
    "Patreon name": "Nombre en Patreon",
    "Entitled": "Con derecho",
    "Tiers": "Niveles",
    "Last charge": "Último cobro",
    "Linked": "Vinculado",
    "gift": "regalo",
    "self": "propio",
    "active": "activo",
    "inactive": "inactivo",
    "yes": "sí",
    "no": "no",
    "from": "de",
    "Patreon & beta whitelist": "Patreon y lista blanca beta",
    "Whitelist grants": "Grants de la lista blanca",
    "Minecraft name, Discord name or user ID…": "Nombre de Minecraft, nombre de Discord o ID de usuario…",
    "No grants found.": "No se encontraron grants.",
    "Patreon links": "Vínculos de Patreon",
    "Discord name, Patreon name or user ID…": "Nombre de Discord, nombre de Patreon o ID de usuario…",
    "No linked Patreon accounts found.": "No se encontraron cuentas de Patreon vinculadas.",
    "Patreon": "Patreon",
    "Open user profile ›": "Abrir perfil de usuario ›",
    "Patreon link": "Vínculo de Patreon",
    "No linked Patreon account for this user.": "Esta persona no tiene una cuenta de Patreon vinculada.",
    "Patron status": "Estado de patrocinador",
    "No tiers.": "Sin niveles.",
    "Live Patreon data": "Datos en vivo de Patreon",
    "Live Patreon data unavailable: {error}": "Datos en vivo de Patreon no disponibles: {error}",
    "Pledge amount": "Monto del pledge",
    "Lifetime support": "Apoyo total histórico",
    "Pledge started": "Inicio del pledge",
    "Last charge status": "Estado del último cobro",
    "Next charge": "Próximo cobro",
    "Pledge cadence": "Frecuencia del pledge",
    "Monthly": "Mensual",
    "Per post": "Por publicación",
    "No whitelist grants for this user.": "Esta persona no tiene grants en la lista blanca.",
  });

  Panel.page({
    id: "patreon",
    title: "Patreon",
    perm: "patreon.view",
    group: "Community",
    async render(view, args) {
      if (args[0]) await renderPerson(view, args[0]);
      else await renderList(view);
    },
  });
})();
