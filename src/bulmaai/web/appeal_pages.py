"""HTML for the appeals website (web/routes_appeals.py): English, Latin American Spanish and Brazilian Portuguese,
picked from Accept-Language. Every value put into a page goes through html.escape."""

from datetime import datetime
from html import escape

LANGUAGES = ("en", "es", "pt")

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "title": "DragonMineZ appeals",
        "intro": "Banned or timed out on the DragonMineZ Discord server? You can ask staff to review it here.",
        "privacy": "Log in with Discord so we know who you are. We only see your Discord username and ID.",
        "login": "Log in with Discord",
        "logout": "Log out",
        "not_setup": "Appeals are not set up yet. Please try again later.",
        "closed": "Appeals are closed right now. Please try again later.",
        "login_failed": "Discord login failed or expired. Please try again.",
        "try_again": "Try again",
        "error": "Something went wrong on our side. Please try again later.",
        "rate": "Too many requests. Please wait a few minutes and try again.",
        "expired": "Your session expired. Please reload the page and try again.",
        "nothing": "You aren't banned or timed out on the DragonMineZ server, so there's nothing to appeal.",
        "join": "Join the server",
        "banned": "You are banned from the DragonMineZ server.",
        "timed_out": "You are timed out on the DragonMineZ server until {until}.",
        "reason": "Reason",
        "date": "Date",
        "no_reason": "No reason given",
        "why": "Why should it be lifted?",
        "extra": "Anything else staff should know? (optional)",
        "send": "Send appeal",
        "why_short": "Please explain in at least {min} characters why it should be lifted.",
        "too_long": "Your text is too long.",
        "pending": "Your appeal was sent on {date}. Staff will review it.",
        "check_back": "The bot can't message you, so check this page for staff's answer.",
        "you_wrote": "What you wrote",
        "question": "Staff asked you a question:",
        "your_answer": "Your answer",
        "send_answer": "Send answer",
        "answered": "You answered staff's question. They'll take it into account.",
        "answer_empty": "Please write an answer.",
        "accepted": "Your appeal was accepted.",
        "accepted_ban": "You can join the server again.",
        "accepted_timeout": "Your timeout was removed.",
        "denied": "Your appeal was denied.",
        "staff_reason": "Staff's reason",
        "again_on": "You can appeal again on {date}.",
    },
    "es": {
        "title": "Apelaciones de DragonMineZ",
        "intro": "¿Te banearon o te silenciaron en el servidor de Discord de DragonMineZ? Aquí puedes pedirle al staff que lo revise.",
        "privacy": "Inicia sesión con Discord para que sepamos quién eres. Solo vemos tu nombre de usuario y tu ID de Discord.",
        "login": "Iniciar sesión con Discord",
        "logout": "Cerrar sesión",
        "not_setup": "Las apelaciones todavía no están configuradas. Inténtalo más tarde.",
        "closed": "Las apelaciones están cerradas por ahora. Inténtalo más tarde.",
        "login_failed": "El inicio de sesión con Discord falló o expiró. Inténtalo de nuevo.",
        "try_again": "Intentar de nuevo",
        "error": "Algo salió mal de nuestro lado. Inténtalo más tarde.",
        "rate": "Demasiadas solicitudes. Espera unos minutos e inténtalo de nuevo.",
        "expired": "Tu sesión expiró. Recarga la página e inténtalo de nuevo.",
        "nothing": "No estás baneado ni silenciado en el servidor de DragonMineZ, así que no hay nada que apelar.",
        "join": "Unirse al servidor",
        "banned": "Estás baneado del servidor de DragonMineZ.",
        "timed_out": "Estás silenciado en el servidor de DragonMineZ hasta el {until}.",
        "reason": "Motivo",
        "date": "Fecha",
        "no_reason": "Sin motivo indicado",
        "why": "¿Por qué debería levantarse?",
        "extra": "¿Algo más que el staff deba saber? (opcional)",
        "send": "Enviar apelación",
        "why_short": "Explica en al menos {min} caracteres por qué debería levantarse.",
        "too_long": "Tu texto es demasiado largo.",
        "pending": "Tu apelación se envió el {date}. El staff la va a revisar.",
        "check_back": "El bot no puede escribirte, así que revisa esta página para ver la respuesta del staff.",
        "you_wrote": "Lo que escribiste",
        "question": "El staff te hizo una pregunta:",
        "your_answer": "Tu respuesta",
        "send_answer": "Enviar respuesta",
        "answered": "Respondiste la pregunta del staff. La van a tener en cuenta.",
        "answer_empty": "Escribe una respuesta.",
        "accepted": "Tu apelación fue aceptada.",
        "accepted_ban": "Puedes volver a unirte al servidor.",
        "accepted_timeout": "Se quitó tu silencio.",
        "denied": "Tu apelación fue rechazada.",
        "staff_reason": "Motivo del staff",
        "again_on": "Puedes volver a apelar el {date}.",
    },
    "pt": {
        "title": "Apelações do DragonMineZ",
        "intro": "Foi banido ou silenciado no servidor do Discord do DragonMineZ? Aqui você pode pedir para a staff revisar.",
        "privacy": "Entre com o Discord para sabermos quem você é. Só vemos seu nome de usuário e seu ID do Discord.",
        "login": "Entrar com o Discord",
        "logout": "Sair",
        "not_setup": "As apelações ainda não estão configuradas. Tente novamente mais tarde.",
        "closed": "As apelações estão fechadas no momento. Tente novamente mais tarde.",
        "login_failed": "O login com o Discord falhou ou expirou. Tente novamente.",
        "try_again": "Tentar de novo",
        "error": "Algo deu errado do nosso lado. Tente novamente mais tarde.",
        "rate": "Muitas solicitações. Espere alguns minutos e tente novamente.",
        "expired": "Sua sessão expirou. Recarregue a página e tente novamente.",
        "nothing": "Você não está banido nem silenciado no servidor do DragonMineZ, então não há nada para apelar.",
        "join": "Entrar no servidor",
        "banned": "Você está banido do servidor do DragonMineZ.",
        "timed_out": "Você está silenciado no servidor do DragonMineZ até {until}.",
        "reason": "Motivo",
        "date": "Data",
        "no_reason": "Nenhum motivo informado",
        "why": "Por que deveria ser retirado?",
        "extra": "Mais alguma coisa que a staff deva saber? (opcional)",
        "send": "Enviar apelação",
        "why_short": "Explique em pelo menos {min} caracteres por que deveria ser retirado.",
        "too_long": "Seu texto é longo demais.",
        "pending": "Sua apelação foi enviada em {date}. A staff vai analisar.",
        "check_back": "O bot não consegue te mandar mensagem, então confira esta página para ver a resposta da staff.",
        "you_wrote": "O que você escreveu",
        "question": "A staff te fez uma pergunta:",
        "your_answer": "Sua resposta",
        "send_answer": "Enviar resposta",
        "answered": "Você respondeu a pergunta da staff. Ela vai levar isso em conta.",
        "answer_empty": "Escreva uma resposta.",
        "accepted": "Sua apelação foi aceita.",
        "accepted_ban": "Você pode entrar no servidor de novo.",
        "accepted_timeout": "Seu silenciamento foi removido.",
        "denied": "Sua apelação foi recusada.",
        "staff_reason": "Motivo da staff",
        "again_on": "Você pode apelar de novo em {date}.",
    },
}

