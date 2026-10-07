"""Throwaway: /v2preview posts Components V2 mockups (case cards, automod alerts, replies) with fake data so
Bruno can pick which features ship. Delete this cog (and its DEFAULT_INITIAL_EXTENSIONS entry) once decided."""

from datetime import timedelta

import discord
from discord.ext import commands

from bulmaai.utils.permissions import is_bruno

NO_PINGS = discord.AllowedMentions.none()
ui = discord.ui


def _ts(delta: timedelta = timedelta(), style: str = "R") -> str:
    return discord.utils.format_dt(discord.utils.utcnow() + delta, style)


def _label(text: str) -> ui.TextDisplay:
    return ui.TextDisplay(f"-# ── **{text}**")


async def _preview_only(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("Preview only, nothing happened.", ephemeral=True)


def _button(label: str, style=discord.ButtonStyle.secondary, emoji: str | None = None, callback=_preview_only):
    button = ui.Button(label=label, style=style, emoji=emoji)
    button.callback = callback
    return button


class Fake:
    """Placeholder people and numbers; the invoker plays the punished user, the bot plays the moderator."""

    def __init__(self, ctx: discord.ApplicationContext):
        self.user = ctx.author
        self.mod = ctx.bot.user
        self.name = discord.utils.escape_markdown(ctx.author.name)
        self.avatar = ctx.author.display_avatar.url
        self.channel = ctx.channel.mention
        self.jump = f"https://discord.com/channels/{ctx.guild_id}/{ctx.channel_id}"
        self.images = [f"https://cdn.discordapp.com/embed/avatars/{i}.png" for i in range(4)]


# --- case cards ---------------------------------------------------------------------------------------------


def _case_header(f: Fake, case_id: int, emoji: str, action: str, *, struck: bool = False) -> str:
    action_text = f"~~{action}~~" if struck else action
    return f"### {emoji} Case {case_id} · {action_text} · {f.name}"


def _case_body(f: Fake, reason: str, length: str | None = None) -> str:
    line = f"**User** {f.user.mention}　**Moderator** {f.mod.mention}"
    if length:
        line += f"　**Length** {length}"
    return f"{line}\n**Reason** {reason}"


def _case_footer(f: Fake) -> ui.TextDisplay:
    return ui.TextDisplay(f"-# ID: {f.user.id} · {_ts(style='f')}")


def case_base(f: Fake) -> ui.DesignerView:
    return ui.DesignerView(
        _label("A · Case card, base (Dyno layout in V2)"),
        ui.Container(
            ui.Section(
                ui.TextDisplay(f"{_case_header(f, 274, '🔇', 'Mute')}\n{_case_body(f, 'Warning threshold reached (2 warnings)', '1 day')}"),
                accessory=ui.Thumbnail(f.avatar),
            ),
            _case_footer(f),
            color=discord.Color.orange(),
        ),
    )


def case_context(f: Fake) -> ui.DesignerView:
    return ui.DesignerView(
        _label("B · + Context line"),
        ui.Container(
            ui.Section(
                ui.TextDisplay(f"{_case_header(f, 273, '⚠️', 'Warn')}\n{_case_body(f, 'stop instigating weird drama')}"),
                accessory=ui.Thumbnail(f.avatar),
            ),
            ui.Separator(spacing=discord.SeparatorSpacingSize.small),
            ui.TextDisplay("-# 📊 3rd case · `▰▰` 2/2 warns in 7d · account 12 days old · joined 3 days ago"),
            _case_footer(f),
            color=discord.Color.gold(),
        ),
    )


def case_buttons(f: Fake) -> ui.DesignerView:
    return ui.DesignerView(
        _label("C · + Action buttons (staff-tier checked on click)"),
        ui.Container(
            ui.Section(
                ui.TextDisplay(f"{_case_header(f, 274, '🔇', 'Mute')}\n{_case_body(f, 'Spamming invites', '1 day')}"),
                accessory=ui.Thumbnail(f.avatar),
            ),
            _case_footer(f),
            ui.ActionRow(
                _button("Edit reason", emoji="✏️"),
                _button("Remove timeout", discord.ButtonStyle.success, "🔊"),
                _button("History", emoji="📜"),
            ),
            color=discord.Color.orange(),
        ),
    )


def case_expiry(f: Fake) -> list[ui.DesignerView]:
    active = ui.DesignerView(
        _label("D1 · + Live expiry, while active"),
        ui.Container(
            ui.Section(
                ui.TextDisplay(f"{_case_header(f, 275, '🔨', 'Tempban')}\n{_case_body(f, 'Scam links', '7 days')}\n⏳ Ends {_ts(timedelta(days=7))}"),
                accessory=ui.Thumbnail(f.avatar),
            ),
            _case_footer(f),
            color=discord.Color.red(),
        ),
    )
    lifted = ui.DesignerView(
        _label("D2 · + Live expiry, after it ended or was undone (card edits itself, buttons go away)"),
        ui.Container(
            ui.Section(
                ui.TextDisplay(
                    f"{_case_header(f, 275, '🔨', 'Tempban', struck=True)}\n{_case_body(f, 'Scam links', '7 days')}\n"
                    f"✅ Lifted {_ts(timedelta(minutes=-3))} by {f.mod.mention} · *appeal accepted*"
                ),
                accessory=ui.Thumbnail(f.avatar),
            ),
            _case_footer(f),
            color=discord.Color.dark_grey(),
        ),
    )
    return [active, lifted]


def case_linked(f: Fake) -> list[ui.DesignerView]:
    merged = ui.DesignerView(
        _label("E1 · Linked escalation, as one card"),
        ui.Container(
            ui.Section(
                ui.TextDisplay(f"{_case_header(f, 273, '⚠️', 'Warn')}\n{_case_body(f, 'stop instigating weird drama')}"),
                accessory=ui.Thumbnail(f.avatar),
            ),
            ui.Separator(),
            ui.TextDisplay(
                f"⚡ **Warn ladder → Case 274 · Mute · 1 day**\n-# `▰▰` 2/2 warns in 7d · ⏳ ends {_ts(timedelta(days=1))}"
            ),
            _case_footer(f),
            color=discord.Color.orange(),
        ),
    )
    split = ui.DesignerView(
        _label("E2 · Linked escalation, as its own card pointing back"),
        ui.Container(
            ui.Section(
                ui.TextDisplay(f"{_case_header(f, 274, '🔇', 'Mute')}\n{_case_body(f, 'Warning threshold reached (2 warnings)', '1 day')}"),
                accessory=ui.Thumbnail(f.avatar),
            ),
            ui.TextDisplay("-# ⚡ Triggered by case #273 (warn ladder: 2/2 in 7d)"),
            _case_footer(f),
            color=discord.Color.orange(),
        ),
    )
    return [merged, split]


def case_everything(f: Fake) -> ui.DesignerView:
    return ui.DesignerView(
        _label("F · Everything together"),
        ui.Container(
            ui.Section(
                ui.TextDisplay(
                    f"{_case_header(f, 274, '🔇', 'Mute')}\n{_case_body(f, 'Warning threshold reached (2 warnings)', '1 day')}\n"
                    f"⏳ Ends {_ts(timedelta(days=1))}"
                ),
                accessory=ui.Thumbnail(f.avatar),
            ),
            ui.TextDisplay("-# ⚡ Triggered by case #273 · 📊 3rd case · account 12 days old · joined 3 days ago"),
            _case_footer(f),
            ui.ActionRow(
                _button("Edit reason", emoji="✏️"),
                _button("Remove timeout", discord.ButtonStyle.success, "🔊"),
                _button("History", emoji="📜"),
            ),
            color=discord.Color.orange(),
        ),
    )


# --- automod alerts -----------------------------------------------------------------------------------------


def _alert_container(f: Fake, *, with_buttons: bool, toggle: ui.Button | None = None) -> ui.Container:
    items: list[ui.Item] = [
        ui.Section(
            ui.TextDisplay(
                "### 🚨 Moderation Alert · image burst\n8 images across 2 messages in 20s\n"
                f"**Action** timeout 7d　**Deleted** 1/2　**Purged** 1\n**Channels** {f.channel}"
            ),
            accessory=ui.Thumbnail(f.avatar),
        ),
        ui.Separator(),
        ui.TextDisplay(
            f"👤 **{f.name}** (`{f.user.id}`)\n"
            "-# Account 2 years old · joined 3 days ago · 0 prior cases · `▱▱` 0/2 warns"
        ),
        ui.TextDisplay(
            "> 🎰 MrBeast launched his crypto casino! Use code **EPIC** for a $2,500 bonus, hurry it ends in 1h…\n"
            f"-# [Jump to message]({f.jump}) · 4 attachments"
        ),
        ui.MediaGallery(*(discord.MediaGalleryItem(url) for url in f.images)),
        ui.TextDisplay("-# 🧠 Auto-learned 4 images · detected " + _ts(timedelta(minutes=-1))),
    ]
    if with_buttons:
        items.append(
            ui.ActionRow(
                _button("Remove timeout", discord.ButtonStyle.success),
                _button("Ban", discord.ButtonStyle.danger),
                _button("False positive"),
            )
        )
    if toggle is not None:
        items.append(ui.ActionRow(toggle))
    return ui.Container(*items, color=discord.Color.red())


def alert_full(f: Fake) -> ui.DesignerView:
    return ui.DesignerView(
        _label("G · Alert with user snapshot, quoted message, image gallery (auto-learned, so no Delete & learn)"),
        _alert_container(f, with_buttons=True),
    )


def alert_collapsed(f: Fake) -> ui.DesignerView:
    """Show details / Hide actually toggle, so you can feel it."""

    def collapsed() -> ui.DesignerView:
        show = _button("Show details", emoji="🔽", callback=lambda i: i.response.edit_message(view=expanded()))
        return ui.DesignerView(
            _label("H · Alert collapsed once handled"),
            ui.Container(
                ui.Section(
                    ui.TextDisplay(
                        f"✅ **Handled** · image burst · **{f.name}**\n"
                        f"-# Timed out 7d · 4 images learned · by {f.mod.mention} {_ts(timedelta(minutes=-2))}"
                    ),
                    accessory=show,
                ),
                color=discord.Color.dark_grey(),
            ),
            timeout=None,
        )

    def expanded() -> ui.DesignerView:
        hide = _button("Hide details", emoji="🔼", callback=lambda i: i.response.edit_message(view=collapsed()))
        return ui.DesignerView(
            _label("H · Alert collapsed once handled (expanded)"), _alert_container(f, with_buttons=False, toggle=hide), timeout=None
        )

    return collapsed()


# --- replies ------------------------------------------------------------------------------------------------


def replies(f: Fake) -> ui.DesignerView:
    return ui.DesignerView(
        _label("I · Mod command reply in V2 (vs today's embed)"),
        ui.Container(
            ui.TextDisplay(f"🔨 ***{f.name} has been banned for 7d.***\n-# Case #7 · DM delivered"),
            color=discord.Color.red(),
        ),
        _label("J · Confirm prompt in V2 (/clearwarns, /purge, /lockdown start; shown privately)"),
        ui.Container(
            ui.TextDisplay(f"**Clear all active warnings for {f.user.mention}?**\n-# 3 warnings stop counting toward the ladder."),
            ui.ActionRow(_button("Clear warnings", discord.ButtonStyle.danger), _button("Cancel")),
            color=discord.Color.gold(),
        ),
    )


SECTIONS = {
    "cases": lambda f: [case_base(f), case_context(f), case_buttons(f), *case_expiry(f), *case_linked(f), case_everything(f)],
    "alerts": lambda f: [alert_full(f), alert_collapsed(f)],
    "replies": lambda f: [replies(f)],
}


class V2PreviewCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="v2preview", description="Post Components V2 mockups with fake data (Bruno only)")
    @discord.default_permissions(administrator=True)
    @discord.option("section", str, choices=["all", *SECTIONS], description="Which mockups to post")
    async def v2preview(self, ctx: discord.ApplicationContext, section: str = "all"):
        if not is_bruno(ctx.author):
            return await ctx.respond("Only Bruno can run this.", ephemeral=True)
        await ctx.respond("Posting mockups…", ephemeral=True)
        fake = Fake(ctx)
        for name, build in SECTIONS.items():
            if section in ("all", name):
                for view in build(fake):
                    await ctx.channel.send(view=view, allowed_mentions=NO_PINGS)


def setup(bot: discord.Bot):
    bot.add_cog(V2PreviewCog(bot))
