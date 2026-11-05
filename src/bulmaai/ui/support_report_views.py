"""The weekly staff support report card (cogs/support_report.py). The weekly post is English (a staff channel);
/support-report's ephemeral copy is in the reader's language. The AI-written sections are English either way."""

import discord

from bulmaai.services.ai_guard import defuse_mentions
from bulmaai.services.support_report import Insights, ReportData
from bulmaai.ui.v2 import card, facts, fit, head, trim
from bulmaai.utils.i18n import t, table

TEXT = table({
    "en": {
        "title": "## 📊 Weekly support report",
        "range": "-# {since} to {until} (UTC)",
        "footer": "-# AI-written sections can be wrong. Nothing here goes live on its own.",
        "spend_note": "-# Spend covers support and crash-log AI replies (not ticket summaries, translation or vision).",
        "closed": "Tickets closed",
        "solved": "Solved",
        "unresolved": "Unresolved",
        "ai_replies": "AI replies",
        "escalations": "Escalations",
        "handoffs": "Handoffs",
        "diagnoses": "Crash-log diagnoses",
        "spend": "Support AI spend (est.)",
        "feedback": "Answer feedback",
        "helpful": "👍 {up} · 👎 {down} ({percent}% helpful)",
        "tags": "**Top tags**",
        "themes": "**Recurring themes**",
        "gaps": "**Questions the AI couldn't answer**",
        "notes": "**Notes**",
        "no_ai": "-# No AI summary this time.",
    },
    "es": {
        "title": "## 📊 Reporte semanal de soporte",
        "range": "-# Del {since} al {until} (UTC)",
        "footer": "-# Las secciones escritas por IA pueden equivocarse. Nada de esto se publica por sí solo.",
        "spend_note": "-# El gasto cubre las respuestas de IA de soporte y de logs de crasheo (no resúmenes de tickets, "
        "traducciones ni visión).",
        "closed": "Tickets cerrados",
        "solved": "Resueltos",
        "unresolved": "Sin resolver",
        "ai_replies": "Respuestas de IA",
        "escalations": "Escalados",
        "handoffs": "Pases al staff",
        "diagnoses": "Diagnósticos de crasheos",
        "spend": "Gasto de IA de soporte (est.)",
        "feedback": "Opiniones sobre respuestas",
        "helpful": "👍 {up} · 👎 {down} ({percent}% útiles)",
        "tags": "**Etiquetas principales**",
        "themes": "**Temas recurrentes**",
        "gaps": "**Preguntas que la IA no pudo responder**",
        "notes": "**Notas**",
        "no_ai": "-# Esta vez no hay resumen de IA.",
    },
    "pt": {
        "title": "## 📊 Relatório semanal de suporte",
        "range": "-# De {since} a {until} (UTC)",
        "footer": "-# As seções escritas por IA podem errar. Nada aqui é publicado sozinho.",
        "spend_note": "-# O gasto cobre as respostas de IA de suporte e de logs de crash (não resumos de tickets, "
        "traduções nem visão).",
        "closed": "Tickets fechados",
        "solved": "Resolvidos",
        "unresolved": "Não resolvidos",
        "ai_replies": "Respostas da IA",
        "escalations": "Escalonamentos",
        "handoffs": "Passados à staff",
        "diagnoses": "Diagnósticos de crash",
        "spend": "Gasto de IA de suporte (est.)",
        "feedback": "Avaliações das respostas",
        "helpful": "👍 {up} · 👎 {down} ({percent}% úteis)",
        "tags": "**Tags principais**",
        "themes": "**Temas recorrentes**",
        "gaps": "**Perguntas que a IA não conseguiu responder**",
        "notes": "**Notas**",
        "no_ai": "-# Sem resumo de IA desta vez.",
    },
})


def _money(amount: float) -> str:
    return f"${amount:.2f}"


def _spend(data: ReportData) -> str:
    split = ", ".join(f"{item.model} {_money(item.cost)}" for item in data.usage if item.cost)
    return _money(data.spend) + (f" ({split})" if split else "")


def support_report_card(data: ReportData, insights: Insights | None, language: str = "en") -> discord.ui.DesignerView:
    def say(key: str, **values) -> str:
        return t(TEXT, key, language, **values)

    last_day = data.until - (data.until - data.since) / 7
    title = f"{say('title')}\n{say('range', since=f'{data.since:%d %b}', until=f'{last_day:%d %b %Y}')}"
    numbers = [
        facts(
            (say("closed"), data.tickets_closed),
            (say("solved"), data.tickets_resolved),
            (say("unresolved"), data.tickets_unresolved),
        ),
        facts(
            (say("ai_replies"), data.ai_replies),
            (say("escalations"), data.escalations),
            (say("handoffs"), data.handoffs),
            (say("diagnoses"), data.log_diagnoses),
        ),
        facts((say("spend"), _spend(data))),
    ]
    if data.ratings:  # 👍/👎 under the week's answers; no line without any
        rated = say("helpful", up=data.helpful, down=data.not_helpful, percent=data.helpful_percent)
        numbers.insert(2, facts((say("feedback"), rated)))
    if data.top_tags:
        numbers.append(f"{say('tags')} " + ", ".join(f"{defuse_mentions(tag)} ({count})" for tag, count in data.top_tags))
    parts = ["\n".join(numbers), say("spend_note")]
    if insights is not None:
        if insights.themes:
            parts.append(f"{say('themes')}\n" + "\n".join(f"- {trim(theme, 140)}" for theme in insights.themes))
        if insights.gaps:
            lines = (
                f"- {trim(question, 200)} → {trim(page, 80)}" if page else f"- {trim(question, 200)}"
                for question, page in insights.gaps
            )
            parts.append(f"{say('gaps')}\n" + "\n".join(lines))
        if insights.notes:
            parts.append(f"{say('notes')}\n" + trim(insights.notes, 500))
    else:
        parts.append(say("no_ai"))
    parts.append(say("footer"))
    title, *body = fit([title, *parts])
    return card(head(title), *body)
