"""The daily AI metrics card (cogs/ai_daily.py). The channel post is English; /ai-daily's ephemeral copy follows the
reader's language. The chart is an attached PNG shown in a media gallery."""

from datetime import date

import discord

from bulmaai.services.ai_daily import DailyData
from bulmaai.ui.v2 import card, fit, head
from bulmaai.utils.i18n import t, table

TEXT = table({
    "en": {
        "title": "## 📊 AI daily · {date}",
        "title_partial": "## 📊 AI daily · today so far",
        "date": "{weekday} {day} {month}",
        "weekdays": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
        "months": "Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec",
        "answers": "Answers",
        "delta": "{percent:+d}% vs 7-day avg",
        "handed_off": "handed off {percent}%",
        "second_opinion": "second opinion {percent}%",
        "tickets": "Tickets",
        "opened": "opened {count}",
        "closed": "closed {count}",
        "resolved": "resolved {percent}%",
        "cost": "Cost",
        "cycle": "cycle ${spend:.2f}",
        "cycle_budget": "cycle ${spend:.2f} of ${budget:.0f} (on pace for ${pace:.0f})",
        "cycle_budget_only": "cycle ${spend:.2f} of ${budget:.0f}",
        "haiku_pool": "Haiku pool {percent}%",
        "sonnet_pool": "Sonnet pool {percent}%",
        "waiting": "Waiting on staff",
        "suggestions": "{count} support info suggestions",
        "answers_to_fix": "{count} 👎 answers",
        "eval": "Last eval: {percent}% pass ({date})",
    },
    "es": {
        "title": "## 📊 IA diaria · {date}",
        "title_partial": "## 📊 IA diaria · hoy hasta ahora",
        "date": "{weekday} {day} {month}",
        "weekdays": "lun,mar,mié,jue,vie,sáb,dom",
        "months": "ene,feb,mar,abr,may,jun,jul,ago,sep,oct,nov,dic",
        "answers": "Respuestas",
        "delta": "{percent:+d}% vs promedio de 7 días",
        "handed_off": "pasadas al staff {percent}%",
        "second_opinion": "segunda opinión {percent}%",
        "tickets": "Tickets",
        "opened": "abiertos {count}",
        "closed": "cerrados {count}",
        "resolved": "resueltos {percent}%",
        "cost": "Costo",
        "cycle": "ciclo ${spend:.2f}",
        "cycle_budget": "ciclo ${spend:.2f} de ${budget:.0f} (al ritmo de ${pace:.0f})",
        "cycle_budget_only": "ciclo ${spend:.2f} de ${budget:.0f}",
        "haiku_pool": "pool Haiku {percent}%",
        "sonnet_pool": "pool Sonnet {percent}%",
        "waiting": "Esperando al staff",
        "suggestions": "{count} sugerencias de información de soporte",
        "answers_to_fix": "{count} respuestas 👎",
        "eval": "Última evaluación: {percent}% aprobadas ({date})",
    },
    "pt": {
        "title": "## 📊 IA diária · {date}",
        "title_partial": "## 📊 IA diária · hoje até agora",
        "date": "{weekday} {day} {month}",
        "weekdays": "seg,ter,qua,qui,sex,sáb,dom",
        "months": "jan,fev,mar,abr,mai,jun,jul,ago,set,out,nov,dez",
        "answers": "Respostas",
        "delta": "{percent:+d}% vs média de 7 dias",
        "handed_off": "passadas à staff {percent}%",
        "second_opinion": "segunda opinião {percent}%",
        "tickets": "Tickets",
        "opened": "abertos {count}",
        "closed": "fechados {count}",
        "resolved": "resolvidos {percent}%",
        "cost": "Custo",
        "cycle": "ciclo ${spend:.2f}",
        "cycle_budget": "ciclo ${spend:.2f} de ${budget:.0f} (no ritmo de ${pace:.0f})",
        "cycle_budget_only": "ciclo ${spend:.2f} de ${budget:.0f}",
        "haiku_pool": "pool Haiku {percent}%",
        "sonnet_pool": "pool Sonnet {percent}%",
        "waiting": "Aguardando a staff",
        "suggestions": "{count} sugestões de informação de suporte",
        "answers_to_fix": "{count} respostas 👎",
        "eval": "Última avaliação: {percent}% aprovadas ({date})",
    },
})


def short_date(day: date, language: str = "en") -> str:
    weekdays = t(TEXT, "weekdays", language).split(",")
    months = t(TEXT, "months", language).split(",")
    return t(TEXT, "date", language, weekday=weekdays[day.weekday()], day=day.day, month=months[day.month - 1])


def eval_text(percent: int, day: date, language: str = "en") -> str:
    return t(TEXT, "eval", language, percent=percent, date=f"{day.day} {t(TEXT, 'months', language).split(',')[day.month - 1]}")


def _line(label: str, pieces: list[str]) -> str:
    return f"**{label}** " + " · ".join(pieces) if pieces else ""


def daily_card(
    data: DailyData, *, eval_line: str | None = None, language: str = "en", image: str = "ai-daily.png"
) -> discord.ui.DesignerView:
    def say(key: str, **values) -> str:
        return t(TEXT, key, language, **values)

    today = data.today
    total = today.answers_total
    answers = [str(total)]
    # Not on an unfinished day: half a day against full-day averages always reads as a drop.
    if total and not data.partial and data.answers_delta is not None:
        answers[0] += f" ({say('delta', percent=round(data.answers_delta * 100))})"
    if total:
        answers.append(say("handed_off", percent=round(today.answers["handoff"] / total * 100)))
        answers.append(say("second_opinion", percent=round(today.escalated / total * 100)))
    if today.helpful + today.not_helpful:
        answers.append(f"👍 {today.helpful} 👎 {today.not_helpful}")

    tickets = []
    if today.tickets_opened or today.tickets_closed:
        tickets = [say("opened", count=today.tickets_opened), say("closed", count=today.tickets_closed)]
        if today.tickets_closed:
            tickets.append(say("resolved", percent=round(today.tickets_resolved / today.tickets_closed * 100)))

    cost = [f"${today.cost_total:.2f}"]
    pace = data.projected_spend
    if data.monthly_budget > 0:
        values = dict(spend=data.cycle_spend, budget=data.monthly_budget)
        cost.append(say("cycle_budget", pace=pace, **values) if pace is not None else say("cycle_budget_only", **values))
    else:
        cost.append(say("cycle", spend=data.cycle_spend))
    for pool, key in (("small", "haiku_pool"), ("big", "sonnet_pool")):
        limit = data.pool_limit.get(pool)
        if limit and limit > 0:
            cost.append(say(key, percent=round(data.pool_used.get(pool, 0) / limit * 100)))

    waiting = []
    if data.suggestions_waiting:
        waiting.append(say("suggestions", count=data.suggestions_waiting))
    if data.answers_unreviewed:
        waiting.append(say("answers_to_fix", count=data.answers_unreviewed))

    title = say("title_partial") if data.partial else say("title", date=short_date(data.day, language))
    body = "\n".join(
        line
        for line in (
            _line(say("answers"), answers),
            _line(say("tickets"), tickets),
            _line(say("cost"), cost),
            _line(say("waiting"), waiting),
        )
        if line
    )
    footer = f"-# {eval_line}" if eval_line else ""
    title, *rest = fit([title, body, footer])
    gallery = discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{image}"))
    return card(head(title), *rest[:1], gallery, *rest[1:])
