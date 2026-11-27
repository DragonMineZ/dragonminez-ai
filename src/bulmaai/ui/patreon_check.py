"""The ticket panel's Patreon check: picking "Patreon & Beta Access" first shows the member, ephemerally, what
Patreon says about them and the usual fix; "Still need help" opens the ticket form as before."""

import discord

from bulmaai.services.patreon_members import PatreonMember, is_entitled
from bulmaai.ui.v2 import card
from bulmaai.utils.i18n import t, table

PATREON_COLOUR = discord.Colour.from_rgb(255, 85, 0)

HAS_ROLE = "has_role"
ENTITLED_NO_ROLE = "entitled_no_role"
NOT_ENTITLED = "not_entitled"
NOT_FOUND = "not_found"

TEXT = table({
    "en": {
        "title": "### 🔎 Quick Patreon check",
        HAS_ROLE: "You already have the Patreon role. For beta access, run `/patreon beta-access` with your "
        "Minecraft username, or press **Verify & Get Beta Access** on the welcome DM.",
        ENTITLED_NO_ROLE: "Patreon shows you as an active patron ({tiers}), so you're entitled to the role. The "
        "Patreon bot gives it and it usually shows up within a few minutes. Still missing after an hour? On Patreon "
        "open **Settings → Connected apps → Discord**, disconnect and connect again.",
        NOT_ENTITLED: "Your Discord account is connected on Patreon, but Patreon says **{status}**, so the role "
        "isn't due right now. It comes back on its own once a payment on an eligible tier goes through.",
        NOT_FOUND: "No Patreon account has this Discord account connected, so the Patreon bot can't give you the "
        "role. On Patreon open **Settings → Connected apps** and connect Discord (this account); the role follows "
        "within a few minutes. If you connected a different Discord account, that one has the role.",
        "footer": "-# Didn't fix it? Open a ticket and the team will check.",
        "button": "Still need help",
        "tiers_none": "no tier listed",
        "active_patron": "active, but on a tier without the role",
        "declined_patron": "your last payment was declined",
        "former_patron": "you're a former patron",
    },
    "es": {
        "title": "### 🔎 Revisión rápida de Patreon",
        HAS_ROLE: "Ya tienes el rol de Patreon. Para el acceso beta, usa `/patreon beta-access` con tu usuario de "
        "Minecraft, o pulsa **Verify & Get Beta Access** en el MD de bienvenida.",
        ENTITLED_NO_ROLE: "Patreon te muestra como patrocinador activo ({tiers}), así que te corresponde el rol. Lo "
        "da el bot de Patreon y suele aparecer en unos minutos. ¿Sigue sin aparecer después de una hora? En Patreon "
        "abre **Configuración → Apps conectadas → Discord**, desconéctalo y vuelve a conectarlo.",
        NOT_ENTITLED: "Tu cuenta de Discord está conectada en Patreon, pero Patreon dice **{status}**, así que el rol "
        "no corresponde ahora. Vuelve solo cuando se procese un pago en un nivel que lo incluya.",
        NOT_FOUND: "Ninguna cuenta de Patreon tiene esta cuenta de Discord conectada, así que el bot de Patreon no "
        "puede darte el rol. En Patreon abre **Configuración → Apps conectadas** y conecta Discord (esta cuenta); el "
        "rol llega en unos minutos. Si conectaste otra cuenta de Discord, esa es la que tiene el rol.",
        "footer": "-# ¿No se arregló? Abre un ticket y el equipo lo revisará.",
        "button": "Aún necesito ayuda",
        "tiers_none": "sin nivel",
        "active_patron": "activo, pero en un nivel sin el rol",
        "declined_patron": "tu último pago fue rechazado",
        "former_patron": "eres un ex patrocinador",
    },
    "pt": {
        "title": "### 🔎 Verificação rápida do Patreon",
        HAS_ROLE: "Você já tem o cargo do Patreon. Para o acesso beta, use `/patreon beta-access` com seu usuário do "
        "Minecraft, ou aperte **Verify & Get Beta Access** na DM de boas-vindas.",
        ENTITLED_NO_ROLE: "O Patreon mostra você como apoiador ativo ({tiers}), então o cargo é seu. Quem dá o cargo "
        "é o bot do Patreon e ele costuma aparecer em poucos minutos. Ainda não apareceu depois de uma hora? No "
        "Patreon abra **Configurações → Apps conectados → Discord**, desconecte e conecte de novo.",
        NOT_ENTITLED: "Sua conta do Discord está conectada no Patreon, mas o Patreon diz **{status}**, então o cargo "
        "não vale agora. Ele volta sozinho quando um pagamento num nível que o inclui for aprovado.",
        NOT_FOUND: "Nenhuma conta do Patreon tem esta conta do Discord conectada, então o bot do Patreon não consegue "
        "te dar o cargo. No Patreon abra **Configurações → Apps conectados** e conecte o Discord (esta conta); o "
        "cargo chega em poucos minutos. Se você conectou outra conta do Discord, é ela que tem o cargo.",
        "footer": "-# Não resolveu? Abra um ticket e a equipe vai verificar.",
        "button": "Ainda preciso de ajuda",
        "tiers_none": "sem nível",
        "active_patron": "ativo, mas num nível sem o cargo",
        "declined_patron": "seu último pagamento foi recusado",
        "former_patron": "você é um ex-apoiador",
    },
})


def diagnose(*, has_role: bool, member: PatreonMember | None, eligible_tier_ids) -> str:
    if has_role:
        return HAS_ROLE
    if member is None:
        return NOT_FOUND
    return ENTITLED_NO_ROLE if is_entitled(member, eligible_tier_ids) else NOT_ENTITLED


def check_card(diagnosis: str, member: PatreonMember | None, language: str, form_custom_id: str):
    tiers = ", ".join(member.tier_titles) if member and member.tier_titles else t(TEXT, "tiers_none", language)
    status = (member.patron_status if member else None) or ""
    status_text = t(TEXT, status, language) if status in TEXT["en"] else status
    body = t(TEXT, diagnosis, language, tiers=tiers, status=status_text)
    button = discord.ui.Button(
        label=t(TEXT, "button", language), emoji="🎫", style=discord.ButtonStyle.secondary, custom_id=form_custom_id
    )
    return card(t(TEXT, "title", language), body, t(TEXT, "footer", language), color=PATREON_COLOUR, buttons=[button])
