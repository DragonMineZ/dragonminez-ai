"use strict";

(() => {
  const { h, api, run, can, time, user, badge, table, field, roleName, t } = Panel;

  Panel.i18n({
    " · plus anyone with Discord Administrator": " · además de cualquiera con Administrador en Discord",
    "Overview": "Resumen",
    "Staff": "Staff",
    "Bot": "Bot",
    "not connected": "no conectado",
    "Websocket latency": "Latencia de Websocket",
    "Uptime": "Tiempo de actividad",
    "Guild members": "Miembros del servidor",
    "Database": "Base de datos",
    "ok": "ok",
    "AI budget": "Presupuesto de IA",
    "AI paused: small pool spent": "IA pausada: pool pequeño agotado",
    "Pools reset ": "Los pools se reinician ",
    "since ": "desde ",
    "guild not available": "servidor no disponible",
    "AI tokens today · {pool}": "Tokens de IA hoy · {pool}",
    "no limit": "sin límite",
    "disabled": "deshabilitado",
    "Refresh": "Actualizar",
    "Extensions ({count})": "Extensiones ({count})",
    "Extension": "Extensión",
    "Reload": "Recargar",
    "hosts the panel": "aloja el panel",
    "Reloaded {name}": "Se recargó {name}",
    "No extensions loaded.": "Sin extensiones cargadas.",
    "Guild owner and Bruno.": "Propietario del servidor y Bruno.",
    "Discord Administrator permission (no roles configured).": "Permiso de Administrador de Discord (sin roles configurados).",
    "No roles configured.": "Sin roles configurados.",
    "{tier} ({count})": "{tier} ({count})",
    "Nobody.": "Nadie.",
    "Permission": "Permiso",
    "yes": "sí",
    "Permission matrix": "Matriz de permisos",
    "Permissions for staff in this panel website.": "Permisos para el staff en este sitio del panel.",
    "Each permission is shared with the discord Bot in the app.": "Cada permiso se comparte con el bot de Discord en la app.",
  });

  const TIER_ORDER = ["owner", "admin", "moderator", "helper"];

  function duration(seconds) {
    const d = Math.floor(seconds / 86400);
    const hrs = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return d ? `${d}d ${hrs}h ${m}m` : hrs ? `${hrs}h ${m}m` : `${m}m`;
  }

  function num(value) {
    return Number(value).toLocaleString();
  }

  function stat(label, value, extra) {
    return h("div", { class: "card stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), extra || null);
  }

  function budgetCard(pool) {
    const label = t("AI tokens today · {pool}", { pool: pool.pool });
    if (pool.limit === null || pool.limit === undefined) return stat(label, num(pool.used), h("div", { class: "muted small" }, t("no limit")));
    if (pool.limit === 0) return stat(label, num(pool.used), badge(t("disabled"), "warn"));
    const pct = Math.round((pool.used / pool.limit) * 100);
    return stat(label, `${pct}%`,
      h("meter", { min: "0", max: String(pool.limit), value: String(Math.min(pool.used, pool.limit)), low: String(pool.limit * 0.7), high: String(pool.limit * 0.9), optimum: "0", "aria-label": label }),
      h("div", { class: "muted small" }, `${num(pool.used)} / ${num(pool.limit)}`));
  }

  function extensionsCard(data, reload) {
    const canReload = can("bot.reload");
    const rows = data.extensions.map((name) => ({ name }));
    return h("div", { class: "card" },
      h("h2", {}, t("Extensions ({count})", { count: data.extensions.length })),
      table([
        { label: t("Extension"), render: (r) => h("span", { class: "mono" }, r.name) },
        {
          label: "",
          render: (r) => {
            if (!canReload) return "";
            if (r.name === data.panel_extension) return h("span", { class: "muted small" }, t("hosts the panel"));
            const button = h("button", { class: "btn small", type: "button" }, t("Reload"));
            button.addEventListener("click", () => run(button, async () => {
              await api(`/api/status/extensions/${encodeURIComponent(r.name)}/reload`, { method: "POST" });
              await reload();
            }, t("Reloaded {name}", { name: r.name })));
            return button;
          },
        },
      ], rows, { empty: t("No extensions loaded.") }));
  }

  Panel.page({
    id: "overview",
    title: t("Overview"),
    perm: "status.view",
    group: "Dashboard",
    async render(view) {
      const body = h("div");
      const reload = async () => {
        const data = await api("/api/status");
        const b = data.ai_budget;
        body.replaceChildren(
          h("div", { class: "grid" },
            stat(t("Bot"), data.bot ? data.bot.display_name : t("not connected"), data.bot ? h("div", { class: "muted small mono" }, data.bot.id) : null),
            stat(t("Websocket latency"), data.latency_ms === null ? "—" : `${data.latency_ms} ms`),
            stat(t("Uptime"), duration(data.uptime_seconds), h("div", { class: "muted small" }, t("since "), time(data.started_at))),
            stat(t("Guild members"), data.guild && data.guild.member_count !== null ? num(data.guild.member_count) : "—",
              h("div", { class: "muted small" }, data.guild ? data.guild.name : t("guild not available"))),
            stat(t("Database"), data.db.ok ? badge(t("ok"), "ok") : badge(t("error"), "danger"),
              data.db.error ? h("div", { class: "error small" }, data.db.error) : null)),
          h("h2", {}, t("AI budget")),
          h("p", { class: "muted small" },
            b.paused ? badge(t("AI paused: small pool spent"), "danger") : null, " ", t("Pools reset "), time(b.resets_at), t(" (00:00 UTC).")),
          h("div", { class: "grid" }, b.pools.map(budgetCard)),
          extensionsCard(data, reload));
      };
      const refresh = h("button", { class: "btn small ghost", type: "button" }, t("Refresh"));
      refresh.addEventListener("click", () => run(refresh, reload));
      view.append(h("div", { class: "row spread" }, h("h1", {}, t("Overview")), refresh), body);
      await reload();
    },
  });

  Panel.page({
    id: "staff",
    title: t("Staff"),
    perm: "audit.view",
    group: "Dashboard",
    async render(view) {
      const data = await api("/api/staff");
      const tiers = TIER_ORDER.filter((t) => data.tiers.includes(t));

      const rolesLine = (tier) => {
        const ids = data.role_ids[tier] || [];
        const span = h("div", { class: "muted small" });
        if (tier === "owner") span.textContent = t("Guild owner and Bruno.");
        else if (!ids.length) span.textContent = tier === "admin" ? t("Discord Administrator permission (no roles configured).") : t("No roles configured.");
        else Promise.all(ids.map(roleName)).then((names) => {
          span.textContent = t("Roles: {roles}", { roles: names.join(", ") + (tier === "admin" ? t(" · plus anyone with Discord Administrator") : "") });
        });
        return span;
      };

      const groups = tiers.map((tier) => {
        const members = data.members.filter((m) => m.tier === tier);
        return h("div", { class: "card" },
          h("h2", {}, t("{tier} ({count})", { tier: tier[0].toUpperCase() + tier.slice(1), count: members.length })),
          rolesLine(tier),
          members.length
            ? h("div", { class: "row" }, members.map((m) => user(m.user)))
            : h("div", { class: "muted small" }, t("Nobody.")));
      });

      const levels = { helper: 1, moderator: 2, admin: 3, owner: 4 };
      const columns = [{ label: t("Permission"), render: (p) => h("span", { class: "mono" }, p.name) }].concat(
        tiers.slice().reverse().map((tier) => ({
          label: tier,
          render: (p) => (levels[tier] >= p.tier_level ? badge(t("yes"), p.tier === tier ? "accent" : "ok") : h("span", { class: "muted" }, "—")),
        })));

      view.append(
        h("h1", {}, t("Staff")),
        h("p", { class: "muted" }, t("Permissions for staff in this panel website.")),
        ...groups,
        h("div", { class: "card" },
          h("h2", {}, t("Permission matrix")),
          h("p", { class: "muted small" }, t("Each permission is shared with the discord Bot in the app.")),
          table(columns, data.permissions)));
    },
  });
})();
