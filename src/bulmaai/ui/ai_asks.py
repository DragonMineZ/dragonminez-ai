"""Bulma's answer controls. When her reply asks the requester something, the ask_requester tool attaches a control
spec (validated here) and the answer card carries buttons, a select, a "Fill in" form, an "Upload" form or a steps
checklist for it; the offer_action tool attaches one button that runs an existing flow for them instead. Only the
requester may use them. The answer is recorded on the card as `-# ↳ ` lines, which history and transcripts read
back as the requester's turn; the spec itself lives in the ai_asks table (services/ai_asks), so clicks work after a
restart. Uploaded files are reposted on a card of their own whose `-# ↳ <@id> uploaded **name**` lines credit the
requester, so the files read back as their turn.

Pure helpers only (spec validation, the card's controls, custom ids, the modals, answer lines, readback); the
Discord wiring is in cogs/ai_tickets.py."""

import re
from typing import Any, Callable

import discord

from bulmaai.services import ai_guard
from bulmaai.ui.log_help_views import LOG_HELP_CONTENT
from bulmaai.ui.ticket_views import CATEGORIES
from bulmaai.ui.v2 import TEXT_LIMIT, card, fit

KINDS = ("choices", "select", "form", "file", "steps")  # ask_requester's
ACTION_KIND = "action"  # offer_action's
CONTROL_KINDS = (*KINDS, ACTION_KIND)
FIELD_TYPES = ("short", "long", "select", "radio")
FILE_WHATS = ("log", "screenshot", "any")
MAX_FILES = 3
UPLOAD_INDEX, WHERE_INDEX = 0, 1  # the file kind's controls: the Upload button and the "Where is it?" select
UPLOAD_FIELD_ID = "files"
UPLOAD_NAME_LIMIT = 60  # three upload lines still fit under a long answer card
PLACES = ("windows", "mac", "linux", "server")  # ui/log_help_views keys; the launchers line is ours (TEXT)
HELP_COLOR = discord.Color.from_rgb(88, 101, 242)  # /loghelp's
MAX_CHOICES = 5  # one row of buttons
MAX_SELECT_OPTIONS = 25
MAX_RADIO_OPTIONS = 10
MAX_FIELDS = 5
BUTTON_LABEL_LIMIT = 80
OPTION_LABEL_LIMIT = 100  # select/radio option labels and descriptions, placeholders
TITLE_LIMIT = 45  # the form's title and each field's label
SHORT_VALUE_LIMIT = 200
LONG_VALUE_LIMIT = 1000
MAX_STEPS = 8
STEP_LIMIT = 200
STEP_DONE, STEP_BROKEN = 0, 1  # a steps button's index is step * 2 + this
CONTROLS_TEXT_ID = 810  # the component id of the text that belongs to the controls (a steps card's list)
ACTIONS = ("get_staff", "open_ticket", "link_patreon")
# Where each action may go out (ai_client's channel kinds). get_staff is the ticket's "No, I still need help";
# open_ticket is the ticket panel, which needs a server and makes no sense inside a ticket.
ACTION_CHANNELS = {"get_staff": ("ticket",), "open_ticket": ("public",), "link_patreon": ("ticket", "dm", "public")}
ACTION_WHERE = {"get_staff": "in tickets", "open_ticket": "in public channels", "link_patreon": "anywhere"}
ACTION_RUN, ACTION_DECLINE = 0, 1  # an action card's buttons: the action, and "No thanks"
ALLOWED_FLAGS = frozenset({"unknown_link"})  # a label is never a clickable link, so "latest.log" may stay

