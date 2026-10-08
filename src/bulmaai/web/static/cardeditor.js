"use strict";

// Shared Components V2 card tooling, used by the Announce composer (announce.js) and the Templates page
// (templates.js). Exposes, on the global Panel object:
//
//   Panel.cardPreview(card, {languageButtons})  -> Node   Discord-style dark-theme render of one card
//   Panel.cardEditor({onChange, serverValidate}) -> editor  block editor + live preview + counters
//        editor.el                  the whole workbench (editor column + sticky preview column)
//        editor.get()               -> card JSON ({accent_color, blocks}) as the API expects it
//        editor.set(card)           load a card (null/undefined = a fresh card with one empty text block)
//        editor.setLanguageButtons(bool)  preview + component count include the English/Español/Português row
//        editor.dirty() / editor.markClean()  unsaved-work tracking (set() marks clean)
//        editor.issues()            -> [string] local validation problems (mirrors the backend rules)
//        editor.stats()             -> {chars, components}
//   Panel.cardSummary(card, max)  -> one-line text for lists
//   Panel.CARD_LIMITS             -> {chars, components}
//
// Never use innerHTML with data; everything goes through h().

(() => {
  const { h, api, t, i18n } = Panel;
  const LIMITS = { chars: 4000, components: 40, galleryImages: 10, buttons: 5, buttonLabel: 80 };
  const BOT_AVATAR = "https://cdn.discordapp.com/embed/avatars/0.png";
  const BOT_NAME = "BulmaAI";
  const DEFAULT_ACCENT = "#F39C12";
  const LANG_ROW = [["English", "🇺🇸"], ["Español", "🇪🇸"], ["Português", "🇧🇷"]];

  const isHttp = (u) => /^https?:\/\/[^\s]+$/i.test(String(u || "").trim());
  const len = (s) => [...String(s || "")].length;
  const safeUrl = (u) => (isHttp(u) ? String(u).trim() : null);

  // ---------- mention pills (names come from Panel.guild(), cached) --------------------------------

  const userCache = new Map();
  function userLabel(id) {
    if (userCache.has(id)) return Promise.resolve(userCache.get(id));
    return api(`/api/users/${id}`).then((d) => {
      const label = `@${d.user.display_name}`;
      userCache.set(id, label);
      return label;
    }).catch(() => `@${id}`);
  }

  function mentionPill(kind, id) {
    const el = h("span", { class: "mention" }, kind === "channel" ? `#${id}` : `@${id}`);
    const resolved = kind === "role" ? Panel.roleName(id) : kind === "channel" ? Panel.channelName(id) : userLabel(id);
    Promise.resolve(resolved).then((name) => { if (name) el.textContent = name; }).catch(() => {});
    return el;
  }

  // ---------- Discord markdown -> DOM ---------------------------------------------------------------

  const INLINE_PATTERNS = [
    { re: /^`([^`]+)`/, fn: (m) => h("code", {}, m[1]) },
    { re: /^\|\|([\s\S]+?)\|\|/, fn: (m) => h("span", { class: "d-spoiler", onclick: (e) => e.currentTarget.classList.toggle("revealed") }, parseInline(m[1])) },
    { re: /^\*\*([\s\S]+?)\*\*/, fn: (m) => h("strong", {}, parseInline(m[1])) },
    { re: /^__([\s\S]+?)__/, fn: (m) => h("u", {}, parseInline(m[1])) },
    { re: /^~~([\s\S]+?)~~/, fn: (m) => h("s", {}, parseInline(m[1])) },
    { re: /^\*([^\s*][\s\S]*?)\*/, fn: (m) => h("em", {}, parseInline(m[1])) },
    { re: /^_([^\s_][\s\S]*?)_(?!\w)/, guard: (prev) => !/\w/.test(prev), fn: (m) => h("em", {}, parseInline(m[1])) },
    { re: /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/, fn: (m) => h("a", { href: m[2], target: "_blank", rel: "noopener noreferrer" }, parseInline(m[1])) },
    { re: /^<a?:(\w+):(\d+)>/, fn: (m) => h("img", { class: "d-emoji", src: `https://cdn.discordapp.com/emojis/${m[2]}.png`, alt: `:${m[1]}:`, title: `:${m[1]}:` }) },
    { re: /^<@!?(\d+)>/, fn: (m) => mentionPill("user", m[1]) },
    { re: /^<@&(\d+)>/, fn: (m) => mentionPill("role", m[1]) },
    { re: /^<#(\d+)>/, fn: (m) => mentionPill("channel", m[1]) },
    { re: /^@(everyone|here)\b/, fn: (m) => h("span", { class: "mention" }, `@${m[1]}`) },
    { re: /^https?:\/\/[^\s<]+/, fn: (m) => h("a", { href: m[0], target: "_blank", rel: "noopener noreferrer" }, m[0]) },
  ];

  function parseInline(text) {
    const nodes = [];
    let buffer = "";
    let prev = "";
    let rest = String(text);
    const flush = () => { if (buffer) { nodes.push(document.createTextNode(buffer)); buffer = ""; } };
    while (rest) {
      let hit = null;
      for (const p of INLINE_PATTERNS) {
        if (p.guard && !p.guard(prev)) continue;
        const m = p.re.exec(rest);
        if (m) { hit = { node: p.fn(m), length: m[0].length }; break; }
      }
      if (hit) { flush(); nodes.push(hit.node); rest = rest.slice(hit.length); prev = ""; }
      else { buffer += rest[0]; prev = rest[0]; rest = rest.slice(1); }
    }
    flush();
    return nodes;
  }

  const RE_HEADING = /^(#{1,3})\s+(.*)$/;
  const RE_SUBTEXT = /^-#\s+(.*)$/;
  const RE_BULLET = /^[-*]\s+(.*)$/;
  const RE_ORDERED = /^(\d+)\.\s+(.*)$/;
  const isBlockStart = (line) => line.startsWith("```") || RE_HEADING.test(line) || RE_SUBTEXT.test(line)
    || line.startsWith("> ") || RE_BULLET.test(line) || RE_ORDERED.test(line);

  function joinLines(lines) {
    const nodes = [];
    lines.forEach((line, i) => {
      if (i > 0) nodes.push(h("br"));
      nodes.push(...parseInline(line));
    });
    return nodes;
  }

  function renderContent(text) {
    const box = h("div", { class: "d-content" });
    const lines = String(text).split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      let m;
      if (line.startsWith("```")) {
        let j = i + 1;
        const code = [];
        while (j < lines.length && !lines[j].startsWith("```")) { code.push(lines[j]); j++; }
        box.append(h("pre", { class: "d-code-block" }, h("code", {}, code.join("\n"))));
        i = j + 1;
      } else if ((m = RE_SUBTEXT.exec(line))) {
        box.append(h("div", { class: "d-subtext" }, parseInline(m[1])));
        i++;
      } else if ((m = RE_HEADING.exec(line))) {
        box.append(h(`h${m[1].length}`, { class: "d-heading" }, parseInline(m[2])));
        i++;
      } else if (line.startsWith("> ")) {
        const quote = [];
        while (i < lines.length && lines[i].startsWith("> ")) { quote.push(lines[i].slice(2)); i++; }
        box.append(h("blockquote", { class: "d-quote" }, joinLines(quote)));
      } else if (RE_BULLET.test(line)) {
        const items = [];
        while (i < lines.length && (m = RE_BULLET.exec(lines[i]))) { items.push(m[1]); i++; }
        box.append(h("ul", { class: "d-list" }, items.map((it) => h("li", {}, parseInline(it)))));
      } else if (RE_ORDERED.test(line)) {
        const start = Number(RE_ORDERED.exec(line)[1]);
        const items = [];
        while (i < lines.length && (m = RE_ORDERED.exec(lines[i]))) { items.push(m[2]); i++; }
        box.append(h("ol", { class: "d-list", start: String(start) }, items.map((it) => h("li", {}, parseInline(it)))));
      } else if (line.trim() === "") {
        if (box.lastChild && !box.lastChild.classList.contains("d-gap") && i < lines.length - 1) box.append(h("div", { class: "d-gap" }));
        i++;
      } else {
        const para = [];
        while (i < lines.length && lines[i].trim() !== "" && !isBlockStart(lines[i])) { para.push(lines[i]); i++; }
        box.append(h("p", { class: "d-para" }, joinLines(para)));
      }
    }
    return box;
  }

  // ---------- card model helpers --------------------------------------------------------------------

  // Counting rules from the API contract: container=1, text=1, section=3, separator=1, gallery=1,
  // buttons row = 1 + N, language row = 4. Characters: text/section text + gallery descriptions.
  function cardStats(card, languageButtons) {
    let chars = 0;
    let components = 1;
    for (const b of (card && card.blocks) || []) {
      if (b.type === "text") { chars += len(b.text); components += 1; }
      else if (b.type === "section") { chars += len(b.text); components += 3; }
      else if (b.type === "separator") components += 1;
      else if (b.type === "gallery") { for (const im of b.images || []) chars += len(im.description); components += 1; }
      else if (b.type === "buttons") components += 1 + (b.buttons || []).length;
    }
    if (languageButtons) components += 4;
    return { chars, components };
  }

  const TYPE_LABEL = {
    text: "Text", section: "Text + thumbnail", separator: "Separator", gallery: "Image gallery", buttons: "Link buttons",
  };

  function cardIssues(card, languageButtons) {
    const out = [];
    const blocks = (card && card.blocks) || [];
    if (!blocks.length) out.push(t("Add at least one block."));
    blocks.forEach((b, i) => {
      const where = t("Block {n} ({type})", { n: i + 1, type: t(TYPE_LABEL[b.type]) });
      if ((b.type === "text" || b.type === "section") && !String(b.text || "").trim()) out.push(`${where}: ${t("the text is empty.")}`);
      if (b.type === "section" && !isHttp(b.thumbnail_url)) out.push(`${where}: ${t("the thumbnail needs an http(s) image URL.")}`);
      if (b.type === "gallery") {
        const imgs = b.images || [];
        if (!imgs.length) out.push(`${where}: ${t("add at least one image.")}`);
        if (imgs.length > LIMITS.galleryImages) out.push(`${where}: ${t("at most 10 images.")}`);
        if (imgs.some((im) => !isHttp(im.url))) out.push(`${where}: ${t("every image needs an http(s) URL.")}`);
      }
      if (b.type === "buttons") {
        const bs = b.buttons || [];
        if (!bs.length) out.push(`${where}: ${t("add at least one button.")}`);
        if (bs.length > LIMITS.buttons) out.push(`${where}: ${t("at most 5 buttons.")}`);
        if (bs.some((x) => !String(x.label || "").trim() || len(x.label) > LIMITS.buttonLabel)) out.push(`${where}: ${t("every button needs a label (1-80 characters).")}`);
        if (bs.some((x) => !isHttp(x.url))) out.push(`${where}: ${t("every button needs an http(s) URL.")}`);
      }
    });
    const { chars, components } = cardStats(card, languageButtons);
    if (chars > LIMITS.chars) out.push(t("Too much text: {n} of {max} characters.", { n: chars, max: LIMITS.chars }));
    if (components > LIMITS.components) out.push(t("Too many components: {n} of {max}.", { n: components, max: LIMITS.components }));
    return out;
  }

  function cardSummary(card, max = 80) {
    for (const b of (card && card.blocks) || []) {
      if ((b.type === "text" || b.type === "section") && String(b.text || "").trim()) {
        const line = b.text.split("\n").map((l) => l.replace(/^(#{1,3}|-#|>|[-*])\s+/, "").trim()).find(Boolean) || "";
        return line.length > max ? `${line.slice(0, max)}…` : line;
      }
    }
    return t("(no content)");
  }

  // ---------- V2 preview ------------------------------------------------------------------------------

  function galleryCols(n) {
    return n === 1 ? 1 : n === 2 || n === 4 ? 2 : 3;
  }

  function previewImage(url, cls, alt) {
    const img = h("img", { class: cls, src: url, alt: alt || "", loading: "lazy" });
    img.addEventListener("error", () => { img.classList.add("broken"); });
    return img;
  }

  function previewBlock(b) {
    if (b.type === "text") return String(b.text || "").trim() ? h("div", { class: "v2-text" }, renderContent(b.text)) : null;
    if (b.type === "section") {
      const url = safeUrl(b.thumbnail_url);
      if (!String(b.text || "").trim() && !url) return null;
      return h("div", { class: "v2-section" },
        h("div", { class: "v2-text" }, renderContent(b.text || "")),
        url ? previewImage(url, "v2-thumb", "") : null);
    }
    if (b.type === "separator") {
      return h("div", { class: `v2-sep ${b.spacing === "large" ? "large" : "small"}` }, b.divider !== false ? h("hr") : null);
    }
    if (b.type === "gallery") {
      const imgs = (b.images || []).filter((im) => safeUrl(im.url)).slice(0, LIMITS.galleryImages);
      if (!imgs.length) return null;
      return h("div", { class: `v2-gallery cols-${galleryCols(imgs.length)} n-${imgs.length}` },
        imgs.map((im) => previewImage(safeUrl(im.url), "v2-gallery-img", im.description || "")));
    }
    if (b.type === "buttons") {
      const bs = (b.buttons || []).filter((x) => String(x.label || "").trim()).slice(0, LIMITS.buttons);
      if (!bs.length) return null;
      return h("div", { class: "d-buttons" }, bs.map((x) => h("a", {
        class: "d-link-button", href: safeUrl(x.url) || null, target: "_blank", rel: "noopener noreferrer",
      }, x.emoji ? h("span", { class: "d-btn-emoji" }, x.emoji) : null, x.label, h("span", { class: "d-ext" }, "↗"))));
    }
    return null;
  }

  function cardPreview(card, { languageButtons = false } = {}) {
    const blocks = ((card && card.blocks) || []).map(previewBlock).filter(Boolean);
    const accent = card && card.accent_color && /^#[0-9a-f]{6}$/i.test(card.accent_color) ? card.accent_color : null;
    const container = h("div", { class: accent ? "v2-container" : "v2-container no-accent" },
      blocks.length ? blocks : h("div", { class: "v2-empty" }, t("Nothing to show yet.")),
      languageButtons ? h("div", { class: "d-buttons lang-row" },
        LANG_ROW.map(([label, flag]) => h("span", { class: "d-link-button" }, h("span", { class: "d-btn-emoji" }, flag), label))) : null);
    if (accent) container.style.borderLeftColor = accent;
    return h("div", { class: "d-message" },
      h("img", { class: "d-avatar", src: BOT_AVATAR, alt: "" }),
      h("div", { class: "d-body" },
        h("div", { class: "d-header" },
          h("span", { class: "d-name" }, BOT_NAME),
          h("span", { class: "d-bot-tag" }, t("BOT")),
          h("span", { class: "d-timestamp" }, new Date().toLocaleTimeString(Panel.locale(), { hour: "numeric", minute: "2-digit" }))),
        container));
  }

  // ---------- the block editor ----------------------------------------------------------------------

  const BLOCK_ICON = { text: "¶", section: "▣", separator: "―", gallery: "▦", buttons: "↗" };
  let keySeq = 0;

  function newBlock(type, data) {
    const d = data || {};
    const b = { key: ++keySeq, type, open: true };
    if (type === "text") b.text = d.text || "";
    else if (type === "section") { b.text = d.text || ""; b.thumbnail_url = d.thumbnail_url || ""; }
    else if (type === "separator") { b.divider = d.divider !== false; b.spacing = d.spacing === "large" ? "large" : "small"; }
    else if (type === "gallery") b.images = (d.images && d.images.length ? d.images : [{}]).map((im) => ({ url: im.url || "", description: im.description || "" }));
    else if (type === "buttons") b.buttons = (d.buttons && d.buttons.length ? d.buttons : [{}]).map((x) => ({ label: x.label || "", url: x.url || "", emoji: x.emoji || "" }));
    return b;
  }

  function blockToJson(b) {
    if (b.type === "text") return { type: "text", text: b.text };
    if (b.type === "section") return { type: "section", text: b.text, thumbnail_url: b.thumbnail_url.trim() };
    if (b.type === "separator") return { type: "separator", divider: b.divider, spacing: b.spacing };
    if (b.type === "gallery") {
      return {
        type: "gallery",
        images: b.images.filter((im) => im.url.trim() || im.description.trim())
          .map((im) => ({ url: im.url.trim(), description: im.description.trim() || null })),
      };
    }
    return {
      type: "buttons",
      buttons: b.buttons.filter((x) => x.label.trim() || x.url.trim() || x.emoji.trim())
        .map((x) => (x.emoji.trim() ? { label: x.label.trim(), url: x.url.trim(), emoji: x.emoji.trim() } : { label: x.label.trim(), url: x.url.trim() })),
    };
  }

  function blockSummary(b) {
    if (b.type === "text" || b.type === "section") {
      const line = b.text.split("\n").map((l) => l.replace(/^(#{1,3}|-#|>|[-*])\s+/, "").trim()).find(Boolean);
      return line ? (line.length > 60 ? `${line.slice(0, 60)}…` : line) : t("(empty)");
    }
    if (b.type === "separator") return `${b.divider ? t("line") : t("no line")} · ${b.spacing === "large" ? t("large") : t("small")}`;
    if (b.type === "gallery") return t("{n} image(s)", { n: b.images.length });
    return b.buttons.map((x) => x.label).filter(Boolean).join(" · ") || t("(empty)");
  }

  function cardEditor({ onChange, serverValidate = true } = {}) {
    let blocks = [];
    let accent = DEFAULT_ACCENT;
    let useAccent = true;
    let languageButtons = false;
    let baseline = "";
    let focusKey = null;
    let previewTimer;
    let validateTimer;
    let validateSeq = 0;

    const blocksBox = h("div", { class: "v2-blocks" });
    const counters = h("div", { class: "v2-counters" });
    const issuesBox = h("ul", { class: "v2-issues" });
    const serverProblem = h("p", { class: "error small", hidden: true });
    const previewBox = h("div", { class: "stack" });

    const accentCheck = h("input", { type: "checkbox", checked: true, id: `acc-${++keySeq}` });
    const accentInput = h("input", { type: "color", value: DEFAULT_ACCENT, "aria-label": t("Accent colour") });

    function get() {
      return { accent_color: useAccent ? accent.toUpperCase() : null, blocks: blocks.map(blockToJson) };
    }

    function stats() {
      return cardStats(get(), languageButtons);
    }

    function issues() {
      return cardIssues(get(), languageButtons);
    }

    function drawCounters() {
      const card = get();
      const { chars, components } = cardStats(card, languageButtons);
      counters.replaceChildren(
        h("span", { class: chars > LIMITS.chars ? "over" : "" }, t("text {n}/{max}", { n: chars, max: LIMITS.chars })),
        " · ",
        h("span", { class: components > LIMITS.components ? "over" : "" }, t("components {n}/{max}", { n: components, max: LIMITS.components })));
      issuesBox.replaceChildren(...cardIssues(card, languageButtons).map((m) => h("li", {}, m)));
    }

    function drawPreview() {
      previewBox.replaceChildren(cardPreview(get(), { languageButtons }));
    }

    function scheduleServerValidation() {
      if (!serverValidate || !Panel.can("announce.send")) return;
      clearTimeout(validateTimer);
      validateTimer = setTimeout(async () => {
        const mine = ++validateSeq;
        const card = get();
        if (!card.blocks.length) { serverProblem.hidden = true; return; }
        try {
          await api("/api/cards/preview", { method: "POST", body: { card } });
          if (mine === validateSeq) serverProblem.hidden = true;
        } catch (error) {
          if (mine !== validateSeq) return;
          serverProblem.textContent = error.message;
          serverProblem.hidden = false;
        }
      }, 700);
    }

    // Called on every edit. Counters are instant; the preview waits a beat so typing stays smooth.
    function changed() {
      drawCounters();
      clearTimeout(previewTimer);
      previewTimer = setTimeout(drawPreview, 120);
      scheduleServerValidation();
      if (onChange) onChange();
    }

    function structural() {
      drawBlocks();
      drawCounters();
      drawPreview();
      scheduleServerValidation();
      if (onChange) onChange();
    }

    // ---- per-block editors ----

    const input = (value, attrs, handler) => {
      const el = h("input", { type: "text", value, ...attrs });
      el.addEventListener("input", () => handler(el.value));
      return el;
    };
    const area = (value, attrs, handler) => {
      const el = h("textarea", { value, ...attrs });
      el.addEventListener("input", () => handler(el.value));
      return el;
    };

    function textBody(b, onText) {
      const count = h("div", { class: "muted small" });
      const upd = () => { count.textContent = t("{n} characters", { n: len(b.text) }); };
      upd();
      return [
        Panel.field(t("Text (Discord markdown)"), area(b.text, {
          rows: "6", maxlength: String(LIMITS.chars), placeholder: t("## Heading\nWrite your message here. **bold**, *italic*, -# small text, [links](https://…)"),
        }, (v) => { b.text = v; upd(); onText(); })),
        count,
      ];
    }

    function bodyFor(b, refreshSummary) {
      const upd = () => { refreshSummary(); changed(); };
      if (b.type === "text") return textBody(b, upd);
      if (b.type === "section") {
        return [
          ...textBody(b, upd),
          Panel.field(t("Thumbnail image URL"), input(b.thumbnail_url, { type: "url", placeholder: "https://…" }, (v) => { b.thumbnail_url = v; upd(); })),
        ];
      }
      if (b.type === "separator") {
        const divider = h("input", { type: "checkbox", checked: b.divider });
        divider.addEventListener("change", () => { b.divider = divider.checked; upd(); });
        const spacing = h("select", {}, h("option", { value: "small" }, t("Small")), h("option", { value: "large" }, t("Large")));
        spacing.value = b.spacing;
        spacing.addEventListener("change", () => { b.spacing = spacing.value; upd(); });
        return h("div", { class: "row" },
          h("label", { class: "row check" }, divider, t("Show a divider line")),
          h("div", {}, h("label", {}, t("Spacing")), spacing));
      }
      if (b.type === "gallery") {
        const rows = h("div", { class: "stack tight" });
        const drawRows = () => {
          rows.replaceChildren(...b.images.map((im, i) => h("div", { class: "v2-row" },
            input(im.url, { type: "url", placeholder: t("Image URL"), "aria-label": t("Image URL") }, (v) => { im.url = v; upd(); }),
            input(im.description, { placeholder: t("Description (optional)"), "aria-label": t("Description (optional)") }, (v) => { im.description = v; upd(); }),
            h("button", {
              class: "btn small ghost", type: "button", disabled: b.images.length <= 1, "aria-label": t("Remove image"),
              onclick: () => { b.images.splice(i, 1); drawRows(); upd(); },
            }, "✕"))));
        };
        drawRows();
        const add = h("button", { class: "btn small", type: "button" }, t("Add image"));
        add.addEventListener("click", () => {
          if (b.images.length >= LIMITS.galleryImages) { Panel.toast(t("A gallery holds at most 10 images."), true); return; }
          b.images.push({ url: "", description: "" });
          drawRows();
          upd();
          const last = rows.querySelectorAll("input[type=url]");
          if (last.length) last[last.length - 1].focus();
        });
        return [rows, h("div", { class: "row" }, add, h("span", { class: "muted small" }, t("1 to 10 images.")))];
      }
      const rows = h("div", { class: "stack tight" });
      const drawRows = () => {
        rows.replaceChildren(...b.buttons.map((x, i) => h("div", { class: "v2-row buttons" },
          input(x.label, { maxlength: String(LIMITS.buttonLabel), placeholder: t("Label"), "aria-label": t("Label") }, (v) => { x.label = v; upd(); }),
          input(x.url, { type: "url", placeholder: "https://…", "aria-label": t("URL") }, (v) => { x.url = v; upd(); }),
          input(x.emoji, { maxlength: "40", placeholder: t("Emoji"), class: "emoji-input", "aria-label": t("Emoji (optional)") }, (v) => { x.emoji = v; upd(); }),
          h("button", {
            class: "btn small ghost", type: "button", disabled: b.buttons.length <= 1, "aria-label": t("Remove button"),
            onclick: () => { b.buttons.splice(i, 1); drawRows(); upd(); },
          }, "✕"))));
      };
      drawRows();
      const add = h("button", { class: "btn small", type: "button" }, t("Add button"));
      add.addEventListener("click", () => {
        if (b.buttons.length >= LIMITS.buttons) { Panel.toast(t("Discord allows at most 5 buttons."), true); return; }
        b.buttons.push({ label: "", url: "", emoji: "" });
        drawRows();
        upd();
        const last = rows.querySelectorAll(".v2-row input");
        if (last.length) last[last.length - 3].focus();
      });
      return [rows, h("div", { class: "row" }, add, h("span", { class: "muted small" }, t("1 to 5 link buttons.")))];
    }

    function move(i, to) {
      if (to < 0 || to >= blocks.length) return;
      blocks.splice(to, 0, blocks.splice(i, 1)[0]);
      structural();
    }

    function blockCard(b, i) {
      const summary = h("span", { class: "v2-summary muted" }, blockSummary(b));
      const body = h("div", { class: "v2-block-body stack", hidden: !b.open }, bodyFor(b, () => { summary.textContent = blockSummary(b); }));
      const toggle = h("button", {
        class: "v2-toggle", type: "button", "aria-expanded": String(b.open),
        "aria-label": b.open ? t("Collapse block") : t("Expand block"),
      }, h("span", { class: "v2-icon", "aria-hidden": "true" }, BLOCK_ICON[b.type]),
      h("strong", {}, t(TYPE_LABEL[b.type])), summary);
      toggle.addEventListener("click", () => {
        b.open = !b.open;
        body.hidden = !b.open;
        toggle.setAttribute("aria-expanded", String(b.open));
        toggle.setAttribute("aria-label", b.open ? t("Collapse block") : t("Expand block"));
        card.classList.toggle("collapsed", !b.open);
      });
      const btn = (label, aria, handler, disabled, danger) => h("button", {
        class: `btn small ghost${danger ? " danger-text" : ""}`, type: "button", "aria-label": aria, title: aria, disabled, onclick: handler,
      }, label);
      const card = h("div", { class: `v2-block${b.open ? "" : " collapsed"}`, "data-key": String(b.key) },
        h("div", { class: "v2-block-head" }, toggle,
          h("div", { class: "v2-actions" },
            btn("▲", t("Move up"), () => move(i, i - 1), i === 0),
            btn("▼", t("Move down"), () => move(i, i + 1), i === blocks.length - 1),
            btn("✕", t("Remove block"), () => { blocks.splice(i, 1); structural(); }, false, true))),
        body);
      return card;
    }

    function drawBlocks() {
      blocksBox.replaceChildren(...(blocks.length
        ? blocks.map(blockCard)
        : [h("div", { class: "empty" }, t("No blocks yet. Add one below to start building the message."))]));
      if (focusKey !== null) {
        const el = blocksBox.querySelector(`[data-key="${focusKey}"]`);
        focusKey = null;
        if (el) {
          el.scrollIntoView({ block: "nearest" });
          const first = el.querySelector("textarea, input:not([type=checkbox]), select");
          if (first) first.focus({ preventScroll: true });
        }
      }
    }

    function addBlock(type) {
      const b = newBlock(type);
      blocks.push(b);
      focusKey = b.key;
      structural();
    }

    const addMenu = h("div", { class: "v2-add" },
      h("div", { class: "small muted" }, t("Add a block")),
      h("div", { class: "row" }, Object.keys(TYPE_LABEL).map((type) => h("button", {
        class: "btn small", type: "button", onclick: () => addBlock(type),
      }, h("span", { class: "v2-icon", "aria-hidden": "true" }, BLOCK_ICON[type]), t(TYPE_LABEL[type])))));

    accentCheck.addEventListener("change", () => { useAccent = accentCheck.checked; accentInput.disabled = !useAccent; changed(); });
    accentInput.addEventListener("input", () => { accent = accentInput.value; changed(); });

    const el = h("div", { class: "card-workbench" },
      h("div", { class: "card-editor-col stack" },
        h("div", { class: "v2-accent row" },
          h("label", { class: "row check", for: accentCheck.id }, accentCheck, t("Accent colour")),
          accentInput,
          h("span", { class: "muted small" }, t("The coloured bar on the left of the card."))),
        blocksBox,
        addMenu),
      h("div", { class: "card-preview-col stack" },
        h("h2", {}, t("Preview")),
        counters,
        issuesBox,
        serverProblem,
        previewBox,
        h("p", { class: "muted small" }, t("Buttons are shown as they'll appear; they only work in Discord."))));

    function set(card) {
      const c = card || null;
      useAccent = c ? Boolean(c.accent_color) : true;
      accent = (c && c.accent_color) || DEFAULT_ACCENT;
      accentCheck.checked = useAccent;
      accentInput.value = /^#[0-9a-f]{6}$/i.test(accent) ? accent.toLowerCase() : DEFAULT_ACCENT.toLowerCase();
      accentInput.disabled = !useAccent;
      blocks = c && c.blocks ? c.blocks.map((b) => newBlock(b.type, b)) : [newBlock("text")];
      drawBlocks();
      drawCounters();
      drawPreview();
      serverProblem.hidden = true;
      baseline = JSON.stringify(get());
    }

    set(null);

    return {
      el, get, set, stats, issues,
      setLanguageButtons(on) { languageButtons = Boolean(on); drawCounters(); drawPreview(); },
      dirty: () => JSON.stringify(get()) !== baseline,
      markClean() { baseline = JSON.stringify(get()); },
    };
  }

  Panel.cardPreview = cardPreview;
  Panel.cardEditor = cardEditor;
  Panel.cardSummary = cardSummary;
  Panel.CARD_LIMITS = LIMITS;

  i18n({
    "BOT": "BOT",
    "Text": "Texto",
    "Text + thumbnail": "Texto + miniatura",
    "Separator": "Separador",
    "Image gallery": "Galería de imágenes",
    "Link buttons": "Botones de enlace",
    "Add at least one block.": "Agrega al menos un bloque.",
    "Block {n} ({type})": "Bloque {n} ({type})",
    "the text is empty.": "el texto está vacío.",
    "the thumbnail needs an http(s) image URL.": "la miniatura necesita una URL de imagen http(s).",
    "add at least one image.": "agrega al menos una imagen.",
    "at most 10 images.": "máximo 10 imágenes.",
    "every image needs an http(s) URL.": "cada imagen necesita una URL http(s).",
    "add at least one button.": "agrega al menos un botón.",
    "at most 5 buttons.": "máximo 5 botones.",
    "every button needs a label (1-80 characters).": "cada botón necesita una etiqueta (1-80 caracteres).",
    "every button needs an http(s) URL.": "cada botón necesita una URL http(s).",
    "Too much text: {n} of {max} characters.": "Demasiado texto: {n} de {max} caracteres.",
    "Too many components: {n} of {max}.": "Demasiados componentes: {n} de {max}.",
    "(no content)": "(sin contenido)",
    "Nothing to show yet.": "Aún no hay nada que mostrar.",
    "(empty)": "(vacío)",
    "line": "línea",
    "no line": "sin línea",
    "large": "grande",
    "small": "pequeño",
    "{n} image(s)": "{n} imagen(es)",
    "text {n}/{max}": "texto {n}/{max}",
    "components {n}/{max}": "componentes {n}/{max}",
    "{n} characters": "{n} caracteres",
    "Text (Discord markdown)": "Texto (markdown de Discord)",
    "## Heading\nWrite your message here. **bold**, *italic*, -# small text, [links](https://…)":
      "## Encabezado\nEscribe tu mensaje aquí. **negrita**, *cursiva*, -# texto pequeño, [enlaces](https://…)",
    "Thumbnail image URL": "URL de la imagen de miniatura",
    "Show a divider line": "Mostrar una línea divisoria",
    "Spacing": "Espaciado",
    "Small": "Pequeño",
    "Large": "Grande",
    "Image URL": "URL de la imagen",
    "Description (optional)": "Descripción (opcional)",
    "Remove image": "Quitar imagen",
    "Add image": "Agregar imagen",
    "A gallery holds at most 10 images.": "Una galería admite como máximo 10 imágenes.",
    "1 to 10 images.": "De 1 a 10 imágenes.",
    "Label": "Etiqueta",
    "URL": "URL",
    "Emoji": "Emoji",
    "Emoji (optional)": "Emoji (opcional)",
    "Remove button": "Quitar botón",
    "Add button": "Agregar botón",
    "Discord allows at most 5 buttons.": "Discord permite hasta 5 botones.",
    "1 to 5 link buttons.": "De 1 a 5 botones de enlace.",
    "Collapse block": "Contraer bloque",
    "Expand block": "Expandir bloque",
    "Move up": "Subir",
    "Move down": "Bajar",
    "Remove block": "Quitar bloque",
    "No blocks yet. Add one below to start building the message.": "Aún no hay bloques. Agrega uno abajo para empezar a armar el mensaje.",
    "Add a block": "Agregar un bloque",
    "Accent colour": "Color de acento",
    "The coloured bar on the left of the card.": "La barra de color a la izquierda de la tarjeta.",
    "Preview": "Vista previa",
    "Buttons are shown as they'll appear; they only work in Discord.": "Los botones se ven como aparecerán; solo funcionan en Discord.",
  });
})();