STYLE = """
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e6e6;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:640px;margin:0 auto;padding:32px 16px}
h1{font-size:1.4rem;margin:0 0 16px}
.box{background:#181b22;border:1px solid #2a2f3a;border-radius:10px;padding:16px;margin:0 0 16px}
.muted{color:#9aa3b2;font-size:.9rem}
.error{background:#3a1d1f;border-color:#6b2a2f}
.ok{background:#1b3324;border-color:#2c6b43}
blockquote{margin:8px 0;padding:8px 12px;border-left:3px solid #4b5468;background:#13161c;white-space:pre-wrap;word-wrap:break-word}
label{display:block;font-weight:600;margin:12px 0 6px}
textarea{width:100%;min-height:140px;padding:10px;border-radius:8px;border:1px solid #2a2f3a;background:#0f1115;color:inherit;font:inherit}
textarea.small{min-height:80px}
.button,button{display:inline-block;margin-top:12px;padding:10px 18px;border:0;border-radius:8px;background:#5865f2;color:#fff;font:inherit;font-weight:600;text-decoration:none;cursor:pointer}
form.inline{display:inline}
.link{background:none;color:#9aa3b2;padding:0;margin:0;font-weight:400;text-decoration:underline}
dl{margin:0}dt{font-weight:600}dd{margin:0 0 8px}
""".strip()


def pick_language(header: str | None) -> str:
    """The best of en/es/pt in Accept-Language by q value (ties keep the first); English by default."""
    best, best_q = "en", -1.0
    for part in (header or "").split(","):
        tag, _, params = part.strip().partition(";")
        base = tag.strip().lower().split("-")[0]
        q = 1.0
        params = params.strip()
        if params.startswith("q="):
            try:
                q = float(params[2:])
            except ValueError:
                q = 0.0
        if base in TEXT and q > best_q:
            best, best_q = base, q
    return best


def date(when: datetime | None) -> str:
    return when.strftime("%Y-%m-%d") if when else "?"


def date_time(when: datetime | None) -> str:
    return when.strftime("%Y-%m-%d %H:%M UTC") if when else "?"


def page(lang: str, body: str, *, logout_token: str | None = None) -> str:
    t = TEXT[lang]
    logout = (
        f'<form class=inline method=post action="/appeal/logout"><input type=hidden name=csrf value="{escape(logout_token)}">'
        f'<button class="link" type=submit>{escape(t["logout"])}</button></form>'
        if logout_token
        else ""
    )
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f'<title>{escape(t["title"])}</title><link rel="stylesheet" href="/appeal/style.css"></head>'
        f'<body><main><h1>{escape(t["title"])}</h1>{body}<p class="muted">{logout}</p></main></body></html>'
    )