CUSTOM_ID_PREFIX = "ai_ask:"  # ai_ask:{kind}:{requester_id}:{index}
FORM_MODAL_PREFIX = "ai_ask_form:"  # ai_ask_form:{card message id}
FILE_MODAL_PREFIX = "ai_ask_file:"  # ai_ask_file:{card message id}
ANSWER_LINE_PREFIX = "-# ↳ "
# A pick: "-# ↳ <@id>: **label**". A form field: "-# ↳ <@id> · Field label: **value**".
ANSWER_LINE_RE = re.compile(r"^-# ↳ <@!?(\d+)>(?: · (.+?))?: \*\*(.+)\*\*$", re.DOTALL)
# An uploaded file, on the question card and on the repost that carries it: "-# ↳ <@id> uploaded **name**".
UPLOAD_LINE_RE = re.compile(r"^-# ↳ <@!?(\d+)> uploaded \*\*(.+)\*\*$")
SHOWN_NOTE = (
    "The requester sees this under your reply. Now write the reply itself: the question in full, so it reads fine "
    "without the controls."
)
STEPS_NOTE = (
    "The requester sees these steps under your reply, one at a time, with Done and Still broken buttons; you hear "
    "back when a step fails or all are done. Now write the reply itself: introduce the steps, don't list them again."
)
ACTION_NOTE = (
    "The requester sees this button under your reply; it runs only when they click it. Now write the reply itself, "
    "saying what the button does, so it reads fine without it."
)

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "fill_in": "Fill in",
        "not_yours": "Only the person Bulma asked can answer this.",
        "ai_off": "Bulma isn't answering here right now. Type your answer in the chat instead.",
        "closed": "This ticket is no longer open.",
        "answered": "Already answered.",
        "expired": "This question expired, type your answer instead.",
        "failed": "Something went wrong, please try again.",
        "paused": "My AI circuits are recharging for today ⚡ I'll be back at 00:00 UTC. "
        "A staff member will help you in the meantime.",
        "rate_limited": "You're sending messages faster than I can keep up, so I'll pause for a few minutes. "
        "Staff can still see this.",
        "upload": "Upload",
        "upload_title_log": "Upload your log",
        "upload_title_screenshot": "Upload a screenshot",
        "upload_title_any": "Upload a file",
        "upload_field_log": "latest.log or crash-report.txt",
        "upload_field_screenshot": "Screenshot",
        "upload_field_any": "File",
        "too_big": "That file is too big, zip it or type the error instead.",
        "where": "Where is it?",
        "launchers_title": "🚀 Launchers",
        "launchers_value": "CurseForge app: right-click the profile → Open Folder → `logs`\n"
        "Modrinth App: right-click the instance → Open folder → `logs`\n"
        "Prism Launcher: select the instance → Folder → `.minecraft` (or `minecraft`) → `logs`",
        "done": "Done",
        "still_broken": "Still broken",
        "step_failed": "Step {n} didn't work:",
        "steps_done": "Did all the steps (1-{n})",
        "no_thanks": "No thanks",
        "action_get_staff": "Get a staff member",
        "action_open_ticket": "Open a ticket",
        "action_link_patreon": "Link Patreon",
        "on_it": "On it…",
        "ask_off": "Bulma isn't answering here right now.",
        "ask_no_access": "You can't ask Bulma here. Try `/ask`, or open a ticket.",
        "ask_bot": "Bulma can't help with another bot's message.",
        "ask_empty": "That message has nothing Bulma can read.",
    },
    "es": {
        "fill_in": "Rellenar",
        "not_yours": "Solo la persona a la que Bulma preguntó puede responder esto.",
        "ai_off": "Bulma no está respondiendo aquí ahora mismo. Escribe tu respuesta en el chat.",
        "closed": "Este ticket ya no está abierto.",
        "answered": "Ya se respondió.",
        "expired": "Esta pregunta caducó, escribe tu respuesta en el chat.",
        "failed": "Algo salió mal, inténtalo de nuevo.",
        "paused": "Mis circuitos de IA se están recargando por hoy ⚡ Vuelvo a las 00:00 UTC. "
        "Un miembro del staff te ayudará mientras tanto.",
        "rate_limited": "Estás enviando mensajes más rápido de lo que puedo seguir, así que pausaré unos minutos. "
        "El staff aún puede ver esto.",
        "upload": "Subir",
        "upload_title_log": "Sube tu log",
        "upload_title_screenshot": "Sube una captura",
        "upload_title_any": "Sube un archivo",
        "upload_field_log": "latest.log o crash-report.txt",
        "upload_field_screenshot": "Captura de pantalla",
        "upload_field_any": "Archivo",
        "too_big": "Ese archivo es demasiado grande, comprímelo o escribe el error.",
        "where": "¿Dónde está?",
        "launchers_title": "🚀 Launchers",
        "launchers_value": "App de CurseForge: clic derecho en el perfil → Open Folder → `logs`\n"
        "Modrinth App: clic derecho en la instancia → Open folder → `logs`\n"
        "Prism Launcher: selecciona la instancia → Folder → `.minecraft` (o `minecraft`) → `logs`",
        "done": "Hecho",
        "still_broken": "Sigue sin funcionar",
        "step_failed": "El paso {n} no funcionó:",
        "steps_done": "Hice todos los pasos (1-{n})",
        "no_thanks": "No, gracias",
        "action_get_staff": "Llamar al staff",
        "action_open_ticket": "Abrir un ticket",
        "action_link_patreon": "Vincular Patreon",
        "on_it": "Estoy en ello…",
        "ask_off": "Bulma no está respondiendo aquí ahora mismo.",
        "ask_no_access": "No puedes preguntarle a Bulma aquí. Prueba `/ask` o abre un ticket.",
        "ask_bot": "Bulma no puede ayudar con el mensaje de otro bot.",
        "ask_empty": "Ese mensaje no tiene nada que Bulma pueda leer.",
    },
    "pt": {
        "fill_in": "Preencher",
        "not_yours": "Só a pessoa a quem a Bulma perguntou pode responder isto.",
        "ai_off": "A Bulma não está respondendo aqui agora. Escreva sua resposta no chat.",
        "closed": "Este ticket não está mais aberto.",
        "answered": "Já respondido.",
        "expired": "Esta pergunta expirou, escreva sua resposta no chat.",
        "failed": "Algo deu errado, tente novamente.",
        "paused": "Meus circuitos de IA estão recarregando por hoje ⚡ Volto às 00:00 UTC. "
        "Um membro da staff vai te ajudar enquanto isso.",
        "rate_limited": "Você está enviando mensagens mais rápido do que consigo acompanhar, então vou pausar por "
        "alguns minutos. A staff ainda pode ver isto.",
        "upload": "Enviar",
        "upload_title_log": "Envie seu log",
        "upload_title_screenshot": "Envie uma captura de tela",
        "upload_title_any": "Envie um arquivo",
        "upload_field_log": "latest.log ou crash-report.txt",
        "upload_field_screenshot": "Captura de tela",
        "upload_field_any": "Arquivo",
        "too_big": "Esse arquivo é grande demais, compacte-o ou escreva o erro.",
        "where": "Onde fica?",
        "launchers_title": "🚀 Launchers",
        "launchers_value": "App do CurseForge: clique com o botão direito no perfil → Open Folder → `logs`\n"
        "Modrinth App: clique com o botão direito na instância → Open folder → `logs`\n"
        "Prism Launcher: selecione a instância → Folder → `.minecraft` (ou `minecraft`) → `logs`",
        "done": "Feito",
        "still_broken": "Ainda não funciona",
        "step_failed": "O passo {n} não funcionou:",
        "steps_done": "Fiz todos os passos (1-{n})",
        "no_thanks": "Não, obrigado",
        "action_get_staff": "Chamar a staff",
        "action_open_ticket": "Abrir um ticket",
        "action_link_patreon": "Vincular Patreon",
        "on_it": "Estou nisso…",
        "ask_off": "A Bulma não está respondendo aqui agora.",
        "ask_no_access": "Você não pode perguntar à Bulma aqui. Tente `/ask` ou abra um ticket.",
        "ask_bot": "A Bulma não pode ajudar com a mensagem de outro bot.",
        "ask_empty": "Essa mensagem não tem nada que a Bulma possa ler.",
    },
}


