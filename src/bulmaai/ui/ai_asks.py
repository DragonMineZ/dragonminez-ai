"""Bulma's answer controls. When her reply asks the requester something, the ask_requester tool attaches a control
spec (validated here) and the answer card carries buttons, a select or a "Fill in" form for it. Only the requester
may use them. The answer is recorded on the card as `-# ↳ ` lines, which history and transcripts read back as the
requester's turn; the spec itself lives in the ai_asks table (services/ai_asks), so clicks work after a restart.

Pure helpers only (spec validation, the card's controls, custom ids, the form modal, answer lines, readback); the
Discord wiring is in cogs/ai_tickets.py."""

import re
from typing import Any, Callable

import discord

from bulmaai.services import ai_guard
from bulmaai.ui.v2 import TEXT_LIMIT, fit

KINDS = ("choices", "select", "form")
FIELD_TYPES = ("short", "long", "select", "radio")
MAX_CHOICES = 5  # one row of buttons
MAX_SELECT_OPTIONS = 25
MAX_RADIO_OPTIONS = 10
MAX_FIELDS = 5
BUTTON_LABEL_LIMIT = 80
OPTION_LABEL_LIMIT = 100  # select/radio option labels and descriptions, placeholders
TITLE_LIMIT = 45  # the form's title and each field's label
SHORT_VALUE_LIMIT = 200
LONG_VALUE_LIMIT = 1000
ALLOWED_FLAGS = frozenset({"unknown_link"})  # a label is never a clickable link, so "latest.log" may stay

CUSTOM_ID_PREFIX = "ai_ask:"  # ai_ask:{kind}:{requester_id}:{index}
FORM_MODAL_PREFIX = "ai_ask_form:"  # ai_ask_form:{card message id}
ANSWER_LINE_PREFIX = "-# ↳ "
# A pick: "-# ↳ <@id>: **label**". A form field: "-# ↳ <@id> · Field label: **value**".
ANSWER_LINE_RE = re.compile(r"^-# ↳ <@!?(\d+)>(?: · (.+?))?: \*\*(.+)\*\*$", re.DOTALL)
SHOWN_NOTE = (
    "The requester sees this under your reply. Now write the reply itself: the question in full, so it reads fine "
    "without the controls."
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


# One validator per kind; later kinds (file, steps) add theirs here.
VALIDATORS: dict[str, Callable[[dict, Any, list[str]], dict | str]] = {
    "choices": _choices,
    "select": _select,
    "form": _form,
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
    return {"ok": True, "shown": spec, **extra, "note": SHOWN_NOTE}


# --- The card's controls ---


def custom_id(kind: str, requester_id: int, index: int = 0) -> str:
    return f"{CUSTOM_ID_PREFIX}{kind}:{requester_id}:{index}"


def parse_custom_id(value: str) -> tuple[str, int, int] | None:
    """(kind, requester id, index) of a control on an answer card."""
    if not value.startswith(CUSTOM_ID_PREFIX):
        return None
    try:
        kind, requester_id, index = value.removeprefix(CUSTOM_ID_PREFIX).split(":")
        return (kind, int(requester_id), int(index)) if kind in KINDS else None
    except ValueError:
        return None


def ask_rows(spec: dict, requester_id: int, language: str | None) -> list[discord.ui.ActionRow]:
    """The controls under the answer, handled by the cog's on_interaction via custom id (they survive restarts)."""
    kind = spec.get("kind")
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


# --- The form ---


def form_modal_id(message_id: int) -> str:
    return f"{FORM_MODAL_PREFIX}{message_id}"


def parse_form_modal_id(value: str) -> int | None:
    """The answer card a submitted form belongs to."""
    if not value.startswith(FORM_MODAL_PREFIX):
        return None
    try:
        return int(value.removeprefix(FORM_MODAL_PREFIX))
    except ValueError:
        return None


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


def read_answer(texts: list[str]) -> tuple[int, str] | None:
    """(user id, answer) recorded on a card: a pick's label, or a form's "label: value" lines."""
    user_id: int | None = None
    parts: list[str] = []
    for line in texts:
        if match := ANSWER_LINE_RE.match(line or ""):
            user_id = user_id if user_id is not None else int(match[1])
            parts.append(f"{match[2]}: {match[3]}" if match[2] else match[3])
    return (user_id, "\n".join(parts)) if user_id is not None else None