def box(text: str, kind: str = "") -> str:
    """text must already be HTML."""
    return f'<div class="box {kind}">{text}</div>'


def para(text: str) -> str:
    return f"<p>{escape(text)}</p>"


def quote(text: str | None) -> str:
    return f"<blockquote>{escape(text or '')}</blockquote>"


def link_button(href: str, label: str) -> str:
    return f'<a class="button" href="{escape(href)}" rel="noopener noreferrer">{escape(label)}</a>'


def landing(lang: str) -> str:
    t = TEXT[lang]
    return page(lang, box(para(t["intro"]) + f'<p class="muted">{escape(t["privacy"])}</p>' + link_button("/appeal/login", t["login"])))


def message(lang: str, key: str, *, retry: bool = False, kind: str = "") -> str:
    t = TEXT[lang]
    extra = link_button("/appeal/", t["try_again"]) if retry else ""
    return page(lang, box(para(t[key]) + extra, kind))


def nothing(lang: str, invite: str, csrf: str) -> str:
    t = TEXT[lang]
    join = link_button(invite, t["join"]) if invite else ""
    return page(lang, box(para(t["nothing"]) + join), logout_token=csrf)


def punishment_box(lang: str, kind: str, reason: str | None, at: datetime | None, until: datetime | None) -> str:
    t = TEXT[lang]
    head = t["banned"] if kind == "ban" else t["timed_out"].format(until=date_time(until))
    facts = f'<dl><dt>{escape(t["reason"])}</dt><dd>{escape(reason or t["no_reason"])}</dd>'
    if at:
        facts += f'<dt>{escape(t["date"])}</dt><dd>{escape(date(at))}</dd>'
    return box(para(head) + facts + "</dl>")


def form(
    lang: str,
    *,
    csrf: str,
    kind: str,
    reason: str | None,
    at: datetime | None,
    until: datetime | None,
    why_max: int,
    extra_max: int,
    error: str | None = None,
    why: str = "",
    extra: str = "",
) -> str:
    t = TEXT[lang]
    body = punishment_box(lang, kind, reason, at, until)
    if error:
        body += box(para(error), "error")
    body += (
        '<form method="post" action="/appeal/submit">'
        f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
        f'<label for="why">{escape(t["why"])}</label>'
        f'<textarea id="why" name="why" required maxlength="{why_max}">{escape(why)}</textarea>'
        f'<label for="extra">{escape(t["extra"])}</label>'
        f'<textarea id="extra" name="extra" class="small" maxlength="{extra_max}">{escape(extra)}</textarea>'
        f'<button type="submit">{escape(t["send"])}</button></form>'
    )
    return page(lang, body, logout_token=csrf)


def status(lang: str, appeal, *, csrf: str, invite: str, reply_max: int, again_on: datetime | None, error: str | None = None) -> str:
    """Pending (maybe with a question to answer), accepted or denied."""
    t = TEXT[lang]
    if appeal.outcome == "accepted":
        done = t["accepted_ban"] if appeal.kind == "ban" else t["accepted_timeout"]
        join = link_button(invite, t["join"]) if invite and appeal.kind == "ban" else ""
        return page(lang, box(para(t["accepted"]) + para(done) + join, "ok"), logout_token=csrf)
    if appeal.outcome == "denied":
        body = para(t["denied"])
        if appeal.staff_note:
            body += f'<p><strong>{escape(t["staff_reason"])}</strong></p>' + quote(appeal.staff_note)
        if again_on:
            body += para(t["again_on"].format(date=date(again_on)))
        return page(lang, box(body, "error"), logout_token=csrf)
    body = box(para(t["pending"].format(date=date(appeal.created_at))) + f'<p class="muted">{escape(t["check_back"])}</p>')
    if appeal.question:
        qa = f'<p><strong>{escape(t["question"])}</strong></p>' + quote(appeal.question)
        if appeal.user_reply:
            qa += f'<p><strong>{escape(t["your_answer"])}</strong></p>' + quote(appeal.user_reply) + para(t["answered"])
        else:
            if error:
                qa += box(para(error), "error")
            qa += (
                '<form method="post" action="/appeal/reply">'
                f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
                f'<input type="hidden" name="appeal" value="{int(appeal.id)}">'
                f'<label for="reply">{escape(t["your_answer"])}</label>'
                f'<textarea id="reply" name="reply" required maxlength="{reply_max}"></textarea>'
                f'<button type="submit">{escape(t["send_answer"])}</button></form>'
            )
        body += box(qa)
    wrote = quote(appeal.answers.get("why"))
    if appeal.answers.get("extra"):
        wrote += quote(appeal.answers.get("extra"))
    body += box(f'<p><strong>{escape(t["you_wrote"])}</strong></p>' + wrote)
    return page(lang, body, logout_token=csrf)