def text(language: str | None, key: str) -> str:
    return TEXT.get(language or "en", TEXT["en"])[key]


# --- Spec validation (the ask_requester tool) ---


def _clean(value: Any, limit: int, settings: Any) -> str | None:
    """One line of model-written text, or None when it is empty, too long, or trips the reply guard."""
    if not isinstance(value, str):
        return None
    line = " ".join(value.split())
    if not line or len(line) > limit:
        return None
    screened, flags = ai_guard.screen_reply(line, settings)
    return screened if not flags - ALLOWED_FLAGS and len(screened) <= limit else None


def _preview(value: Any) -> str:
    return " ".join(str(value).split())[:40]


def _labels(raw: Any, limit: int, settings: Any, dropped: list[str], what: str) -> list[str]:
    """The distinct (ignoring case) valid labels of a model-written list, in order."""
    out: list[str] = []
    for value in raw if isinstance(raw, list) else []:
        label = _clean(value, limit, settings)
        if label is None:
            dropped.append(f"{what} {_preview(value)!r}: empty, over {limit} characters or not allowed")
        elif label.casefold() in {seen.casefold() for seen in out}:
            dropped.append(f"{what} {label!r}: duplicate")
        else:
            out.append(label)
    return out


def _capped(items: list, limit: int, dropped: list[str], what: str) -> list:
    if len(items) > limit:
        dropped.append(f"{what} after the first {limit}")
    return items[:limit]


def _choices(args: dict, settings: Any, dropped: list[str]) -> dict | str:
    labels = _labels(args.get("choices"), BUTTON_LABEL_LIMIT, settings, dropped, "choice")
    labels = _capped(labels, MAX_CHOICES, dropped, "choices")
    if len(labels) < 2:
        return f"choices needs 2-{MAX_CHOICES} short answers (up to {BUTTON_LABEL_LIMIT} characters each)"
    return {"kind": "choices", "choices": labels}


def _select(args: dict, settings: Any, dropped: list[str]) -> dict | str:
    options: list[dict[str, str]] = []
    for raw in args.get("options") if isinstance(args.get("options"), list) else []:
        raw = raw if isinstance(raw, dict) else {"label": raw}
        label = _clean(raw.get("label"), OPTION_LABEL_LIMIT, settings)
        if label is None or label.casefold() in {option["label"].casefold() for option in options}:
            dropped.append(f"option {_preview(raw.get('label'))!r}: empty, duplicate, too long or not allowed")
            continue
        option = {"label": label}
        if raw.get("description"):
            if description := _clean(raw["description"], OPTION_LABEL_LIMIT, settings):
                option["description"] = description
            else:
                dropped.append(f"description of option {label!r}")
        options.append(option)
    options = _capped(options, MAX_SELECT_OPTIONS, dropped, "options")
    if len(options) < 2:
        return f"select needs 2-{MAX_SELECT_OPTIONS} options (labels up to {OPTION_LABEL_LIMIT} characters)"
    spec: dict[str, Any] = {"kind": "select", "options": options}
    if args.get("placeholder"):
        if placeholder := _clean(args["placeholder"], OPTION_LABEL_LIMIT, settings):
            spec["placeholder"] = placeholder
        else:
            dropped.append("placeholder")
    return spec


