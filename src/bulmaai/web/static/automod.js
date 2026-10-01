"use strict";
// Automod page: one card per filter with an on/off switch (moderation_disabled_filters) and its
// thresholds. Everything here is a plain setting, so it also shows up on the Settings page.

(() => {
  const { h, api, run, can, field, badge, t } = Panel;

  Panel.i18n({
    "Automod": "Automod",
    "Enable automod": "Activar automod",
    "Each filter can be switched off on its own; its thresholds are kept for when you turn it back on. Staff, exempt roles and excluded channels are never checked.": "Cada filtro se puede apagar por separado; sus límites se guardan para cuando lo vuelvas a encender. El staff, los roles exentos y los canales excluidos nunca se revisan.",
    "Search filters": "Buscar filtros",
    "Save": "Guardar",
    "Saved {name}": "Se guardó {name}",
    "{name} on": "{name} activado",
    "{name} off": "{name} desactivado",
    "Automod on": "Automod activado",
    "Automod off": "Automod desactivado",
    "0 turns this check off.": "0 desactiva esta revisión.",
    "Warns": "Advierte",
    "No settings, just on/off.": "Sin opciones, solo encendido/apagado.",
    "Blocked links": "Enlaces bloqueados",
    "Deletes links to blocked domains.": "Borra enlaces a dominios bloqueados.",
    "Blocked domains": "Dominios bloqueados",
    "Always-allowed domains": "Dominios siempre permitidos",
    "Phishing links": "Enlaces de phishing",
    "Checks links against the PhishDestroy threat list.": "Revisa los enlaces contra la lista de amenazas de PhishDestroy.",
    "Action (alert or delete)": "Acción (alert o delete)",
    "Scam images": "Imágenes de estafa",
    "Matches known scam pictures by image hash.": "Detecta imágenes de estafa conocidas por su hash.",
    "Delete matches (off = alert only)": "Borrar coincidencias (apagado = solo alertar)",
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
    "Words (comma-separated)": "Palabras (separadas por comas)",
    "Mass mentions": "Menciones masivas",
    "Deletes messages pinging many users or roles.": "Borra mensajes que mencionan a muchos usuarios o roles.",
    "Mention limit": "Límite de menciones",
    "@everyone / @here": "@everyone / @here",
    "Deletes @everyone and @here from members who can't use them.": "Borra @everyone y @here de miembros que no pueden usarlos.",
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
  // flag: the filter's own bool setting; switching the card on also sets it true.
  // warns: these hits add a warn strike (cogs/moderation.py _WARN_REASONS).
  const FILTERS = [
    { id: "blocked_domain", name: "Blocked links", desc: "Deletes links to blocked domains.", warns: true,
      params: [["moderation_blocked_domains", "Blocked domains"], ["moderation_allowed_domains", "Always-allowed domains"]] },
    { id: "phishdestroy_domain", name: "Phishing links", desc: "Checks links against the PhishDestroy threat list.",
      params: [["phishdestroy_action", "Action (alert or delete)"]] },
    { id: "scam_image", name: "Scam images", desc: "Matches known scam pictures by image hash.", flag: "moderation_scam_images_enabled",
      params: [["moderation_scam_images_enforce", "Delete matches (off = alert only)"], ["moderation_scam_image_distance", "Match distance (lower = stricter)"]] },
    { id: "discord_invite", name: "Invite links", desc: "Deletes Discord server invites.", flag: "moderation_block_discord_invites", warns: true, params: [] },
    { id: "suspicious_shortener", name: "Link shorteners", desc: "Alerts staff about shortened links (bit.ly and similar).", params: [] },
    { id: "link_burst", name: "Link spam", desc: "Times out users posting many links quickly.",
      params: [["moderation_link_burst_count", "Links"], ["moderation_link_burst_window_seconds", "Window (seconds)"]] },
    { id: "banned_word", name: "Banned words", desc: "Deletes messages with banned words. * works as a wildcard.", warns: true,
      params: [["moderation_banned_words", "Words (comma-separated)"]] },
    { id: "mass_mention", name: "Mass mentions", desc: "Deletes messages pinging many users or roles.", warns: true,
      params: [["moderation_mass_mention_limit", "Mention limit"]] },
    { id: "everyone_ping", name: "@everyone / @here", desc: "Deletes @everyone and @here from members who can't use them.",
      flag: "moderation_block_everyone_ping", params: [] },
    { id: "excessive_caps", name: "Excessive caps", desc: "Deletes mostly-uppercase messages.",
      params: [["moderation_caps_percent", "Caps %"], ["moderation_caps_min_length", "Minimum letters"]] },
    { id: "excessive_emoji", name: "Excessive emoji", desc: "Deletes messages with too many emoji.", params: [["moderation_emoji_limit", "Emoji limit"]] },
    { id: "wall_of_text", name: "Wall of text", desc: "Deletes messages with too many line breaks (code blocks don't count).",
      params: [["moderation_newline_limit", "Line limit"]] },
    { id: "zalgo", name: "Zalgo text", desc: "Deletes glitchy zalgo text.", flag: "moderation_zalgo_enabled", params: [] },
    { id: "duplicate_spam", name: "Duplicate messages", desc: "Deletes the same message sent over and over.",
      params: [["moderation_duplicate_count", "Copies"], ["moderation_duplicate_window_seconds", "Window (seconds)"]] },
    { id: "fast_messages", name: "Fast messages", desc: "Deletes messages sent too quickly in a row.",
      params: [["moderation_fast_message_count", "Messages"], ["moderation_fast_message_window_seconds", "Window (seconds)"]] },
    { id: "image_burst", name: "Image spam", desc: "Times out users flooding images across messages or channels.",
      params: [["moderation_image_burst_count", "Images"], ["moderation_image_burst_window_seconds", "Window (seconds)"],
        ["moderation_image_burst_min_messages", "Minimum messages"], ["moderation_image_burst_timeout_seconds", "Timeout (seconds)"]] },
  ];

  const asText = (value) => (Array.isArray(value) ? value.join(", ") : value === null || value === undefined ? "" : String(value));
  const put = (name, value) => api(`/api/settings/${encodeURIComponent(name)}`, { method: "PUT", body: { value } });

  function toggle(checked, label, locked, onChange) {
    const input = h("input", { type: "checkbox", class: "switch", role: "switch", checked, disabled: locked, "aria-label": label });
    input.addEventListener("change", () => onChange(input));
    return input;
  }

  function paramInput(setting) {
    if (setting.kind === "bool") return h("input", { type: "checkbox", checked: setting.value });
    if (setting.kind.endsWith("_list")) return h("textarea", { rows: "2", value: asText(setting.value) });
    const number = setting.kind === "int" || setting.kind === "float";
    return h("input", { type: number ? "number" : "text", min: number ? "0" : null, value: asText(setting.value) });
  }

  function filterCard(filter, byName, reload) {
    const locked = !can("settings.edit");
    const disabled = byName[DISABLED].value;
    const flagOn = !filter.flag || byName[filter.flag].value;
    const on = flagOn && !disabled.includes(filter.id);

    const sw = toggle(on, t(filter.name), locked, (input) => run(input, async () => {
      const rest = disabled.filter((id) => id !== filter.id);
      await put(DISABLED, (input.checked ? rest : [...rest, filter.id]).join(","));
      if (input.checked && !flagOn) await put(filter.flag, "true");
      await reload();
    }, t(input.checked ? "{name} on" : "{name} off", { name: t(filter.name) })));

    const inputs = filter.params.map(([name, label]) => {
      const setting = byName[name];
      const input = paramInput(setting);
      input.disabled = locked;
      const raw = () => (setting.kind === "bool" ? String(input.checked) : input.value);
      return { name, label, input, raw, before: raw(), counts: setting.kind === "int" && /(count|limit|percent)$/.test(name) };
    });
    const save = h("button", { class: "btn small primary", type: "button", disabled: locked }, t("Save"));
    save.addEventListener("click", () => run(save, async () => {
      for (const p of inputs.filter((p) => p.raw() !== p.before)) await put(p.name, p.raw());
      await reload();
    }, t("Saved {name}", { name: t(filter.name) })));

    return h("div", { class: on ? "card filter" : "card filter off" },
      h("div", { class: "row spread" }, h("h3", {}, t(filter.name)), sw),
      h("p", { class: "muted small" }, t(filter.desc), " ", filter.warns ? badge(t("Warns"), "warn") : null),
      inputs.length
        ? h("div", { class: "stack" },
          inputs.map((p) => h("div", {}, field(t(p.label), p.input), p.counts ? h("div", { class: "muted small" }, t("0 turns this check off.")) : null)),
          h("div", { class: "row end" }, save))
        : h("p", { class: "muted small" }, t("No settings, just on/off.")));
  }

  Panel.page({
    id: "automod",
    title: t("Automod"),
    perm: "settings.view",
    group: "Moderation",
    async render(view) {
      const search = h("input", { type: "search", placeholder: t("Search filters"), "aria-label": t("Search filters") });
      const master = h("div");
      const grid = h("div", { class: "filters" });
      let byName = {};

      const draw = () => {
        const q = search.value.trim().toLowerCase();
        grid.replaceChildren(...FILTERS
          .filter((f) => !q || t(f.name).toLowerCase().includes(q) || t(f.desc).toLowerCase().includes(q))
          .map((f) => filterCard(f, byName, reload)));
      };
      const reload = async () => {
        byName = Object.fromEntries((await api("/api/settings")).settings.map((s) => [s.name, s]));
        const enabled = byName.moderation_enabled.value;
        master.replaceChildren(h("label", { class: "row" },
          toggle(enabled, t("Enable automod"), !can("settings.edit"), (input) => run(input, async () => {
            await put("moderation_enabled", String(input.checked));
            await reload();
          }, t(input.checked ? "Automod on" : "Automod off"))),
          t("Enable automod")));
        draw();
      };

      search.addEventListener("input", draw);
      view.append(
        h("div", { class: "row spread" }, h("h1", {}, t("Automod")), master),
        h("p", { class: "muted" }, t("Each filter can be switched off on its own; its thresholds are kept for when you turn it back on. Staff, exempt roles and excluded channels are never checked.")),
        h("div", { class: "card" }, search),
        grid);
      await reload();
    },
  });
})();
