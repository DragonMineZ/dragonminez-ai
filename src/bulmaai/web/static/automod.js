"use strict";
// Automod page: a master switch, one card per filter (on/off switch via moderation_disabled_filters,
// hit stats from /api/automod/stats, tuning suggestion, Edit dialog for its thresholds) and a
// Default settings tab. Everything saved here is a plain setting, so it also shows on the Settings page.

(() => {
  const { h, api, run, can, field, badge, dialog, guild, channelName, roleName, t } = Panel;

  Panel.i18n({
    "Automod": "Automod",
    "Filters": "Filtros",
    "Default settings": "Configuración predeterminada",
    "Enable automod": "Activar automod",
    "Automod on": "Automod activado",
    "Automod off": "Automod desactivado",
    "Automod is off: no filter runs until you turn it back on.": "Automod está desactivado: ningún filtro se ejecuta hasta que lo vuelvas a activar.",
    "Each filter can be switched off on its own; its thresholds are kept for when you turn it back on.": "Cada filtro se puede apagar por separado; sus límites se guardan para cuando lo vuelvas a encender.",
    "Only admins can change automod settings.": "Solo los admins pueden cambiar la configuración de automod.",
    "Search filters": "Buscar filtros",
    "Show": "Mostrar",
    "All": "Todos",
    "On": "Activados",
    "Off": "Desactivados",
    "Stats for": "Estadísticas de",
    "Last {days} days": "Últimos {days} días",
    "{on} of {total} filters on": "{on} de {total} filtros activados",
    "No filters match.": "Ningún filtro coincide.",
    "Edit": "Editar",
    "Save": "Guardar",
    "Saved {name}": "Se guardó {name}",
    "Nothing changed.": "No hubo cambios.",
    "{name} on": "{name} activado",
    "{name} off": "{name} desactivado",
    "0 turns this check off.": "0 desactiva esta revisión.",
    "Adds a warn strike": "Suma una advertencia",
    "Deletes": "Borra",
    "Times out": "Aísla",
    "Alerts staff": "Avisa al staff",
    "Deletes, times out across channels": "Borra; aísla si es en varios canales",
    "Deletes + warns": "Borra y advierte",
    "custom": "personalizado",
    "When it triggers": "Cuando se activa",
    "Default: {action}": "Predeterminado: {action}",
    "Delete + warn strike": "Borrar y sumar advertencia",
    "Time out for 10 minutes": "Aislar 10 minutos",
    "Only for this filter. Picking a category covers all its channels.": "Solo para este filtro. Elegir una categoría cubre todos sus canales.",
    "Members with any of these roles skip this filter.": "Los miembros con cualquiera de estos roles se saltan este filtro.",
    "Ignored in {places}": "Ignorado en {places}",
    "Skipped for {roles}": "Omitido para {roles}",
    "category": "categoría",
    "Limits": "Límites",
    "No hits": "Sin alertas",
    "{hits} hits · {confirmed} confirmed · {fp} false positives": "{hits} alertas · {confirmed} confirmadas · {fp} falsos positivos",
    "{rate}% false positives": "{rate}% falsos positivos",
    "Suggested": "Sugerido",
    "Apply": "Aplicar",
    "Setting updated": "Configuración actualizada",
    "Other hits": "Otras alertas",
    "Filter": "Filtro",
    "Hits": "Alertas",
    "Confirmed": "Confirmado",
    "False positives": "Falsos positivos",
    "Use the False positive button on automod alerts; each click undoes the automod action and feeds these numbers.": "Usa el botón de Falso positivo en alertas de automod; cada clic deshace la acción de automod y alimenta estos números.",
    "Couldn't load hit stats: {error}": "No se pudieron cargar las estadísticas: {error}",
    "on": "sí",
    "off": "no",
    "none": "ninguno",
    "{count} entries": "{count} entradas",
    "Log channel": "Canal de registro",
    "Where automod alerts go.": "Dónde llegan las alertas de automod.",
    "Ignored channels": "Canales ignorados",
    "Automod never checks messages in these channels.": "Automod nunca revisa mensajes en estos canales.",
    "Exempt roles": "Roles exentos",
    "Members with any of these roles are never checked. Staff are always exempt.": "Los miembros con cualquiera de estos roles nunca se revisan. El staff siempre está exento.",
    "Warn ladder": "Escalera de advertencias",
    "What warn strikes lead to, e.g. 2/7d=24h, 5/30d=3d, 7/30d=ban.": "A qué llevan las advertencias, p. ej. 2/7d=24h, 5/30d=3d, 7/30d=ban.",
    "DM members when automod or staff act on them": "Enviar MD a los miembros cuando automod o el staff actúan sobre ellos",
    "Filter channels…": "Filtrar canales…",
    "Filter roles…": "Filtrar roles…",
    "{count} selected": "{count} seleccionados",
    "Loading…": "Cargando…",
    "— none —": "— ninguno —",
    "Blocked links": "Enlaces bloqueados",
    "Deletes links to blocked domains.": "Borra enlaces a dominios bloqueados.",
    "Blocked domains": "Dominios bloqueados",
    "Always-allowed domains": "Dominios siempre permitidos",
    "Comma-separated, e.g. example.com, bad.site": "Separados por comas, p. ej. example.com, bad.site",
    "Phishing links": "Enlaces de phishing",
    "Checks links against the PhishDestroy threat list.": "Revisa los enlaces contra la lista de amenazas de PhishDestroy.",
    "Action": "Acción",
    "Alert staff only": "Solo avisar al staff",
    "Delete the message": "Borrar el mensaje",
    "Scam images": "Imágenes de estafa",
    "Matches known scam pictures by image hash.": "Detecta imágenes de estafa conocidas por su hash.",
    "Delete matches (off = alert only)": "Borrar coincidencias (apagado = solo avisar)",
    "Match distance (lower = stricter)": "Distancia de coincidencia (menor = más estricto)",
    "Invite links": "Enlaces de invitación",
    "Deletes Discord server invites.": "Borra invitaciones a servidores de Discord.",
    "Link shorteners": "Acortadores de enlaces",
    "Alerts staff about shortened links (bit.ly and similar).": "Avisa al staff de enlaces acortados (bit.ly y similares).",
    "Link spam": "Spam de enlaces",
    "Times out users posting many links quickly.": "Aísla a usuarios que publican muchos enlaces rápido.",
    "Links": "Enlaces",
    "Window (seconds)": "Ventana (segundos)",
    "Banned words": "Palabras prohibidas",
    "Deletes messages with banned words. * works as a wildcard.": "Borra mensajes con palabras prohibidas. * funciona como comodín.",
    "Words": "Palabras",
    "Comma-separated, * is a wildcard: free*nitro": "Separadas por comas, * es comodín: free*nitro",
    "Mass mentions": "Menciones masivas",
    "Deletes messages pinging many users or roles.": "Borra mensajes que mencionan a muchos usuarios o roles.",
    "Mention limit": "Límite de menciones",
    "@everyone / @here": "@everyone / @here",
    "Deletes @everyone and @here from members who can't use them; the 3rd try in 10 minutes is a warn.": "Borra @everyone y @here de miembros que no pueden usarlos; el 3.er intento en 10 minutos es un aviso.",
    "Excessive caps": "Exceso de mayúsculas",
    "Deletes mostly-uppercase messages.": "Borra mensajes casi todo en mayúsculas.",
    "Caps %": "% de mayúsculas",
    "Minimum letters": "Letras mínimas",
    "Excessive emoji": "Exceso de emojis",
    "Deletes messages with too many emoji.": "Borra mensajes con demasiados emojis.",
    "Emoji limit": "Límite de emojis",
    "Wall of text": "Muro de texto",
    "Deletes messages with too many line breaks (code blocks don't count).": "Borra mensajes con demasiados saltos de línea (los bloques de código no cuentan).",
    "Line limit": "Límite de líneas",
    "Zalgo text": "Texto zalgo",
    "Deletes glitchy zalgo text.": "Borra texto zalgo distorsionado.",
    "Duplicate messages": "Mensajes duplicados",
    "Deletes the same message sent over and over.": "Borra el mismo mensaje enviado una y otra vez.",
    "Copies": "Copias",
    "Fast messages": "Mensajes rápidos",
    "Deletes messages sent too quickly in a row.": "Borra mensajes enviados demasiado rápido seguidos.",
    "Messages": "Mensajes",
    "Image spam": "Spam de imágenes",
    "Times out users flooding images across messages or channels.": "Aísla a usuarios que inundan imágenes en varios mensajes o canales.",
    "Images": "Imágenes",
    "Minimum messages": "Mensajes mínimos",
    "Timeout (seconds)": "Aislamiento (segundos)",
  });

  const DISABLED = "moderation_disabled_filters";
  const RULES = "moderation_filter_rules";
  // services/moderation.py RULE_ACTIONS; "" = the filter's built-in behaviour.
  const RULE_ACTIONS = [["alert", "Alert staff only"], ["delete", "Delete the message"], ["warn", "Delete + warn strike"], ["timeout", "Time out for 10 minutes"]];
  const RULE_BADGE = { alert: "Alerts staff", delete: "Deletes", warn: "Deletes + warns", timeout: "Times out" };
  const DAYS_KEY = "panel.automod.days";
  const DELETES = "Deletes";

  // id: entry in moderation_disabled_filters; stat: automod_hits.reason when it differs from id.
  // flag: the filter's own bool setting, switched on together with the card. warns: _WARN_REASONS.
  // params: [setting, label, {hint, off: "0 turns it off", choices}]. ownAction: has its own action setting,
  // so its Edit dialog offers no rule action (exemptions still apply).
  const FILTERS = [
    { id: "blocked_domain", name: "Blocked links", desc: "Deletes links to blocked domains.", action: DELETES, warns: true,
      params: [["moderation_blocked_domains", "Blocked domains", { hint: "Comma-separated, e.g. example.com, bad.site" }],
        ["moderation_allowed_domains", "Always-allowed domains", { hint: "Comma-separated, e.g. example.com, bad.site" }]] },
    { id: "phishdestroy_domain", name: "Phishing links", desc: "Checks links against the PhishDestroy threat list.", ownAction: true,
      action: (s) => (s.phishdestroy_action.value === "delete" ? DELETES : "Alerts staff"),
      params: [["phishdestroy_action", "Action", { choices: [["alert", "Alert staff only"], ["delete", "Delete the message"]] }]] },
    { id: "scam_image", name: "Scam images", desc: "Matches known scam pictures by image hash.", flag: "moderation_scam_images_enabled", ownAction: true,
      action: (s) => (s.moderation_scam_images_enforce.value ? DELETES : "Alerts staff"),
      params: [["moderation_scam_images_enforce", "Delete matches (off = alert only)"], ["moderation_scam_image_distance", "Match distance (lower = stricter)"]] },
    { id: "discord_invite", name: "Invite links", desc: "Deletes Discord server invites.", action: DELETES, flag: "moderation_block_discord_invites", warns: true, params: [] },
    { id: "suspicious_shortener", name: "Link shorteners", desc: "Alerts staff about shortened links (bit.ly and similar).", action: "Alerts staff", params: [] },
    { id: "link_burst", stat: "link burst", name: "Link spam", desc: "Times out users posting many links quickly.", action: "Times out",
      params: [["moderation_link_burst_count", "Links", { off: true }], ["moderation_link_burst_window_seconds", "Window (seconds)"]] },
    { id: "banned_word", name: "Banned words", desc: "Deletes messages with banned words. * works as a wildcard.", action: DELETES, warns: true,
      params: [["moderation_banned_words", "Words", { hint: "Comma-separated, * is a wildcard: free*nitro" }]] },
    { id: "mass_mention", name: "Mass mentions", desc: "Deletes messages pinging many users or roles.", action: DELETES, warns: true,
      params: [["moderation_mass_mention_limit", "Mention limit", { off: true }]] },
    { id: "everyone_ping", name: "@everyone / @here", desc: "Deletes @everyone and @here from members who can't use them; the 3rd try in 10 minutes is a warn.", action: DELETES,
      flag: "moderation_block_everyone_ping", params: [] },
    { id: "excessive_caps", name: "Excessive caps", desc: "Deletes mostly-uppercase messages.", action: DELETES,
      params: [["moderation_caps_percent", "Caps %", { off: true }], ["moderation_caps_min_length", "Minimum letters"]] },
    { id: "excessive_emoji", name: "Excessive emoji", desc: "Deletes messages with too many emoji.", action: DELETES,
      params: [["moderation_emoji_limit", "Emoji limit", { off: true }]] },
    { id: "wall_of_text", name: "Wall of text", desc: "Deletes messages with too many line breaks (code blocks don't count).", action: DELETES,
      params: [["moderation_newline_limit", "Line limit", { off: true }]] },
    { id: "zalgo", name: "Zalgo text", desc: "Deletes glitchy zalgo text.", action: DELETES, flag: "moderation_zalgo_enabled", params: [] },
    { id: "duplicate_spam", name: "Duplicate messages", desc: "Deletes the same message sent over and over.", action: "Deletes, times out across channels",
      params: [["moderation_duplicate_count", "Copies", { off: true }], ["moderation_duplicate_window_seconds", "Window (seconds)"]] },
    { id: "fast_messages", name: "Fast messages", desc: "Deletes messages sent too quickly in a row.", action: DELETES,
      params: [["moderation_fast_message_count", "Messages", { off: true }], ["moderation_fast_message_window_seconds", "Window (seconds)"]] },
    { id: "image_burst", stat: "image burst", name: "Image spam", desc: "Times out users flooding images across messages or channels.", action: "Times out",
      params: [["moderation_image_burst_count", "Images", { off: true }], ["moderation_image_burst_window_seconds", "Window (seconds)"],
        ["moderation_image_burst_min_messages", "Minimum messages"], ["moderation_image_burst_timeout_seconds", "Timeout (seconds)"]] },
  ];
  const STAT_KEYS = new Set(FILTERS.map((f) => f.stat || f.id));

  const asText = (value) => (Array.isArray(value) ? value.join(", ") : value === null || value === undefined ? "" : String(value));
  const put = (name, value) => api(`/api/settings/${encodeURIComponent(name)}`, { method: "PUT", body: { value } });
  const storedDays = () => { try { return localStorage.getItem(DAYS_KEY) || "30"; } catch { return "30"; } };

  function switchInput(checked, label, locked, onChange) {
    const input = h("input", { type: "checkbox", class: "switch", role: "switch", checked, disabled: locked, "aria-label": label });
    input.addEventListener("change", () => onChange(input));
    return input;
  }

  // run() swallows errors; put the switch back when the save didn't go through.
  async function flip(input, fn, message) {
    let ok = false;
    await run(input, async () => { await fn(); ok = true; }, message);
    if (!ok) input.checked = !input.checked;
  }

  function rulesOf(s) {
    try {
      const rules = JSON.parse(s[RULES].value || "{}");
      return rules && typeof rules === "object" && !Array.isArray(rules) ? rules : {};
    } catch {
      return {};
    }
  }

  // Channels (and categories, which cover every channel in them) for the exemption pickers.
  function placeItems(g) {
    const kinds = ["text", "news", "forum", "voice", "stage_voice", "category"];
    const rank = (c) => (c.type === "category" ? 0 : 1);
    return g.channels.filter((c) => kinds.includes(c.type)).sort((a, b) => rank(a) - rank(b) || a.position - b.position)
      .map((c) => ({ id: c.id, label: c.type === "category" ? `${c.name} (${t("category")})` : `#${c.name}${c.category ? ` (${c.category})` : ""}` }));
  }

  const roleItems = (g) => g.roles.map((r) => ({ id: r.id, label: `@${r.name}` }));

  function nameOf(items, id) {
    const found = items.find((item) => item.id === String(id));
    return found ? found.label : String(id);
  }

  function defaultAction(filter, s) {
    return typeof filter.action === "function" ? filter.action(s) : filter.action;
  }

  function isOn(filter, s) {
    return (!filter.flag || s[filter.flag].value) && !s[DISABLED].value.includes(filter.id);
  }

  function paramInput(setting, opts) {
    if (opts.choices) {
      const select = h("select", {}, opts.choices.map(([value, label]) => h("option", { value }, t(label))));
      select.value = setting.value;
      return select;
    }
    if (setting.kind === "bool") return h("input", { type: "checkbox", checked: setting.value });
    if (setting.kind.endsWith("_list")) return h("textarea", { rows: "3", value: asText(setting.value) });
    const number = setting.kind === "int" || setting.kind === "float";
    return h("input", { type: number ? "number" : "text", min: number ? "0" : null, step: setting.kind === "float" ? "any" : null, value: asText(setting.value) });
  }

  function summary(filter, s) {
    return h("dl", { class: "kv small" }, filter.params.map(([name, label, opts = {}]) => {
      const setting = s[name];
      let value = setting.value;
      if (opts.choices) value = t((opts.choices.find(([v]) => v === value) || [, value])[1]);
      else if (setting.kind === "bool") value = t(value ? "on" : "off");
      else if (Array.isArray(value)) value = value.length === 0 ? t("none") : value.length <= 3 ? value.join(", ") : t("{count} entries", { count: value.length });
      else if (opts.off && Number(value) === 0) value = `0 (${t("off")})`;
      return [h("dt", {}, t(label)), h("dd", {}, String(value))];
    }));
  }

  async function editFilter(filter, s, g, reload) {
    const inputs = filter.params.map(([name, label, opts = {}]) => {
      const setting = s[name];
      const input = paramInput(setting, opts);
      const raw = () => (setting.kind === "bool" ? String(input.checked) : input.value.trim());
      const help = opts.hint ? t(opts.hint) : opts.off ? t("0 turns this check off.") : null;
      const el = setting.kind === "bool"
        ? h("label", { class: "row check" }, input, t(label))
        : h("div", {}, field(t(label), input), help ? h("div", { class: "muted small" }, help) : null);
      return { name, raw, before: raw(), el };
    });
    const limits = inputs.map((p) => p.el);
    const rules = rulesOf(s);
    const rule = rules[filter.id] || {};
    const actionSelect = filter.ownAction ? null : h("select", {},
      h("option", { value: "" }, t("Default: {action}", { action: t(defaultAction(filter, s)) })),
      RULE_ACTIONS.map(([value, label]) => h("option", { value }, t(label))));
    if (actionSelect) actionSelect.value = rule.action || "";
    const places = idPicker(placeItems(g), rule.channels || [], t("Filter channels…"));
    const roles = idPicker(roleItems(g), rule.roles || [], t("Filter roles…"));
    const ruleRaw = () => {
      const next = { ...rule, channels: places.list(), roles: roles.list() };
      if (actionSelect) next.action = actionSelect.value;
      for (const key of ["action", "channels", "roles"]) if (!next[key] || next[key].length === 0) delete next[key];
      const all = { ...rules, [filter.id]: next };
      if (!Object.keys(next).length) delete all[filter.id];
      return Object.keys(all).length ? JSON.stringify(all) : "";
    };
    inputs.push({ name: RULES, raw: ruleRaw, before: ruleRaw() });

    const body = [
      limits.length ? h("h3", { class: "dialog-section" }, t("Limits")) : null,
      limits,
      actionSelect ? field(t("When it triggers"), actionSelect) : null,
      h("div", {}, h("label", {}, t("Ignored channels")), h("div", { class: "muted small hint" }, t("Only for this filter. Picking a category covers all its channels.")), places.el),
      h("div", {}, h("label", {}, t("Exempt roles")), h("div", { class: "muted small hint" }, t("Members with any of these roles skip this filter.")), roles.el),
    ];
    if (!(await dialog(t(filter.name), body, { confirmLabel: t("Save") }))) return;
    const changed = inputs.filter((p) => p.raw() !== p.before);
    if (!changed.length) { Panel.toast(t("Nothing changed.")); return; }
    await run(null, async () => {
      for (const p of changed) await put(p.name, p.raw());
    }, t("Saved {name}", { name: t(filter.name) }));
    await reload();
  }

  function statLine(stat) {
    if (!stat) return h("div", { class: "muted small" }, t("No hits"));
    const rate = Math.round(stat.false_positive_rate * 1000) / 10;
    return h("div", { class: "row small" },
      h("span", { class: "muted" }, t("{hits} hits · {confirmed} confirmed · {fp} false positives",
        { hits: stat.hits, confirmed: stat.confirmed, fp: stat.false_positives })),
      stat.false_positives ? badge(t("{rate}% false positives", { rate }), rate >= 20 ? "warn" : "") : null);
  }

  function suggestionBox(suggestion, reload) {
    if (!suggestion) return null;
    if (!suggestion.setting) return h("div", { class: "suggestion small" }, h("strong", {}, t("Suggested")), " ", suggestion.note);
    const apply = can("settings.edit") ? h("button", { class: "btn small", type: "button" }, t("Apply")) : null;
    if (apply) {
      apply.addEventListener("click", async () => {
        if (await run(apply, () => put(suggestion.setting, String(suggestion.suggested)), t("Setting updated")) !== undefined) await reload();
      });
    }
    return h("div", { class: "suggestion small row spread" },
      h("span", {}, h("strong", {}, t("Suggested")), " ", h("span", { class: "mono" }, `${suggestion.setting}: ${suggestion.current} → ${suggestion.suggested}`)),
      apply);
  }

  function filterCard(filter, ctx) {
    const { s, stats, suggestions, reload } = ctx;
    const locked = !s || !can("settings.edit");
    const on = s ? isOn(filter, s) : true;
    const key = filter.stat || filter.id;
    const allRules = s ? rulesOf(s) : {};
    const rule = (!filter.ownAction && allRules[filter.id]) || {};
    const exempt = allRules[filter.id] || {};
    const action = rule.action ? RULE_BADGE[rule.action] : s || typeof filter.action !== "function" ? defaultAction(filter, s) : null;
    const warns = rule.action ? rule.action === "warn" : filter.warns;
    const g = ctx.g;

    const sw = s ? switchInput(on, t(filter.name), locked, (input) => flip(input, async () => {
      const rest = s[DISABLED].value.filter((id) => id !== filter.id);
      const next = input.checked ? rest : [...rest, filter.id];
      if (next.length !== s[DISABLED].value.length) await put(DISABLED, next.join(","));
      if (input.checked && filter.flag && !s[filter.flag].value) await put(filter.flag, "true");
      await reload();
    }, t(input.checked ? "{name} on" : "{name} off", { name: t(filter.name) }))) : null;

    const edit = s && g && !locked
      ? h("button", { class: "btn small ghost", type: "button", onclick: () => editFilter(filter, s, g, reload) }, t("Edit"))
      : null;

    return h("article", { class: on ? "card filter" : "card filter off", "aria-label": t(filter.name) },
      h("div", { class: "row spread nowrap" }, h("h3", {}, t(filter.name)), sw),
      h("p", { class: "muted small" }, t(filter.desc)),
      h("div", { class: "row" }, action ? badge(t(action), action === DELETES ? "" : "accent") : null,
        warns ? badge(t("Adds a warn strike"), "warn") : null,
        rule.action ? badge(t("custom")) : null),
      s && filter.params.length ? summary(filter, s) : null,
      g && (exempt.channels || []).length
        ? h("div", { class: "small exempt" }, t("Ignored in {places}", { places: exempt.channels.map((id) => nameOf(placeItems(g), id)).join(", ") }))
        : null,
      g && (exempt.roles || []).length
        ? h("div", { class: "small exempt" }, t("Skipped for {roles}", { roles: exempt.roles.map((id) => nameOf(roleItems(g), id)).join(", ") }))
        : null,
      stats ? statLine(stats.get(key)) : null,
      suggestionBox(suggestions && suggestions.get(key), reload),
      edit ? h("div", { class: "row end tight" }, edit) : null);
  }

  // Searchable checkbox chips for channel/role ID lists.
  function idPicker(items, selected, placeholder) {
    const chosen = new Set(selected.map(String));
    const search = h("input", { type: "search", placeholder, "aria-label": placeholder });
    const count = h("span", { class: "muted small" });
    const box = h("div", { class: "role-picker" });
    const boxes = items.map((item) => {
      const cb = h("input", { type: "checkbox", checked: chosen.has(item.id) });
      cb.addEventListener("change", () => { if (cb.checked) chosen.add(item.id); else chosen.delete(item.id); counted(); });
      const label = h("label", { title: item.id }, cb, item.label);
      box.append(label);
      return { item, label };
    });
    const counted = () => { count.textContent = t("{count} selected", { count: chosen.size }); };
    search.addEventListener("input", () => {
      const q = search.value.trim().toLowerCase();
      for (const { item, label } of boxes) label.hidden = Boolean(q) && !item.label.toLowerCase().includes(q);
    });
    counted();
    // Keep IDs that no longer exist in the server so saving doesn't silently drop them.
    return {
      el: h("div", { class: "stack tight" }, h("div", { class: "row" }, search, count), box),
      get: () => [...chosen].join(","),
      list: () => [...chosen],
    };
  }

  async function renderDefaults(container, s, reload) {
    const locked = !can("settings.edit");
    const g = await guild();
    const textChannels = g.channels.filter((c) => ["text", "news", "forum", "voice", "stage_voice"].includes(c.type))
      .sort((a, b) => a.position - b.position)
      .map((c) => ({ id: c.id, label: `#${c.name}${c.category ? ` (${c.category})` : ""}` }));
    const roles = g.roles.map((r) => ({ id: r.id, label: `@${r.name}` }));

    const logSelect = h("select", {}, h("option", { value: "" }, t("— none —")),
      textChannels.map((c) => h("option", { value: c.id }, c.label)));
    logSelect.value = s.moderation_log_channel_id.value ? String(s.moderation_log_channel_id.value) : "";
    const channels = idPicker(textChannels, s.moderation_excluded_channel_ids.value, t("Filter channels…"));
    const exempt = idPicker(roles, s.moderation_exempt_role_ids.value, t("Filter roles…"));
    const ladder = h("input", { type: "text", value: s.moderation_warn_ladder.value });
    const dm = h("input", { type: "checkbox", checked: s.moderation_dm_on_action.value });

    const fields = [
      ["moderation_log_channel_id", () => logSelect.value],
      ["moderation_excluded_channel_ids", channels.get],
      ["moderation_exempt_role_ids", exempt.get],
      ["moderation_warn_ladder", () => ladder.value.trim()],
      ["moderation_dm_on_action", () => String(dm.checked)],
    ].map(([name, raw]) => ({ name, raw, before: raw() }));

    const save = h("button", { class: "btn primary", type: "button", disabled: locked }, t("Save"));
    save.addEventListener("click", () => {
      const changed = fields.filter((f) => f.raw() !== f.before);
      if (!changed.length) { Panel.toast(t("Nothing changed.")); return; }
      run(save, async () => {
        for (const f of changed) await put(f.name, f.raw());
        await reload();
      }, t("Saved {name}", { name: t("Default settings") }));
    });
    for (const el of [logSelect, ladder, dm]) el.disabled = locked;

    container.replaceChildren(h("div", { class: "card stack defaults" },
      h("div", {}, field(t("Log channel"), logSelect), h("div", { class: "muted small" }, t("Where automod alerts go."))),
      h("div", {}, h("label", {}, t("Ignored channels")), h("div", { class: "muted small hint" }, t("Automod never checks messages in these channels.")), channels.el),
      h("div", {}, h("label", {}, t("Exempt roles")), h("div", { class: "muted small hint" }, t("Members with any of these roles are never checked. Staff are always exempt.")), exempt.el),
      h("div", {}, field(t("Warn ladder"), ladder), h("div", { class: "muted small" }, t("What warn strikes lead to, e.g. 2/7d=24h, 5/30d=3d, 7/30d=ban."))),
      h("label", { class: "row check" }, dm, t("DM members when automod or staff act on them")),
      h("div", { class: "row end" }, save)));
    if (locked) for (const el of container.querySelectorAll("input, select, textarea, button")) el.disabled = true;
  }

  function otherHits(stats) {
    const rows = [...stats.values()].filter((stat) => !STAT_KEYS.has(stat.reason));
    if (!rows.length) return null;
    return h("div", { class: "card stack" }, h("h2", {}, t("Other hits")), Panel.table([
      { label: t("Filter"), render: (f) => badge(f.reason) },
      { label: t("Hits"), render: (f) => String(f.hits) },
      { label: t("Confirmed"), render: (f) => String(f.confirmed) },
      { label: t("False positives"), render: (f) => String(f.false_positives) },
    ], rows, { hint: null }));
  }

  Panel.page({
    id: "automod",
    title: "Automod",
    perm: "mod.cases.view",
    group: "Moderation",
    async render(view, args) {
      const canSettings = can("settings.view");
      let tab = args[0] === "defaults" && canSettings ? "defaults" : "filters";
      const master = h("div", { class: "row" });
      const banner = h("div");
      const tabsEl = h("div", { class: "tabs", role: "tablist" });
      const body = h("div");

      const search = h("input", { type: "search", placeholder: t("Search filters"), "aria-label": t("Search filters") });
      const show = h("select", { "aria-label": t("Show") },
        h("option", { value: "all" }, t("All")), h("option", { value: "on" }, t("On")), h("option", { value: "off" }, t("Off")));
      const days = h("select", { "aria-label": t("Stats for") },
        ["7", "30", "90"].map((d) => h("option", { value: d }, t("Last {days} days", { days: d }))));
      days.value = storedDays();
      const counter = h("span", { class: "muted small" });
      const grid = h("div", { class: "filters" });
      const other = h("div");
      const filtersPane = h("div", {},
        h("div", { class: "card row toolbar" }, search, show, days, counter),
        canSettings ? null : h("p", { class: "muted" }, t("Only admins can change automod settings.")),
        grid, other,
        h("p", { class: "muted small" }, t("Use the False positive button on automod alerts; each click undoes the automod action and feeds these numbers.")));
      const defaultsPane = h("div");

      const ctx = { s: null, g: null, stats: null, suggestions: null, reload: null };

      const drawGrid = () => {
        const q = search.value.trim().toLowerCase();
        const shown = FILTERS.filter((f) => {
          if (q && !`${t(f.name)} ${t(f.desc)} ${f.id}`.toLowerCase().includes(q)) return false;
          if (show.value === "all" || !ctx.s) return true;
          return isOn(f, ctx.s) === (show.value === "on");
        });
        grid.replaceChildren(...(shown.length ? shown.map((f) => filterCard(f, ctx)) : [h("p", { class: "empty" }, t("No filters match."))]));
        if (ctx.s) counter.textContent = t("{on} of {total} filters on", { on: FILTERS.filter((f) => isOn(f, ctx.s)).length, total: FILTERS.length });
        other.replaceChildren(...[ctx.stats ? otherHits(ctx.stats) : null].filter(Boolean));
      };

      const drawTabs = () => {
        tabsEl.replaceChildren(...[["filters", "Filters"], ["defaults", "Default settings"]]
          .filter(([id]) => id === "filters" || canSettings)
          .map(([id, label]) => h("button", { type: "button", role: "tab", "aria-selected": String(tab === id), "aria-current": String(tab === id),
            onclick: () => { tab = id; history.replaceState(null, "", `#/automod${id === "defaults" ? "/defaults" : ""}`); drawTabs(); drawBody(); } }, t(label))));
      };

      const drawBody = () => {
        body.replaceChildren(tab === "filters" ? filtersPane : defaultsPane);
        if (tab === "defaults" && ctx.s) run(null, () => renderDefaults(defaultsPane, ctx.s, ctx.reload));
      };

      const drawMaster = () => {
        if (!ctx.s) return;
        const enabled = ctx.s.moderation_enabled.value;
        master.replaceChildren(h("label", { class: "row check master" },
          switchInput(enabled, t("Enable automod"), !can("settings.edit"), (input) => flip(input, async () => {
            await put("moderation_enabled", String(input.checked));
            await ctx.reload();
          }, t(input.checked ? "Automod on" : "Automod off"))),
          t("Enable automod")));
        banner.replaceChildren(...(enabled ? [] : [h("div", { class: "card banner warn", role: "status" }, t("Automod is off: no filter runs until you turn it back on."))]));
        grid.classList.toggle("dimmed", !enabled);
      };

      const loadSettings = async () => {
        if (!canSettings) return;
        const [data, g] = await Promise.all([api("/api/settings"), guild()]);
        ctx.s = Object.fromEntries(data.settings.map((x) => [x.name, x]));
        ctx.g = g;
      };
      const loadStats = async () => {
        try {
          const data = await api(`/api/automod/stats?days=${days.value}`);
          ctx.stats = new Map(data.filters.map((f) => [f.reason, f]));
          ctx.suggestions = new Map(data.suggestions.map((x) => [x.reason, x]));
        } catch (error) {
          ctx.stats = null;
          ctx.suggestions = null;
          Panel.toast(t("Couldn't load hit stats: {error}", { error: error.message }), true);
        }
      };
      ctx.reload = async () => {
        await Promise.all([loadSettings(), loadStats()]);
        drawMaster();
        drawGrid();
        if (tab === "defaults") drawBody();
      };

      search.addEventListener("input", drawGrid);
      show.addEventListener("change", drawGrid);
      days.addEventListener("change", () => {
        try { localStorage.setItem(DAYS_KEY, days.value); } catch { /* private mode */ }
        run(null, async () => { await loadStats(); drawGrid(); });
      });

      view.append(
        h("div", { class: "row spread page-head" }, h("h1", {}, t("Automod")), master),
        h("p", { class: "muted" }, t("Each filter can be switched off on its own; its thresholds are kept for when you turn it back on.")),
        banner, tabsEl, body);
      drawTabs();
      await ctx.reload();
      drawBody();
    },
  });
})();