def _field(raw: Any, settings: Any, dropped: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        dropped.append(f"field {_preview(raw)!r}: not an object")
        return None
    label = _clean(raw.get("label"), TITLE_LIMIT, settings)
    field_type = raw.get("type") or "short"
    if label is None or field_type not in FIELD_TYPES:
        dropped.append(
            f"field {_preview(raw.get('label'))!r}: label empty, over {TITLE_LIMIT} characters or not allowed, "
            "or unknown type"
        )
        return None
    field: dict[str, Any] = {"label": label, "type": field_type, "required": raw.get("required") is not False}
    if field_type in ("select", "radio"):
        limit = MAX_SELECT_OPTIONS if field_type == "select" else MAX_RADIO_OPTIONS
        options = _labels(raw.get("options"), OPTION_LABEL_LIMIT, settings, dropped, f"{label!r} option")
        options = _capped(options, limit, dropped, f"{label!r} options")
        if len(options) < 2:
            dropped.append(f"field {label!r}: {field_type} needs 2-{limit} options")
            return None
        field["options"] = options
    if raw.get("placeholder") and field_type != "radio":
        if placeholder := _clean(raw["placeholder"], OPTION_LABEL_LIMIT, settings):
            field["placeholder"] = placeholder
        else:
            dropped.append(f"placeholder of field {label!r}")
    return field


def _form(args: dict, settings: Any, dropped: list[str]) -> dict | str:
    title = _clean(args.get("title"), TITLE_LIMIT, settings)
    if title is None:
        return f"form needs a title (up to {TITLE_LIMIT} characters)"
    fields: list[dict[str, Any]] = []
    for raw in args.get("fields") if isinstance(args.get("fields"), list) else []:
        field = _field(raw, settings, dropped)
        if field is None:
            continue
        if field["label"].casefold() in {seen["label"].casefold() for seen in fields}:
            dropped.append(f"field {field['label']!r}: duplicate")
            continue
        fields.append(field)
    fields = _capped(fields, MAX_FIELDS, dropped, "fields")
    if not fields:
        return f"form needs 1-{MAX_FIELDS} fields"
    if not any(field["required"] for field in fields):
        fields[0]["required"] = True  # a submit always answers something
    return {"kind": "form", "title": title, "fields": fields}


def _file(args: dict, settings: Any, dropped: list[str]) -> dict | str:
    what = args.get("what") or "any"
    if what not in FILE_WHATS:
        return f"file needs what: one of {', '.join(FILE_WHATS)}"
    spec: dict[str, Any] = {"kind": "file", "what": what, "max_files": 1}
    max_files = args.get("max_files")
    if max_files is not None:
        if isinstance(max_files, int) and not isinstance(max_files, bool) and 1 <= max_files <= MAX_FILES:
            spec["max_files"] = max_files
        else:
            dropped.append(f"max_files {_preview(max_files)!r}: must be 1-{MAX_FILES}")
    if args.get("label"):
        if label := _clean(args["label"], BUTTON_LABEL_LIMIT, settings):
            spec["label"] = label
        else:
            dropped.append(f"label {_preview(args['label'])!r}: over {BUTTON_LABEL_LIMIT} characters or not allowed")
    return spec


def _steps(args: dict, settings: Any, dropped: list[str]) -> dict | str:
    steps = _labels(args.get("steps"), STEP_LIMIT, settings, dropped, "step")
    steps = _capped(steps, MAX_STEPS, dropped, "steps")
    if len(steps) < 2:
        return f"steps needs 2-{MAX_STEPS} short steps in order (up to {STEP_LIMIT} characters each)"
    return {"kind": "steps", "steps": steps}


# One validator per kind.
VALIDATORS: dict[str, Callable[[dict, Any, list[str]], dict | str]] = {
    "choices": _choices,
    "select": _select,
    "form": _form,
    "file": _file,
    "steps": _steps,
}


def validate_ask(args: dict, settings: Any) -> dict[str, Any]:
    """The ask_requester tool's output. Valid: {"ok": True, "shown": spec} (what the card will carry; invalid parts
    are listed under "dropped"). Nothing usable: {"ok": False, "error": ...}, so the model can fix the call."""
    validator = VALIDATORS.get(args.get("kind"))
    if validator is None:
        return {"ok": False, "error": f"kind must be one of: {', '.join(KINDS)}"}
    dropped: list[str] = []
    spec = validator(args, settings, dropped)
    extra = {"dropped": dropped} if dropped else {}
    if isinstance(spec, str):
        return {"ok": False, "error": spec, **extra}
    return {"ok": True, "shown": spec, **extra, "note": STEPS_NOTE if spec["kind"] == "steps" else SHOWN_NOTE}


def validate_action(args: dict, settings: Any, channel_kind: str | None = None) -> dict[str, Any]:
    """The offer_action tool's output, shaped like validate_ask's. channel_kind (ticket, dm, public) comes from
    Discord, never from the model: an action that can't run there is an error the model can fix."""
    action = args.get("action")
    if action not in ACTIONS:
        return {"ok": False, "error": f"action must be one of: {', '.join(ACTIONS)}"}
    if channel_kind is not None and channel_kind not in ACTION_CHANNELS[action]:
        return {"ok": False, "error": f"{action} can only be offered {ACTION_WHERE[action]}, and this is a {channel_kind}"}
    dropped: list[str] = []
    spec: dict[str, Any] = {"kind": ACTION_KIND, "action": action}
    if args.get("label"):
        if label := _clean(args["label"], BUTTON_LABEL_LIMIT, settings):
            spec["label"] = label
        else:
            dropped.append(f"label {_preview(args['label'])!r}: over {BUTTON_LABEL_LIMIT} characters or not allowed")
    if args.get("category"):
        if action != "open_ticket":
            dropped.append("category: only for open_ticket")
        elif args["category"] in CATEGORIES:
            spec["category"] = args["category"]
        else:
            dropped.append(
                f"category {_preview(args['category'])!r}: one of {', '.join(CATEGORIES)}; without it they pick one"
            )
    if args.get("no_thanks") is True:
        spec["no_thanks"] = True
    extra = {"dropped": dropped} if dropped else {}
    return {"ok": True, "shown": spec, **extra, "note": ACTION_NOTE}


def allowed_in(spec: dict, channel_kind: str) -> bool:
    """Whether a card there may carry these controls. Uploads never go out in public (the bot reposts the files, so
    moderation never sees them); an action only goes where it can run."""
    kind = spec.get("kind")
    if kind == "file":
        return channel_kind != "public"
    if kind == ACTION_KIND:
        return channel_kind in ACTION_CHANNELS.get(spec.get("action"), ())
    return True


def reserved_text(spec: dict | None) -> int:
    """Room the controls need on the answer's last card besides the answer: a steps card's list and its outcome line."""
    if not spec or spec.get("kind") != "steps":
        return 0
    return sum(len(step) + 12 for step in spec.get("steps") or []) + 300


# --- The card's controls ---


def custom_id(kind: str, requester_id: int, index: int = 0) -> str:
    return f"{CUSTOM_ID_PREFIX}{kind}:{requester_id}:{index}"


def parse_custom_id(value: str) -> tuple[str, int, int] | None:
    """(kind, requester id, index) of a control on an answer card."""
    if not value.startswith(CUSTOM_ID_PREFIX):
        return None
    try:
        kind, requester_id, index = value.removeprefix(CUSTOM_ID_PREFIX).split(":")
        return (kind, int(requester_id), int(index)) if kind in CONTROL_KINDS else None
    except ValueError:
        return None


def step_index(step: int, button: int) -> int:
    """The custom id index of a steps button (STEP_DONE / STEP_BROKEN) for step (0-based)."""
    return step * 2 + button


def split_step_index(index: int) -> tuple[int, int]:
    """(step, button) of a steps button's custom id index."""
    return divmod(index, 2)


def steps_text(spec: dict, progress: int, *, failed: int | None = None) -> str:
    """The steps as a numbered list: done ones ✅, the current one (progress) bold, a failed one ❌. progress past the
    last step: all done. The numbers are escaped so Discord doesn't turn the lines into its own list."""
    lines = []
    for index, step in enumerate(spec.get("steps") or []):
        line = f"{index + 1}\\. {step}"
        if index == failed:
            line = f"❌ {line}"
        elif index < progress:
            line = f"✅ {line}"
        elif index == progress and failed is None:
            line = f"**{line}**"
        lines.append(line)
    return "\n".join(lines)


def steps_items(spec: dict, requester_id: int, language: str | None, progress: int = 0) -> list[discord.ui.Item]:
    """A steps card's controls at step progress: the list (CONTROLS_TEXT_ID, so a click can swap it) and the current
    step's Done / Still broken. The custom ids carry the step, so a click on an outdated card is spotted."""
    buttons = discord.ui.ActionRow(
        discord.ui.Button(
            label=text(language, "done"),
            emoji="✅",
            style=discord.ButtonStyle.secondary,
            custom_id=custom_id("steps", requester_id, step_index(progress, STEP_DONE)),
        ),
        discord.ui.Button(
            label=text(language, "still_broken"),
            emoji="❌",
            style=discord.ButtonStyle.secondary,
            custom_id=custom_id("steps", requester_id, step_index(progress, STEP_BROKEN)),
        ),
    )
    return [discord.ui.TextDisplay(steps_text(spec, progress), id=CONTROLS_TEXT_ID), buttons]


def action_label(spec: dict, language: str | None) -> str:
    """An action button's text: the model's label, else ours for that action."""
    return spec.get("label") or text(language, f"action_{spec.get('action')}")


def ask_rows(spec: dict, requester_id: int, language: str | None) -> list[discord.ui.Item]:
    """The controls under the answer, handled by the cog's on_interaction via custom id (they survive restarts):
    action rows, and for steps the list they belong to."""
    kind = spec.get("kind")
    if kind == "steps":
        return steps_items(spec, requester_id, language)
    if kind == ACTION_KIND and spec.get("action") in ACTIONS:
        buttons = [
            discord.ui.Button(
                label=action_label(spec, language),
                style=discord.ButtonStyle.primary,
                custom_id=custom_id(kind, requester_id, ACTION_RUN),
            )
        ]
        if spec.get("no_thanks"):
            buttons.append(
                discord.ui.Button(
                    label=text(language, "no_thanks"),
                    style=discord.ButtonStyle.secondary,
                    custom_id=custom_id(kind, requester_id, ACTION_DECLINE),
                )
            )
        return [discord.ui.ActionRow(*buttons)]
    if kind == "choices":
        return [
            discord.ui.ActionRow(
                *(
                    discord.ui.Button(
                        label=label, style=discord.ButtonStyle.secondary, custom_id=custom_id(kind, requester_id, index)
                    )
                    for index, label in enumerate(spec["choices"])
                )
            )
        ]
    if kind == "select":
        select = discord.ui.Select(
            custom_id=custom_id(kind, requester_id),
            placeholder=spec.get("placeholder"),
            options=[
                discord.SelectOption(label=option["label"], value=str(index), description=option.get("description"))
                for index, option in enumerate(spec["options"])
            ],
            min_values=1,
            max_values=1,
        )
        return [discord.ui.ActionRow(select)]
    if kind == "form":
        button = discord.ui.Button(
            label=text(language, "fill_in"),
            emoji="📝",
            style=discord.ButtonStyle.secondary,
            custom_id=custom_id(kind, requester_id),
        )
        return [discord.ui.ActionRow(button)]
    if kind == "file":
        upload = discord.ui.Button(
            label=spec.get("label") or text(language, "upload"),
            emoji="📎",
            style=discord.ButtonStyle.secondary,
            custom_id=custom_id(kind, requester_id, UPLOAD_INDEX),
        )
        rows = [discord.ui.ActionRow(upload)]
        if spec.get("what") == "log":
            where = discord.ui.Select(
                custom_id=custom_id(kind, requester_id, WHERE_INDEX),
                placeholder=text(language, "where"),
                options=[discord.SelectOption(label=title, value=key) for key, title, _ in _places(language)],
                min_values=1,
                max_values=1,
            )
            rows.append(discord.ui.ActionRow(where))
        return rows
    return []


def picked_label(spec: dict, kind: str, index: int, values: list | None = None) -> str | None:
    """The answer a button (by index) or the select (by its submitted value) stands for."""
    if spec.get("kind") != kind:
        return None
    try:
        if kind == "choices":
            return spec["choices"][index] if index >= 0 else None
        if kind == "select":
            return spec["options"][int((values or [None])[0])]["label"]
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return None


def _places(language: str | None) -> list[tuple[str, str, str]]:
    """(key, title, where the logs are) for the "Where is it?" select: /loghelp's platforms, then the launchers."""
    data = LOG_HELP_CONTENT.get(language or "en", LOG_HELP_CONTENT["en"])
    return [
        *((key, data[f"{key}_title"], data[f"{key}_value"]) for key in PLACES),
        ("launchers", text(language, "launchers_title"), text(language, "launchers_value")),
    ]


def where_card(language: str | None, values: list | None) -> discord.ui.DesignerView | None:
    """The ephemeral answer to a "Where is it?" pick: that platform's log folders. None for an unknown value."""
    picked = (values or [None])[0]
    for key, title, where in _places(language):
        if key == picked:
            return card(f"**{title}**\n{where}", color=HELP_COLOR)
    return None


# --- The modals (form, upload) ---


def _modal_message_id(prefix: str, value: str) -> int | None:
    if not value.startswith(prefix):
        return None
    try:
        return int(value.removeprefix(prefix))
    except ValueError:
        return None


def form_modal_id(message_id: int) -> str:
    return f"{FORM_MODAL_PREFIX}{message_id}"


def parse_form_modal_id(value: str) -> int | None:
    """The answer card a submitted form belongs to."""
    return _modal_message_id(FORM_MODAL_PREFIX, value)


def file_modal_id(message_id: int) -> str:
    return f"{FILE_MODAL_PREFIX}{message_id}"


def parse_file_modal_id(value: str) -> int | None:
    """The answer card a submitted upload belongs to."""
    return _modal_message_id(FILE_MODAL_PREFIX, value)


def _field_item(field: dict, custom_id_: str) -> discord.ui.ModalItem:
    required = bool(field.get("required"))
    if field["type"] in ("short", "long"):
        short = field["type"] == "short"
        item = discord.ui.InputText(
            style=discord.InputTextStyle.short if short else discord.InputTextStyle.long,
            custom_id=custom_id_,
            placeholder=field.get("placeholder"),
            max_length=SHORT_VALUE_LIMIT if short else LONG_VALUE_LIMIT,
            required=required,
        )
        item.required = required  # older py-cord drops required=False in the constructor (see ui/ticket_views)
        return item
    options = list(enumerate(field["options"]))
    if field["type"] == "select":
        return discord.ui.Select(
            custom_id=custom_id_,
            placeholder=field.get("placeholder"),
            options=[discord.SelectOption(label=label, value=str(index)) for index, label in options],
            min_values=1 if required else 0,
            max_values=1,
            required=required,
        )
    return discord.ui.RadioGroup(
        custom_id=custom_id_,
        options=[discord.RadioGroupOption(label=label, value=str(index)) for index, label in options],
        required=required,
    )


def form_modal(spec: dict, message_id: int) -> discord.ui.DesignerModal:
    """The form behind "Fill in". Not stored (store=False): its submit is routed by custom id in the cog's
    on_interaction, so a restart between open and submit loses nothing."""
    return discord.ui.DesignerModal(
        *(
            discord.ui.Label(field["label"], _field_item(field, f"f{index}"))
            for index, field in enumerate(spec["fields"])
        ),
        title=spec["title"],
        custom_id=form_modal_id(message_id),
        store=False,
    )


def _submitted(components: Any) -> dict[str, Any]:
    """custom id -> submitted value(s) of a modal_submit payload (Label components wrap their item in "component")."""
    out: dict[str, Any] = {}

    def walk(items: Any) -> None:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("custom_id"):
                out[item["custom_id"]] = item["values"] if "values" in item else item.get("value")
            walk([item["component"]] if isinstance(item.get("component"), dict) else [])
            walk(item.get("components"))

    walk(components)
    return out


def form_answers(spec: dict, components: Any) -> list[tuple[str, str]]:
    """(field label, value) for every answered field, in form order. Member text that goes on the bot's card:
    one line each, no links or pings."""
    submitted = _submitted(components)
    answers: list[tuple[str, str]] = []
    for index, field in enumerate(spec.get("fields") or []):
        raw = submitted.get(f"f{index}")
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if field.get("type") in ("select", "radio"):
            try:
                raw = field["options"][int(raw)]
            except (KeyError, IndexError, TypeError, ValueError):
                raw = None
        value = ai_guard.defang(" ".join(str(raw or "").split()))
        if value:
            answers.append((field["label"], value))
    return answers


def file_modal(spec: dict, message_id: int, language: str | None) -> discord.ui.DesignerModal:
    """The upload behind "Upload": one file field of up to the spec's max_files. Routed by custom id like the form
    (store=False)."""
    what = spec.get("what") if spec.get("what") in FILE_WHATS else "any"
    upload = discord.ui.FileUpload(
        custom_id=UPLOAD_FIELD_ID, min_values=1, max_values=spec.get("max_files") or 1, required=True
    )
    return discord.ui.DesignerModal(
        discord.ui.Label(text(language, f"upload_field_{what}"), upload),
        title=text(language, f"upload_title_{what}"),
        custom_id=file_modal_id(message_id),
        store=False,
    )


def uploaded(spec: dict, data: Any) -> list[dict]:
    """The attachment payloads of a submitted upload, in upload order, at most the spec's max_files. The file field
    submits attachment ids; Discord sends the attachments themselves under resolved.attachments (as
    discord.ui.FileUpload.refresh_from_modal reads them)."""
    data = data if isinstance(data, dict) else {}
    ids = _submitted(data.get("components")).get(UPLOAD_FIELD_ID)
    resolved = (data.get("resolved") or {}).get("attachments") if isinstance(data.get("resolved"), dict) else None
    if not isinstance(ids, list) or not isinstance(resolved, dict):
        return []
    payloads = [resolved[str(item)] for item in ids if isinstance(resolved.get(str(item)), dict)]
    return payloads[: spec.get("max_files") or 1]


def upload_names(filenames: list[str]) -> list[str]:
    """The names the files are reposted under: plain enough for attachment:// URLs and the card (letters, digits,
    . _ -; no pings, links or markdown), short, and distinct. The end is kept, so is the extension."""
    names: list[str] = []
    for filename in filenames:
        base = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "").strip("_") or "file"
        name, n = base[-UPLOAD_NAME_LIMIT:], 0
        while name in names:
            n += 1
            name = f"{n}_{base[-(UPLOAD_NAME_LIMIT - 3):]}"
        names.append(name)
    return names


