"use strict";

(() => {
  const { h, api, run, can, time, user, badge, table, field, dialog, t } = Panel;

  Panel.i18n({
    "Dev jar": "Jar de desarrollo",
    "Name, display name or user ID…": "Nombre, apodo o ID de usuario…",
    "Search members": "Buscar miembros",
    "Search": "Buscar",
    "Role": "Rol",
    "Sort": "Ordenar",
    "All roles": "Todos los roles",
    "Newest joined": "Más recientes",
    "Oldest joined": "Más antiguos",
    "Name": "Nombre",
    "Load more": "Cargar más",
    "{n} members": "{n} miembros",
    "No members match your search.": "Ningún miembro coincide con tu búsqueda.",
    "Open profile for ID {id}": "Abrir perfil del ID {id}",
    "(not in the member cache, e.g. banned or left)": "(no está en la caché de miembros, p. ej. baneado o se fue)",
    "View profile ›": "Ver perfil ›",
    "Bot": "Bot",
    "Timed out": "Silenciado",
    "Joined": "Se unió",
    "Users": "Usuarios",
    "← Users": "← Usuarios",
    "Activity": "Actividad",
    "Mod cases": "Casos de moderación",
    "When": "Cuándo",
    "User": "Usuario",
    "Action": "Acción",
    "By": "Por",
    "Dyno (deprecated)": "Dyno (obsoleto)",
    "via Discord": "vía Discord",
    "View in Audit log ›": "Ver en el registro de auditoría ›",
    "Tickets": "Tickets",
    "Bug reports": "Reportes de errores",
    "Patreon": "Patreon",
    "Level": "Nivel",
    "XP": "XP",
    "Last XP award": "Último XP otorgado",
    "Downloads": "Descargas",
    "Last download": "Última descarga",
    "Account created": "Cuenta creada",
    "Joined server": "Se unió al servidor",
    "Closed": "Cerrado",
    "Title": "Título",
    "Messages": "Mensajes",
    "Status": "Estado",
    "resolved": "resuelto",
    "unresolved": "sin resolver",
    "Created": "Creado",
    "No cases.": "No hay casos.",
    "No ticket transcripts.": "No hay transcripciones de tickets.",
    "No bug reports.": "No hay reportes de errores.",
    "bot": "bot",
    "not a member": "no es miembro",
    "banned": "baneado",
    "ban status unknown": "estado de baneo desconocido",
    "timed out until ": "silenciado hasta ",
    "Ban reason: ": "Motivo del baneo: ",
    "entitled": "con acceso",
    "not entitled": "sin acceso",
    "No Patreon account linked.": "No hay cuenta de Patreon vinculada.",
    "Last charge: ": "Último cobro: ",
    "View Patreon details ›": "Ver detalles de Patreon ›",
    "Warn": "Advertir",
    "Add note": "Agregar nota",
    "Timeout": "Silenciar",
    "Remove timeout": "Quitar silencio",
    "Kick": "Expulsar",
    "Ban": "Banear",
    "Softban": "Softban",
    "Unban": "Desbanear",
    "The user gets a DM with the reason.": "El usuario recibe un DM con el motivo.",
    "Internal only; the user isn't notified.": "Solo interno; no se notifica al usuario.",
    "Optional": "Opcional",
    "Required": "Obligatorio",
    "Duration in minutes (max 40320 = 28 days)": "Duración en minutos (máx. 40320 = 28 días)",
    "Duration (e.g. 7d; empty = permanent)": "Duración (p. ej. 7d; vacío = permanente)",
    "Delete their messages from the last N hours (0–168)": "Eliminar sus mensajes de las últimas N horas (0–168)",
    "Reason": "Motivo",
    "{action}: {name}": "{action}: {name}",
    "{action}: done": "{action}: listo",
    "Warn ladder applied: {action}": "Escalera de advertencias aplicada: {action}",
    "Couldn't DM the user (DMs closed); the warning is still recorded.":
      "No se pudo enviar el DM (DMs cerrados); la advertencia quedó registrada de todas formas.",
    "A reason is required.": "Se requiere un motivo.",
    "removed": "eliminado",
    "until ": "hasta ",
    "Edit reason": "Editar motivo",
    "Remove": "Eliminar",
    "Are you sure you want to remove this case?": "¿Estás seguro de que quieres eliminar este caso?",
    "Save": "Guardar",
    "Actions": "Acciones",
    "Automod": "Automod",
    "Days": "Días",
    "Hits": "Alertas",
    "Confirmed": "Confirmado",
    "False positives": "Falsos positivos",
    "FP rate": "Tasa FP",
    "Filters": "Filtros",
    "No automod hits in this period.": "Sin alertas de automod en este período.",
    "Tuning suggestions": "Sugerencias de ajuste",
    "of": "de",
    "marked false positive": "marcados como falso positivo",
    "Apply": "Aplicar",
    "No suggestions: staff haven't flagged enough false positives.": "Sin sugerencias: el personal no ha marcado suficientes falsos positivos.",
    "Use the False positive button on automod alerts; each click undoes the automod action and feeds these numbers.": "Usa el botón de Falso positivo en alertas de automod; cada clic deshace la acción de automod y alimenta estos números.",
    "Suggested": "Sugerido",
  });

  const ACTION_KIND = { warn: "warn", note: "", timeout: "warn", kick: "danger", ban: "danger", softban: "danger", delete: "danger", alert: "warn", unban: "ok", untimeout: "ok", appeal: "" };
  const MEMBERS_LIMIT = 60;

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
    const autoSources = ["automod", "escalation", "antiraid", "tempban"];
    const cols = [
      { label: "#", render: (c) => h("span", { class: "mono" }, String(c.id)) },
      { label: t("When"), render: (c) => time(c.created_at) },
      withUser ? { label: t("User"), render: (c) => user(c.user || c.user_id) } : null,
      { label: t("Action"), render: (c) => h("div", { class: "row" },
        actionBadge(c.action),
        c.active === false ? badge(t("removed")) : null,
        c.duration_seconds ? badge(duration(c.duration_seconds)) : null,
        c.expires_at ? h("span", { class: "muted small" }, t("until "), time(c.expires_at)) : null) },
      { label: t("Reason"), render: (c) => c.reason || h("span", { class: "muted" }, "—") },
      { label: t("By"), render: (c) => (autoSources.includes(c.source) ? badge(c.source, "accent") : h("div", { class: "row" },
        user(c.moderator || c.moderator_id),
        c.source === "dyno" ? badge(t("Dyno (deprecated)"), "warn") : c.source === "discord" ? badge(t("via Discord")) : null)) },
    ];
    if (can("mod.cases.edit")) {
      cols.push({
        label: t("Actions"),
        render: (c) => {
          const buttons = [];
          const editBtn = h("button", { class: "btn small ghost", type: "button" }, t("Edit reason"));
          const removeBtn = (c.action === "warn" || c.action === "note") && c.active !== false ? h("button", { class: "btn small danger", type: "button" }, t("Remove")) : null;
          editBtn.addEventListener("click", async (e) => {
            e.stopPropagation();
            const reasonInput = h("textarea", { rows: "3", maxlength: "400", value: c.reason || "" });
            const ok = await dialog(t("Edit reason"), [field(t("Reason"), reasonInput)], { confirmLabel: t("Save"), danger: false });
            if (!ok) return;
            await run(editBtn, () => api(`/api/cases/${c.id}/reason`, { method: "POST", body: { reason: reasonInput.value.trim() } }), t("{action}: done", { action: t("Edit reason") }));
            Panel.refresh();
          });
          buttons.push(editBtn);
          if (removeBtn) {
            removeBtn.addEventListener("click", async (e) => {
              e.stopPropagation();
              const ok = await dialog(t("Remove"), [h("p", {}, t("Are you sure you want to remove this case?"))], { confirmLabel: t("Remove"), danger: true });
              if (!ok) return;
              await run(removeBtn, () => api(`/api/cases/${c.id}`, { method: "DELETE" }), t("{action}: done", { action: t("Remove") }));
              Panel.refresh();
            });
            buttons.push(removeBtn);
          }
          return buttons.length ? h("div", { class: "row" }, buttons) : null;
        }
      });
    }
    return cols.filter(Boolean);
  }

  // ---- Users: member grid + live search -------------------------------------------------------

  // Real SVG (not h(), which uses createElement and would render an inert tag) for the role colour
  // dot. `fill` is an attribute, not a style="" — CSP only blocks the latter.
  function roleDot(role) {
    if (!role) return null;
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "role-dot");
    svg.setAttribute("width", "10");
    svg.setAttribute("height", "10");
    svg.setAttribute("viewBox", "0 0 10 10");
    svg.setAttribute("aria-hidden", "true");
    const circle = document.createElementNS(NS, "circle");
    circle.setAttribute("cx", "5");
    circle.setAttribute("cy", "5");
    circle.setAttribute("r", "5");
    circle.setAttribute("fill", role.color);
    svg.append(circle);
    return svg;
  }

  function memberTile(m) {
    const badges = [];
    if (m.bot) badges.push(badge(t("Bot")));
    if (m.tier && m.tier !== "none") badges.push(badge(t("Staff: {tier}", { tier: t(m.tier) }), "accent"));
    if (m.timed_out_until) badges.push(badge(t("Timed out"), "warn"));
    return h("a", { class: "tile", href: `#/users/${encodeURIComponent(m.id)}`, title: `${m.display_name} (@${m.name}) — ${m.id}` },
      h("img", { src: m.avatar, alt: "" }),
      h("div", { class: "grow" },
        h("div", { class: "name-row" }, roleDot(m.top_role), h("strong", {}, m.display_name)),
        h("div", { class: "muted small" }, `@${m.name}`),
        h("div", { class: "muted small" }, t("Joined"), " ", time(m.joined_at)),
        badges.length ? h("div", { class: "row" }, badges) : null),
      h("span", { class: "go" }, t("View profile ›")));
  }

  async function renderMembers(view) {
    const search = h("input", { type: "search", placeholder: t("Name, display name or user ID…"), "aria-label": t("Search members"), size: "30" });
    const roleSelect = h("select", {}, h("option", { value: "" }, t("All roles")));
    const sortSelect = h("select", {},
      h("option", { value: "joined_desc" }, t("Newest joined")),
      h("option", { value: "joined_asc" }, t("Oldest joined")),
      h("option", { value: "name" }, t("Name")));
    const count = h("span", { class: "muted small members-count" });
    const grid = h("div", { class: "tiles" });
    const idHint = h("div");
    const sentinel = h("div", { class: "sentinel" });
    const loadMore = h("button", { class: "btn", type: "button", hidden: true }, t("Load more"));

    let offset = 0;
    let hasMore = false;
    let loading = false;
    let seq = 0;

    Panel.guild().then((g) => {
      for (const r of g.roles) roleSelect.append(h("option", { value: r.id }, r.name));
    });

    const idFallback = () => {
      const q = search.value.trim();
      idHint.replaceChildren(/^\d{15,20}$/.test(q)
        ? h("p", {},
          h("a", { href: `#/users/${q}` }, t("Open profile for ID {id}", { id: q })),
          " ", h("span", { class: "muted" }, t("(not in the member cache, e.g. banned or left)")))
        : "");
    };

    const load = async (reset) => {
      if (loading) return;
      loading = true;
      const mine = ++seq;
      if (reset) { offset = 0; grid.replaceChildren(); }
      try {
        const params = new URLSearchParams({ sort: sortSelect.value, offset: String(offset), limit: String(MEMBERS_LIMIT) });
        const q = search.value.trim();
        if (q) params.set("q", q);
        if (roleSelect.value) params.set("role_id", roleSelect.value);
        const data = await api(`/api/members?${params}`);
        if (mine !== seq) return;
        offset += data.members.length;
        hasMore = data.has_more;
        loadMore.hidden = !hasMore;
        count.textContent = t("{n} members", { n: data.total.toLocaleString(Panel.locale()) });
        grid.append(...data.members.map(memberTile));
        if (!grid.children.length) grid.replaceChildren(h("div", { class: "empty" }, t("No members match your search.")));
        idFallback();
      } finally {
        loading = false;
      }
    };

    let debounceTimer;
    const debouncedReload = () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => run(null, () => load(true)), 250);
    };
    search.addEventListener("input", debouncedReload);
    roleSelect.addEventListener("change", () => run(null, () => load(true)));
    sortSelect.addEventListener("change", () => run(null, () => load(true)));
    loadMore.addEventListener("click", () => run(loadMore, () => load(false)));

    const observer = new IntersectionObserver((entries) => {
      if (!sentinel.isConnected) { observer.disconnect(); return; }
      if (entries[0].isIntersecting && hasMore && !loading) load(false);
    });
    observer.observe(sentinel);

    view.append(
      h("h1", {}, t("Users")),
      h("div", { class: "card stack" },
        h("div", { class: "row members-toolbar" }, field(t("Search"), search), field(t("Role"), roleSelect), field(t("Sort"), sortSelect), count),
        idHint,
        grid,
        sentinel,
        h("div", { class: "row end" }, loadMore)));
    search.focus();
    await load(true);
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
    { id: "ban", label: "Ban", perm: "mod.ban", danger: true, show: (p) => p.banned !== true, hours: true, duration: true },
    { id: "softban", label: "Softban", perm: "mod.ban", danger: true, show: (p) => p.banned !== true, hours: true },
    { id: "unban", label: "Unban", perm: "mod.ban", show: (p) => p.banned !== false && !p.member },
  ];

  async function runAction(button, profile, action) {
    const name = profile.user.display_name || profile.user.name;
    const reason = h("textarea", { rows: "3", maxlength: "400", placeholder: t(action.optionalReason ? "Optional" : "Required") });
    const minutes = action.minutes ? h("input", { type: "number", min: "1", max: "40320", value: "60" }) : null;
    const hours = action.hours ? h("input", { type: "number", min: "0", max: "168", value: action.id === "softban" ? "24" : "0" }) : null;
    const banLength = action.duration ? h("input", { type: "text", placeholder: "7d" }) : null;
    const ok = await dialog(t("{action}: {name}", { action: t(action.label), name }), [
      action.help ? h("p", { class: "muted" }, t(action.help)) : null,
      minutes ? field(t("Duration in minutes (max 40320 = 28 days)"), minutes) : null,
      banLength ? field(t("Duration (e.g. 7d; empty = permanent)"), banLength) : null,
      hours ? field(t("Delete their messages from the last N hours (0–168)"), hours) : null,
      field(t("Reason"), reason),
    ], { confirmLabel: t(action.label), danger: Boolean(action.danger) });
    if (!ok) return;
    const body = { reason: reason.value.trim() };
    if (minutes) body.minutes = parseInt(minutes.value, 10);
    if (hours) body.delete_message_hours = parseInt(hours.value, 10) || 0;
    if (banLength && banLength.value.trim()) body.duration = banLength.value.trim();
    if (!body.reason && !action.optionalReason) { Panel.toast(t("A reason is required."), true); return; }
    const result = await run(button, () => api(`/api/users/${profile.user.id}/${action.id}`, { method: "POST", body }), t("{action}: done", { action: t(action.label) }));
    if (!result) return;
    if (result.escalation) Panel.toast(t("Warn ladder applied: {action}", { action: t(result.escalation) }));
    if (result.dm_sent === false) Panel.toast(t("Couldn't DM the user (DMs closed); the warning is still recorded."), true);
    Panel.refresh();
  }

  function actionBar(profile) {
    const buttons = ACTIONS
      .filter((a) => can(a.perm) && (!a.member || profile.member) && (!a.show || a.show(profile)))
      .map((a) => {
        const button = h("button", { class: a.danger ? "btn danger" : "btn", type: "button" }, t(a.label));
        button.addEventListener("click", () => runAction(button, profile, a));
        return button;
      });
    return buttons.length ? h("div", { class: "row" }, buttons) : null;
  }

  async function renderProfile(view, id) {
    const p = await api(`/api/users/${encodeURIComponent(id)}`);
    const s = p.sections;
    const flags = [
      p.user.bot ? badge(t("bot")) : null,
      p.tier !== "none" ? badge(t("Staff: {tier}", { tier: t(p.tier) }), "accent") : null,
      p.member ? null : badge(t("not a member"), "warn"),
      p.banned === true ? badge(t("banned"), "danger") : null,
      p.banned === null ? badge(t("ban status unknown"), "warn") : null,
      p.timed_out_until ? h("span", { class: "badge warn" }, t("timed out until "), time(p.timed_out_until)) : null,
    ];

    view.append(
      h("p", {}, h("a", { href: "#/users" }, t("← Users"))),
      h("div", { class: "card stack" },
        h("div", { class: "row spread" },
          h("div", { class: "profile-head" },
            h("img", { class: "avatar-lg", src: p.user.avatar, alt: "" }),
            h("div", {},
              h("h1", {}, p.user.display_name || p.user.name),
              h("div", { class: "muted" }, `@${p.user.name}`))),
          h("span", { class: "mono muted" }, p.user.id)),
        h("div", { class: "row" }, flags),
        h("div", { class: "grid" },
          stat(t("Account created"), time(p.created_at)),
          stat(t("Joined server"), time(p.joined_at))),
        p.ban_reason ? h("p", {}, h("span", { class: "muted" }, t("Ban reason: ")), p.ban_reason) : null,
        p.roles.length ? h("div", { class: "row" }, p.roles.map((r) => badge(r.name))) : null,
        actionBar(p)),
      section(t("Activity"), s.activity, (a) => h("div", { class: "grid" },
        stat(t("Level"), String(a.level)),
        stat(t("XP"), `${a.xp} / ${a.next_level_xp}`),
        stat(t("Last XP award"), time(a.last_award_at)))),
      section(t("Dev jar"), s.dev_jar, (d) => h("div", { class: "grid" },
        stat(t("Downloads"), String(d.downloads)),
        stat(t("Last download"), time(d.last_download_at)))),
      section(t("Mod cases"), s.cases, (cases) => [
        table(caseColumns({ withUser: false }), cases, { empty: t("No cases.") }),
        h("p", { class: "small" }, h("a", { href: `#/audit/${p.user.id}` }, t("View in Audit log ›")))]),
      section(t("Tickets"), s.tickets, (tickets) => table([
        { label: t("Closed"), render: (t2) => time(t2.closed_at) },
        { label: t("Title"), render: (t2) => t2.title || t2.channel_name || "—" },
        { label: t("Messages"), render: (t2) => String(t2.message_count) },
        { label: t("Status"), render: (t2) => (t2.resolved ? badge(t("resolved"), "ok") : badge(t("unresolved"))) },
      ], tickets, {
        empty: t("No ticket transcripts."),
        onRowClick: Panel.can("tickets.view") ? (t2) => Panel.go(`#/tickets/transcript/${t2.id}`) : undefined,
      })),
      section(t("Bug reports"), s.bug_reports, (bugs) => table([
        { label: t("Created"), render: (b) => time(b.created_at) },
        { label: t("Title"), render: (b) => b.title || h("span", { class: "mono" }, b.thread_id) },
        { label: t("Status"), render: (b) => badge(b.status) },
        { label: t("Issue"), render: (b) => (b.issue_number ? `${b.repo}#${b.issue_number}` : "—") },
      ], bugs, { empty: t("No bug reports.") })),
      section(t("Patreon"), s.patreon, (link) => (link
        ? h("div", { class: "stack" },
          h("div", { class: "row" },
            link.name || "",
            link.entitled ? badge(t("entitled"), "ok") : badge(t("not entitled"), "warn"),
            link.status ? badge(link.status) : null,
            h("span", { class: "muted small" }, t("Last charge: ")), time(link.last_charge_date)),
          can("patreon.view") ? h("p", {}, h("a", { href: `#/patreon/${encodeURIComponent(p.user.id)}` }, t("View Patreon details ›"))) : null)
        : h("p", { class: "muted" }, t("No Patreon account linked.")))));
  }

  // ---- Automod: tuning statistics and suggestions --------------------------------------------------

  Panel.page({
    id: "automod",
    title: "Automod",
    perm: "mod.cases.view",
    group: "Logs",
    async render(view) {
      const daysSelect = h("select", {},
        h("option", { value: "7" }, "7 " + t("Days")),
        h("option", { value: "30", selected: true }, "30 " + t("Days")),
        h("option", { value: "90" }, "90 " + t("Days")));
      const filtersBody = h("div");
      const suggestionsBody = h("div");

      const load = async () => {
        const days = parseInt(daysSelect.value, 10);
        const data = await api(`/api/automod/stats?days=${days}`);

        // Filters table
        if (data.filters.length === 0) {
          filtersBody.replaceChildren(h("p", { class: "muted" }, t("No automod hits in this period.")));
        } else {
          filtersBody.replaceChildren(table([
            { label: t("Filter"), render: (f) => badge(f.reason) },
            { label: t("Hits"), render: (f) => String(f.hits) },
            { label: t("Confirmed"), render: (f) => String(f.confirmed) },
            { label: t("False positives"), render: (f) => String(f.false_positives) },
            { label: t("FP rate"), render: (f) => {
              const rate = f.false_positive_rate * 100;
              const kind = rate >= 20 ? "warn" : "";
              return badge((rate.toFixed(1)) + "%", kind);
            } },
          ], data.filters));
        }

        // Tuning suggestions card
        if (data.suggestions.length === 0) {
          suggestionsBody.replaceChildren(h("p", { class: "muted" }, t("No suggestions: staff haven't flagged enough false positives.")));
        } else {
          const suggestionElements = data.suggestions.map((s) => {
            const stats = h("div", { class: "muted small" },
              s.false_positives + " " + t("of") + " " + s.hits + " " + t("marked false positive"));
            if (!s.setting) {
              return h("div", { class: "card" }, badge(s.reason), stats, h("p", { class: "muted small" }, s.note));
            }
            const applyBtn = can("settings.edit") ? h("button", { class: "btn small", type: "button" }, t("Apply")) : null;
            if (applyBtn) {
              applyBtn.addEventListener("click", async () => {
                await run(applyBtn, () => api(`/api/settings/${s.setting}`, { method: "PUT", body: { value: String(s.suggested) } }), t("Setting updated"));
                Panel.refresh();
              });
            }
            return h("div", { class: "card" },
              h("div", { class: "row spread" },
                h("div", {}, badge(s.reason), stats),
                h("div", { class: "row" }, h("span", { class: "mono" }, `${s.setting}: ${s.current} → ${s.suggested}`), applyBtn)));
          });
          suggestionsBody.replaceChildren(...suggestionElements);
        }
      };

      daysSelect.addEventListener("change", () => run(null, load));

      view.append(
        h("h1", {}, t("Automod")),
        h("div", { class: "card stack" },
          field(t("Days"), daysSelect),
          h("h2", {}, t("Filters")),
          filtersBody),
        h("div", { class: "card stack" },
          h("h2", {}, t("Tuning suggestions")),
          suggestionsBody,
          h("p", { class: "muted small" }, t("Use the False positive button on automod alerts; each click undoes the automod action and feeds these numbers."))));

      await load();
    },
  });

  Panel.page({
    id: "users",
    title: "Users",
    perm: "users.view",
    async render(view, args) {
      if (args[0]) await renderProfile(view, args[0]);
      else await renderMembers(view);
    },
  });

  Panel.caseColumns = caseColumns;  // shared with the Audit log page in logs.js
})();
