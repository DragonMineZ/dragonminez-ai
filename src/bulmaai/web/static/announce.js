"use strict";

// Announce page: compose one Components V2 card with the shared block editor (cardeditor.js), then send,
// schedule, save as a draft, or edit an existing bot message.

(() => {
  const { h, api, run, badge, field, dialog, channelSelect, channelName, toast, t, i18n } = Panel;

  function channelLabel(id) {
    const el = h("span", {}, `#${id}`);
    Panel.channelName(id).then((name) => { el.textContent = name; }).catch(() => {});
    return el;
  }

  function rolesPicker(onChange) {
    const box = h("div", { class: "role-picker muted small" }, t("Loading roles…"));
    let checkboxes = new Map();
    let pending = null;
    function apply(ids) { for (const [id, cb] of checkboxes) cb.checked = ids.includes(id); }
    Panel.guild().then((g) => {
      checkboxes = new Map();
      const items = g.roles.map((r) => {
        const cb = h("input", { type: "checkbox" });
        cb.addEventListener("change", onChange);
        checkboxes.set(r.id, cb);
        return h("label", {}, cb, `@${r.name}`);
      });
      box.replaceChildren(...(items.length ? items : [h("span", { class: "muted small" }, t("No roles in this server."))]));
      if (pending) { apply(pending); pending = null; }
    }).catch(() => { box.replaceChildren(h("span", { class: "error small" }, t("Couldn't load roles."))); });
    return {
      el: box,
      get: () => [...checkboxes.entries()].filter(([, cb]) => cb.checked).map(([id]) => id),
      setSelected(ids) { if (checkboxes.size) apply(ids); else pending = ids; },
    };
  }

  function toLocalInputValue(iso) {
    const d = new Date(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // "https://discord.com/channels/<guild>/<channel>/<message>" or a bare message ID.
  function parseMessageRef(text, fallbackChannel) {
    const value = text.trim();
    const link = /channels\/(?:\d+|@me)\/(\d+)\/(\d+)/.exec(value);
    if (link) return { channel_id: link[1], message_id: link[2] };
    if (/^\d{15,22}$/.test(value)) return { channel_id: fallbackChannel, message_id: value };
    return null;
  }

  const jumpUrl = (channelId, messageId) => `https://discord.com/channels/${(Panel.me() || {}).guild?.id || "@me"}/${channelId}/${messageId}`;

  // ---------- page ---------------------------------------------------------------------------------

  Panel.page({
    id: "announce",
    title: "Announce",
    perm: "announce.send",
    group: "Community",
    async render(view) {
      let composerMode = null; // null | {kind:"edit", channel_id, message_id, jump_url} | {kind:"schedule", id}
      let draftId = null;

      const channel = channelSelect(null, { types: ["text", "news"], includeNone: true });
      const scheduleAt = h("input", { type: "datetime-local" });
      const everyone = h("input", { type: "checkbox" });
      const translateEs = h("input", { type: "checkbox" });
      const translatePt = h("input", { type: "checkbox" });
      const result = h("div");
      const modeRow = h("div", { class: "row" });
      const send = h("button", { class: "btn primary", type: "button" }, t("Send"));

      const editor = Panel.cardEditor({});
      const roles = rolesPicker(() => {});

      function currentPayload() {
        return {
          card: editor.get(),
          mention_roles: roles.get(),
          mention_everyone: everyone.checked,
          translate: { es: translateEs.checked, pt: translatePt.checked },
        };
      }

      // Local mirror of the backend rules: stops the obvious mistakes before a round trip.
      function blockedByIssues() {
        const problems = editor.issues();
        if (!problems.length) return false;
        toast(problems[0], true);
        return true;
      }

      function pingsWarning(payload) {
        const pinging = payload.mention_everyone || payload.mention_roles.length;
        return pinging
          ? h("p", { class: "error" }, t("This will ping people (selected roles and/or @everyone/@here)."))
          : h("p", { class: "muted" }, t("No pings: mentions render but notify nobody unless you picked roles or checked @everyone/@here."));
      }

      function applyPayload(payload) {
        editor.set(payload.card || null);
        everyone.checked = Boolean(payload.mention_everyone);
        translateEs.checked = Boolean(payload.translate && payload.translate.es);
        translatePt.checked = Boolean(payload.translate && payload.translate.pt);
        roles.setSelected(payload.mention_roles || []);
      }

      const stopBtn = h("button", { class: "btn small ghost", type: "button", onclick: () => resetComposer() }, t("Cancel / start new"));

      function updateSendLabel() {
        if (composerMode && composerMode.kind === "edit") send.textContent = t("Save edit");
        else if (composerMode && composerMode.kind === "schedule") send.textContent = t("Save schedule");
        else send.textContent = scheduleAt.value ? t("Schedule send") : t("Send");
      }

      function drawMode() {
        const isEdit = composerMode && composerMode.kind === "edit";
        const isSchedule = composerMode && composerMode.kind === "schedule";
        channel.disabled = isEdit;
        scheduleWrap.hidden = isEdit;
        translateWrap.hidden = isEdit;
        pingsWrap.hidden = isEdit;
        updateSendLabel();
        modeRow.replaceChildren(...(isEdit ? [
          badge(t("editing"), "warn"),
          h("a", { href: composerMode.jump_url, target: "_blank", rel: "noopener noreferrer" }, t("message {id}", { id: composerMode.message_id })),
          stopBtn,
        ] : isSchedule ? [badge(t("editing scheduled send"), "warn"), stopBtn] : []));
      }

      function resetComposer() {
        composerMode = null;
        draftId = null;
        channel.value = "";
        scheduleAt.value = "";
        everyone.checked = false;
        translateEs.checked = false;
        translatePt.checked = false;
        editor.set(null);
        roles.setSelected([]);
        result.replaceChildren();
        drawMode();
      }

      async function doCreate() {
        if (!channel.value) { toast(t("Pick a channel first."), true); return; }
        if (blockedByIssues()) return;
        const payload = { ...currentPayload(), channel_id: channel.value, send_at: scheduleAt.value ? new Date(scheduleAt.value).toISOString() : null };
        const where = await channelName(channel.value);
        const scheduling = Boolean(payload.send_at);
        const ok = await dialog(scheduling ? t("Schedule this message?") : t("Send this message?"), [
          h("p", {}, scheduling
            ? t("Post as the bot in {where} at {time}.", { where, time: new Date(payload.send_at).toLocaleString(Panel.locale()) })
            : t("Post as the bot in {where}.", { where })),
          pingsWarning(payload),
        ], { confirmLabel: scheduling ? t("Schedule") : t("Send") });
        if (!ok) return;
        const sent = await run(send, () => api("/api/announce", { method: "POST", body: payload }), scheduling ? t("Scheduled") : t("Message sent"));
        if (!sent) return;
        if (draftId) { await api(`/api/announce/drafts/${draftId}`, { method: "DELETE" }).catch(() => {}); draftId = null; refreshDrafts().catch(() => {}); }
        editor.markClean();
        result.replaceChildren(scheduling
          ? h("p", {}, t("Scheduled for {time}.", { time: new Date(sent.send_at).toLocaleString(Panel.locale()) }))
          : h("p", {},
              h("span", {}, t("Sent: ")),
              h("a", { href: sent.jump_url, target: "_blank", rel: "noopener noreferrer" }, sent.jump_url),
              sent.translation_errors && Object.keys(sent.translation_errors).length
                ? h("div", { class: "error small" }, t("Translation failed for: {langs}", { langs: Object.keys(sent.translation_errors).join(", ") }))
                : null));
        await Promise.all([refreshScheduled(), refreshHistory()]);
      }

      async function doSaveEdit() {
        if (blockedByIssues()) return;
        const where = await channelName(composerMode.channel_id);
        const ok = await dialog(t("Save changes to this message?"), [
          h("p", {}, t("Edit the bot's message {id} in {where}.", { id: composerMode.message_id, where })),
          h("p", { class: "muted" }, t("Editing never pings anyone again.")),
        ], { confirmLabel: t("Save edit") });
        if (!ok) return;
        const edited = await run(send, () => api(`/api/announce/${composerMode.channel_id}/${composerMode.message_id}`, { method: "PATCH", body: { card: editor.get() } }), t("Message edited"));
        if (!edited) return;
        editor.markClean();
        result.replaceChildren(h("p", {}, h("span", {}, t("Edited: ")), h("a", { href: composerMode.jump_url, target: "_blank", rel: "noopener noreferrer" }, composerMode.jump_url)));
        await refreshHistory();
      }

      async function doSaveSchedule() {
        if (!channel.value) { toast(t("Pick a channel first."), true); return; }
        if (!scheduleAt.value) { toast(t("Pick a time first."), true); return; }
        if (blockedByIssues()) return;
        const payload = { ...currentPayload(), channel_id: channel.value, send_at: new Date(scheduleAt.value).toISOString() };
        const row = await run(send, () => api(`/api/announce/scheduled/${composerMode.id}`, { method: "PUT", body: payload }), t("Schedule updated"));
        if (!row) return;
        resetComposer();
        await refreshScheduled();
      }

      send.addEventListener("click", () => {
        if (composerMode && composerMode.kind === "edit") return doSaveEdit();
        if (composerMode && composerMode.kind === "schedule") return doSaveSchedule();
        return doCreate();
      });

      const saveDraftBtn = h("button", { class: "btn ghost", type: "button" }, t("Save draft"));
      saveDraftBtn.addEventListener("click", () => run(saveDraftBtn, async () => {
        const body = { ...currentPayload(), channel_id: channel.value || null };
        const row = draftId
          ? await api(`/api/announce/drafts/${draftId}`, { method: "PUT", body })
          : await api("/api/announce/drafts", { method: "POST", body });
        draftId = row.id;
        editor.markClean();
        await refreshDrafts();
      }, t("Draft saved")));

      // Asks before throwing away unsaved work in the editor.
      async function confirmReplace() {
        if (!editor.dirty()) return true;
        return dialog(t("Replace the current message?"), h("p", {}, t("The editor has unsaved changes that will be lost.")),
          { confirmLabel: t("Replace"), danger: true });
      }

      // ---------- start from a template ----------

      const templateSelect = h("select", {}, h("option", { value: "" }, t("— choose a template —")));
      const templateCards = new Map();
      api("/api/announce/templates").then(({ templates }) => {
        for (const tpl of templates) {
          templateCards.set(tpl.id, tpl);
          templateSelect.append(h("option", { value: tpl.id }, tpl.name));
        }
      }).catch((error) => toast(error.message, true));
      const useTemplate = h("button", { class: "btn small", type: "button" }, t("Use template"));
      useTemplate.addEventListener("click", async () => {
        const tpl = templateCards.get(templateSelect.value);
        if (!tpl) { toast(t("Pick a template first."), true); return; }
        if (!(await confirmReplace())) return;
        editor.set(tpl.languages && tpl.languages.en);
        editor.markClean();
        editor.setLanguageButtons(false);
        toast(t("Template loaded: {name}", { name: tpl.name }));
      });

      // ---------- load an existing bot message to edit ad hoc ----------

      const ref = h("input", { type: "text", size: "50", placeholder: t("Message link (or ID of a message in the selected channel)") });
      const load = h("button", { class: "btn small", type: "button" }, t("Load"));
      load.addEventListener("click", () => run(load, async () => {
        const parsed = parseMessageRef(ref.value, channel.value);
        if (!parsed || !parsed.channel_id) throw new Error(t("Paste a message link, or pick the channel and paste the message ID."));
        if (!(await confirmReplace())) return;
        const query = new URLSearchParams(parsed);
        const message = await api(`/api/announce/message?${query}`);
        composerMode = { kind: "edit", channel_id: message.channel_id, message_id: message.message_id, jump_url: jumpUrl(message.channel_id, message.message_id) };
        draftId = null;
        channel.value = message.channel_id;
        editor.set(message.card);
        everyone.checked = false;
        roles.setSelected([]);
        drawMode();
        result.replaceChildren();
        toast(t("Message loaded"));
      }));

      scheduleAt.addEventListener("input", updateSendLabel);

      const translateWrap = h("div", { class: "row" },
        h("label", { class: "row check" }, translateEs, t("Also post a Spanish translation")),
        h("label", { class: "row check" }, translatePt, t("Also post a Portuguese translation")));
      const scheduleWrap = field(t("Send at (leave blank to send immediately)"), scheduleAt);
      const pingsWrap = h("div", { class: "stack" },
        h("h3", {}, t("Pings")),
        h("p", { class: "muted small" }, t("Mentions must be written in the text blocks (for example <@&ROLE_ID> or @everyone). The options below only decide which of them actually notify people; nothing else ever pings.")),
        h("div", {}, h("div", { class: "small muted" }, t("Roles that can ping when mentioned in the text")), roles.el),
        h("label", { class: "row check" }, everyone, t("Allow @everyone/@here to ping (only if the text contains it)")));

      // ---------- scheduled / history / drafts lists ----------

      const panels = { scheduled: h("div"), history: h("div"), drafts: h("div") };
      const anchor = h("div", { id: "announce-compose-anchor" });
      const toComposer = () => anchor.scrollIntoView({ block: "start" });

      async function loadScheduledIntoComposer(row) {
        if (!(await confirmReplace())) return;
        composerMode = { kind: "schedule", id: row.id };
        draftId = null;
        channel.value = row.channel_id;
        applyPayload(row.payload);
        scheduleAt.value = toLocalInputValue(row.send_at);
        drawMode();
        toComposer();
      }

      async function refreshScheduled() {
        const { items } = await api("/api/announce/scheduled");
        panels.scheduled.replaceChildren(items.length
          ? h("div", { class: "stack" }, items.map((row) => {
              const editBtn = h("button", { class: "btn small", type: "button" }, t("Load to edit"));
              editBtn.addEventListener("click", () => loadScheduledIntoComposer(row));
              const sendNowBtn = h("button", { class: "btn small", type: "button" }, t("Send now"));
              sendNowBtn.addEventListener("click", () => run(sendNowBtn, async () => {
                await api(`/api/announce/scheduled/${row.id}/send-now`, { method: "POST" });
                await Promise.all([refreshScheduled(), refreshHistory()]);
              }, t("Sent")));
              const cancelBtn = h("button", { class: "btn small danger", type: "button" }, t("Cancel"));
              cancelBtn.addEventListener("click", () => run(cancelBtn, async () => {
                const ok = await dialog(t("Cancel this scheduled announcement?"), h("p", {}, t("This can't be undone.")), { confirmLabel: t("Cancel send"), danger: true });
                if (!ok) return;
                await api(`/api/announce/scheduled/${row.id}/cancel`, { method: "POST" });
                await refreshScheduled();
              }));
              return h("div", { class: "card row spread" },
                h("div", {},
                  h("div", {}, Panel.cardSummary(row.payload.card)),
                  h("div", { class: "muted small" }, channelLabel(row.channel_id), " · ", new Date(row.send_at).toLocaleString(Panel.locale())),
                  row.status === "failed" ? h("div", { class: "error small" }, row.error) : null),
                h("div", { class: "row" }, editBtn, sendNowBtn, cancelBtn));
            }))
          : h("div", { class: "empty" }, t("Nothing scheduled.")));
      }

      async function refreshHistory() {
        const { items } = await api("/api/announce/history");
        panels.history.replaceChildren(items.length
          ? h("div", { class: "stack" }, items.map((row) => {
              const messageIds = row.message_ids && row.message_ids.main;
              const canEdit = Boolean(messageIds && messageIds.length && row.payload && row.payload.card);
              const editBtn = h("button", { class: "btn small", type: "button", disabled: !canEdit }, t("Load to edit"));
              editBtn.addEventListener("click", async () => {
                if (!(await confirmReplace())) return;
                composerMode = { kind: "edit", channel_id: row.channel_id, message_id: messageIds[0], jump_url: jumpUrl(row.channel_id, messageIds[0]) };
                draftId = null;
                channel.value = row.channel_id;
                applyPayload(row.payload);
                drawMode();
                toComposer();
              });
              const statusKind = row.status === "sent" ? "ok" : row.status === "failed" ? "danger" : "";
              return h("div", { class: "card row spread" },
                h("div", {},
                  h("div", {}, Panel.cardSummary(row.payload && row.payload.card)),
                  h("div", { class: "row muted small" }, badge(t(row.status), statusKind), channelLabel(row.channel_id), Panel.time(row.sent_at || row.updated_at)),
                  row.error ? h("div", { class: "error small" }, row.error) : null,
                  row.payload && row.payload.translation_errors ? h("div", { class: "error small" }, t("Translation failed for: {langs}", { langs: Object.keys(row.payload.translation_errors).join(", ") })) : null),
                h("div", { class: "row" }, editBtn));
            }))
          : h("div", { class: "empty" }, t("No announcements sent from the panel yet.")));
      }

      async function refreshDrafts() {
        const { items } = await api("/api/announce/drafts");
        panels.drafts.replaceChildren(items.length
          ? h("div", { class: "stack" }, items.map((row) => {
              const loadBtn = h("button", { class: "btn small", type: "button" }, t("Load"));
              loadBtn.addEventListener("click", async () => {
                if (!(await confirmReplace())) return;
                composerMode = null;
                draftId = row.id;
                channel.value = row.channel_id || "";
                applyPayload(row.payload);
                drawMode();
                toComposer();
              });
              const deleteBtn = h("button", { class: "btn small danger", type: "button" }, t("Delete"));
              deleteBtn.addEventListener("click", () => run(deleteBtn, async () => {
                await api(`/api/announce/drafts/${row.id}`, { method: "DELETE" });
                if (draftId === row.id) draftId = null;
                await refreshDrafts();
              }, t("Draft deleted")));
              return h("div", { class: "card row spread" },
                h("div", {}, Panel.cardSummary(row.payload && row.payload.card),
                  h("div", { class: "muted small" }, Panel.time(row.updated_at))),
                h("div", { class: "row" }, loadBtn, deleteBtn));
            }))
          : h("div", { class: "empty" }, t("No saved drafts.")));
      }

      view.append(
        h("h1", {}, t("Announce")),
        h("p", { class: "muted" }, t("Build a message as the bot out of blocks (text, images, buttons…), then send it now, schedule it, or save a draft.")),
        anchor,
        h("details", { class: "card" },
          h("summary", { class: "details-summary" }, t("Load an existing bot message to edit")),
          h("div", { class: "stack", style: "margin-top:12px" },
            h("div", { class: "row" }, ref, load),
            h("p", { class: "muted small" }, t("Only messages built with this editor (Components V2 cards) can be loaded.")))),
        modeRow,
        h("div", { class: "card stack" },
          h("div", { class: "grid compose-top" },
            field(t("Channel"), channel),
            h("div", {}, h("label", {}, t("Start from template")), h("div", { class: "row" }, templateSelect, useTemplate))),
          editor.el,
          pingsWrap,
          translateWrap,
          scheduleWrap,
          h("div", { class: "row" }, send, saveDraftBtn),
          result),
        h("h2", {}, t("Scheduled")),
        panels.scheduled,
        h("h2", {}, t("History")),
        panels.history,
        h("h2", {}, t("Drafts")),
        panels.drafts);

      drawMode();
      await Promise.all([refreshScheduled(), refreshHistory(), refreshDrafts()]);
    },
  });

  i18n({
    "Loading roles…": "Cargando roles…",
    "No roles in this server.": "No hay roles en este servidor.",
    "Couldn't load roles.": "No se pudieron cargar los roles.",
    "Send": "Enviar",
    "Save edit": "Guardar edición",
    "Save schedule": "Guardar programación",
    "Schedule send": "Programar envío",
    "editing": "editando",
    "editing scheduled send": "editando envío programado",
    "message {id}": "mensaje {id}",
    "Cancel / start new": "Cancelar / empezar de nuevo",
    "Pick a channel first.": "Elige un canal primero.",
    "Pick a time first.": "Elige una hora primero.",
    "This will ping people (selected roles and/or @everyone/@here).": "Esto notificará a personas (roles seleccionados y/o @everyone/@here).",
    "No pings: mentions render but notify nobody unless you picked roles or checked @everyone/@here.":
      "Sin notificaciones: las menciones se muestran pero no avisan a nadie salvo que hayas elegido roles o marcado @everyone/@here.",
    "Schedule this message?": "¿Programar este mensaje?",
    "Send this message?": "¿Enviar este mensaje?",
    "Post as the bot in {where} at {time}.": "Publicar como el bot en {where} el {time}.",
    "Post as the bot in {where}.": "Publicar como el bot en {where}.",
    "Schedule": "Programar",
    "Scheduled": "Programados",
    "Message sent": "Mensaje enviado",
    "Scheduled for {time}.": "Programado para el {time}.",
    "Sent: ": "Enviado: ",
    "Translation failed for: {langs}": "Falló la traducción para: {langs}",
    "Save changes to this message?": "¿Guardar cambios en este mensaje?",
    "Edit the bot's message {id} in {where}.": "Edita el mensaje {id} del bot en {where}.",
    "Editing never pings anyone again.": "Editar nunca vuelve a notificar a nadie.",
    "Message edited": "Mensaje editado",
    "Edited: ": "Editado: ",
    "Schedule updated": "Programación actualizada",
    "Draft saved": "Borrador guardado",
    "Replace the current message?": "¿Reemplazar el mensaje actual?",
    "The editor has unsaved changes that will be lost.": "El editor tiene cambios sin guardar que se perderán.",
    "Replace": "Reemplazar",
    "— choose a template —": "— elige una plantilla —",
    "Use template": "Usar plantilla",
    "Pick a template first.": "Elige una plantilla primero.",
    "Template loaded: {name}": "Plantilla cargada: {name}",
    "Start from template": "Empezar desde una plantilla",
    "Message link (or ID of a message in the selected channel)": "Enlace del mensaje (o ID de un mensaje en el canal elegido)",
    "Paste a message link, or pick the channel and paste the message ID.": "Pega un enlace de mensaje, o elige el canal y pega el ID del mensaje.",
    "Only messages built with this editor (Components V2 cards) can be loaded.": "Solo se pueden cargar mensajes creados con este editor (tarjetas Components V2).",
    "Load": "Cargar",
    "Message loaded": "Mensaje cargado",
    "Also post a Spanish translation": "También publicar una traducción al español",
    "Also post a Portuguese translation": "También publicar una traducción al portugués",
    "Send at (leave blank to send immediately)": "Enviar el (deja vacío para enviar de inmediato)",
    "Announce": "Anunciar",
    "Build a message as the bot out of blocks (text, images, buttons…), then send it now, schedule it, or save a draft.":
      "Arma un mensaje como el bot con bloques (texto, imágenes, botones…), luego envíalo ya, prográmalo o guarda un borrador.",
    "Load an existing bot message to edit": "Cargar un mensaje existente del bot para editarlo",
    "Channel": "Canal",
    "Pings": "Menciones",
    "Mentions must be written in the text blocks (for example <@&ROLE_ID> or @everyone). The options below only decide which of them actually notify people; nothing else ever pings.":
      "Las menciones deben escribirse en los bloques de texto (por ejemplo <@&ID_DEL_ROL> o @everyone). Las opciones de abajo solo deciden cuáles notifican realmente; nada más notifica jamás.",
    "Roles that can ping when mentioned in the text": "Roles que pueden notificar si se mencionan en el texto",
    "Allow @everyone/@here to ping (only if the text contains it)": "Permitir que @everyone/@here notifique (solo si el texto lo contiene)",
    "Save draft": "Guardar borrador",
    "History": "Historial",
    "Drafts": "Borradores",
    "Nothing scheduled.": "No hay nada programado.",
    "Load to edit": "Cargar para editar",
    "Send now": "Enviar ahora",
    "Sent": "Enviado",
    "Cancel": "Cancelar",
    "Cancel this scheduled announcement?": "¿Cancelar este anuncio programado?",
    "This can't be undone.": "Esto no se puede deshacer.",
    "Cancel send": "Cancelar envío",
    "No announcements sent from the panel yet.": "Aún no se ha enviado ningún anuncio desde el panel.",
    "draft": "borrador",
    "scheduled": "programado",
    "sending": "enviando",
    "sent": "enviado",
    "failed": "fallido",
    "cancelled": "cancelado",
    "No saved drafts.": "No hay borradores guardados.",
    "Delete": "Eliminar",
    "Draft deleted": "Borrador eliminado",
  });
})();