def upload_card(
    user_id: int, names: list[str], images: list[bool], color: discord.Colour | None = None
) -> discord.ui.DesignerView:
    """The requester's files, reposted by the bot: one line per file credits them (the record history, transcripts
    and the log parser read back), then screenshots in a gallery and other files as file components. The files go
    with the message as discord.File under the same names."""
    files = list(zip(names, images))
    gallery = [discord.MediaGalleryItem(f"attachment://{name}") for name, image in files if image]
    return discord.ui.DesignerView(
        discord.ui.Container(
            *(discord.ui.TextDisplay(upload_line(user_id, name)) for name in names),
            *([discord.ui.MediaGallery(*gallery)] if gallery else []),
            *(discord.ui.File(f"attachment://{name}") for name, image in files if not image),
            color=color,
        ),
        timeout=None,
    )


# --- The answer on the card, and reading it back ---


def pick_line(user_id: int, label: str) -> str:
    return f"{ANSWER_LINE_PREFIX}<@{user_id}>: **{label}**"


def form_lines(user_id: int, answers: list[tuple[str, str]], used: int = 0) -> list[str]:
    """One line per answered field. used: the card's text already; long values are trimmed so the card stays under
    the V2 text limit."""
    heads = [f"{ANSWER_LINE_PREFIX}<@{user_id}> · {label}: **" for label, _ in answers]
    budget = TEXT_LIMIT - used - sum(len(head) + 2 for head in heads)
    values = fit([value for _, value in answers], max(budget, 0))
    return [f"{head}{value}**" for head, value in zip(heads, values)]


def upload_line(user_id: int, name: str) -> str:
    return f"{ANSWER_LINE_PREFIX}<@{user_id}> uploaded **{name}**"


def step_failed_line(user_id: int, step: int, step_text: str, language: str | None) -> str:
    """Still broken on step (0-based): "-# ↳ <@id>: **Step 2 didn't work:** Delete the config folder"."""
    return f"{ANSWER_LINE_PREFIX}<@{user_id}>: **{text(language, 'step_failed').format(n=step + 1)}** {step_text}"


def steps_done_line(user_id: int, count: int, language: str | None) -> str:
    return f"{ANSWER_LINE_PREFIX}<@{user_id}>: **{text(language, 'steps_done').format(n=count)}**"


def _step_pattern(key: str, language: str, tail: str) -> re.Pattern:
    phrase = re.escape(TEXT[language][key]).replace(re.escape("{n}"), r"(\d+)")
    return re.compile(rf"^-# ↳ <@!?(\d+)>: \*\*{phrase}\*\*{tail}$")


# The steps outcome lines in every language, read back in English for the model: (pattern, readback).
_STEP_LINES = [
    *((_step_pattern("step_failed", language, r" (.+)"), "Step {n} didn't work: {step}") for language in TEXT),
    *((_step_pattern("steps_done", language, ""), "I did all the steps (1-{n}).") for language in TEXT),
]


def _read_step_line(line: str) -> tuple[int, str] | None:
    for pattern, readback in _STEP_LINES:
        if match := pattern.match(line):
            return int(match[1]), readback.format(n=match[2], step=match[3] if pattern.groups > 2 else "")
    return None


def read_progress(custom_ids: list[str | None]) -> tuple[int, str] | None:
    """(requester id, the steps they did) of a steps card still in progress, read off its buttons' custom ids; None
    when it isn't one or no step is done yet."""
    for value in custom_ids:
        parsed = parse_custom_id(value or "")
        if parsed is None or parsed[0] != "steps":
            continue
        done = split_step_index(parsed[2])[0]
        if done <= 0:
            return None
        return parsed[1], "I did step 1." if done == 1 else f"I did steps 1-{done}."
    return None


def read_upload(texts: list[str]) -> tuple[int, list[str]] | None:
    """(uploader id, file names) of a card's upload lines; None when it has none."""
    user_id: int | None = None
    names: list[str] = []
    for line in texts:
        if match := UPLOAD_LINE_RE.match(line or ""):
            user_id = user_id if user_id is not None else int(match[1])
            names.append(match[2])
    return (user_id, names) if user_id is not None else None


def read_answer(texts: list[str]) -> tuple[int, str] | None:
    """(user id, answer) recorded on a card: a pick's label, a form's "label: value" lines, or a steps outcome (in
    English). Upload lines aren't answers: the repost that carries the files is the uploader's turn (read_upload)."""
    user_id: int | None = None
    parts: list[str] = []
    for line in texts:
        if (step := _read_step_line(line or "")) is not None:
            return step
        if match := ANSWER_LINE_RE.match(line or ""):
            user_id = user_id if user_id is not None else int(match[1])
            parts.append(f"{match[2]}: {match[3]}" if match[2] else match[3])
    return (user_id, "\n".join(parts)) if user_id is not None else None


# --- Ask Bulma (the message command) ---


def asked_content(author_name: str, text: str) -> str:
    """The requester's turn when they Ask Bulma about a message: that message quoted as what they want help with."""
    head = f"Help me with this message from {ai_guard.safe_name(author_name)}:"
    return f"{head}\n{text}" if text else head


def asked_context_line(*, own_answer: bool) -> str:
    """The context line for Ask Bulma on someone else's message (the requester's own needs none)."""
    if own_answer:
        return "Ask Bulma: the requester asked you about one of your earlier messages, quoted in their last turn."
    return (
        "Ask Bulma: the requester asked you about a message another member wrote, quoted in their last turn. "
        "Help the requester with it."
    )
