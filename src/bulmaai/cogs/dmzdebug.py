"""Auto-parses ``/dmzdebug`` player-state dumps uploaded to Discord.

A dev runs ``/dmzdebug`` in-game, grabs ``<player>.log`` (or the JSON sidecar) and
uploads it here. This cog detects such a file *by content* (not extension), parses
it (:mod:`bulmaai.utils.dmzdebug_parser`) and replies with a compact diagnostic
embed: core stats, form, active modifiers, quest progress, and — most usefully — a
party client-vs-server desync detector (see ``AI/dmzdebug-discord-parsing.md`` §7).
"""

import logging

import discord
from discord.ext import commands

from bulmaai.utils import dmzdebug_diagnostics as diag
from bulmaai.utils.dmzdebug_parser import (
    DebugReport,
    as_bool,
    as_number,
    detect_format,
    looks_like_dmzdebug,
    parse_debug,
)

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = (".log", ".txt", ".json")
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

_CORE_STATS = ("STR", "SKP", "RES", "VIT", "PWR", "ENE")
_STAT_LABELS = {
    "STR": "Strength", "SKP": "Ki Power", "RES": "Resistance",
    "VIT": "Vitality", "PWR": "Melee", "ENE": "Energy",
}

# Status booleans worth surfacing when set, with friendly labels.
_STATE_FLAGS: tuple[tuple[str, str], ...] = (
    ("AuraActive", "Aura on"),
    ("IsPermanentAura", "Permanent aura"),
    ("IsChargingKi", "Charging Ki"),
    ("Transforming", "Transforming"),
    ("IsBlocking", "Blocking"),
    ("IsStunned", "Stunned"),
    ("IsKnockedDown", "Knocked down"),
    ("IsFused", "Fused"),
    ("IsFusionLeader", "Fusion leader"),
    ("Descending", "Descending"),
    ("InKaioPlanet", "Kaio planet"),
    ("FriendlyFistEnabled", "Friendly fist"),
    ("IsStrikeLocked", "Strike locked"),
    ("AndroidUpgraded", "Android upgraded"),
    ("TailVisible", "Tail visible"),
    ("RenderKatana", "Katana out"),
)

# Resources shown (in order) when present, with friendly labels.
_RESOURCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("CurrentEnergy", "Energy"),
    ("CurrentStamina", "Stamina"),
    ("CurrentPoise", "Poise"),
    ("Release", "Release"),
    ("ReleaseLimit", "Release cap"),
    ("Alignment", "Alignment"),
    ("ZenkaiCount", "Zenkai"),
    ("PendingAttributePoints", "Free attr pts"),
)


# ── Formatting helpers ───────────────────────────────────────────────────────

def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _fmt_num(value) -> str:
    """Render a leaf number without a trailing ``.0`` when it is integral."""
    n = as_number(value)
    if n is None:
        return str(value)
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def _get(section, *keys):
    """Return the first present key from a section dict (order-insensitive)."""
    if not isinstance(section, dict):
        return None
    for k in keys:
        if k in section and section[k] not in (None, ""):
            return section[k]
    return None


def _member_names(members: list) -> str:
    """Render party members as a short, readable ``name (leader)`` list."""
    out = []
    for m in members[:6]:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or m.get("uuid") or "unknown"
        out.append(f"👑 {name}" if m.get("leader") else str(name))
    extra = len(members) - 6
    if extra > 0:
        out.append(f"+{extra} more")
    return ", ".join(out)


# ── Embed builder ────────────────────────────────────────────────────────────

def build_embed(report: DebugReport, filename: str, file_size: int | None = None) -> discord.Embed:
    desync = diag.detect_party_desync(report)
    warnings = diag.collect_warnings(report, filename=filename, file_size=file_size)

    colour = discord.Colour.green()
    if desync or any("dead" in w or "Battle Power to max" in w for w in warnings):
        colour = discord.Colour.red()
    elif warnings:
        colour = discord.Colour.orange()

    title = f"🐉 DMZ Debug — {report.player_name or 'Unknown player'}"
    embed = discord.Embed(title=_truncate(title, 256), colour=colour,
                          timestamp=discord.utils.utcnow())
    embed.set_footer(text=f"📄 {filename}")

    _add_identity(embed, report)
    _add_core_stats(embed, report)
    _add_resources(embed, report)
    _add_state(embed, report)
    _add_form(embed, report)
    _add_skills(embed, report)
    _add_modifiers(embed, report)
    _add_techniques(embed, report)
    _add_quests(embed, report)
    _add_party(embed, report, desync)
    _add_warnings(embed, warnings)

    return embed


def _add_identity(embed: discord.Embed, report: DebugReport) -> None:
    lines: list[str] = []

    # Race / Gender / Class read like a character card and belong up top.
    char = report.sections.get("Character")
    if isinstance(char, dict):
        race = _get(char, "Race")
        gender = _get(char, "Gender")
        klass = _get(char, "Class")
        bits = [str(b) for b in (race, klass, gender) if b]
        if bits:
            lines.append("🧬 " + " · ".join(f"`{b}`" for b in bits))

    if report.player_uuid:
        lines.append(f"🆔 `{report.player_uuid}`")
    if report.fmt == "json":
        meta = []
        if report.mod_version:
            meta.append(f"mod `{report.mod_version}`")
        if report.mc_version:
            meta.append(f"MC `{report.mc_version}`")
        if report.forge_version:
            meta.append(f"Forge `{report.forge_version}`")
        if meta:
            lines.append("⚙️ " + " · ".join(meta))
        if report.dimension:
            lines.append(f"🌍 {report.dimension}")
        if report.generated_at:
            lines.append(f"🕒 {report.generated_at}")
        if report.online is not None:
            lines.append("🟢 online" if report.online else "⚪ offline")
    else:
        lines.append("📝 Legacy text dump — limited metadata (no mod/MC version).")
    if report.scope:
        lines.append(f"🔎 Scope: `{report.scope}`")

    if lines:
        embed.add_field(name="Player", value=_truncate("\n".join(lines), 1024), inline=False)


def _add_core_stats(embed: discord.Embed, report: DebugReport) -> None:
    stats = report.sections.get("Stats")
    if not isinstance(stats, dict) or not stats:
        return

    # Aligned monospace card: "Strength   500" so columns line up regardless of
    # value width. Two rows of three read comfortably on both desktop and mobile.
    present = [(k, stats[k]) for k in _CORE_STATS if k in stats]
    rows = [
        f"{_STAT_LABELS.get(k, k):<9} {_fmt_num(v):>12}"
        for k, v in present
    ]
    value = "```\n" + "\n".join(rows) + "\n```" if rows else "*(no core stats)*"

    derived_bits: list[str] = []
    if report.derived:
        d = report.derived
        if "battlePower" in d:
            derived_bits.append(f"⚡ **BP** {_fmt_num(d['battlePower'])}")
        if "level" in d:
            derived_bits.append(f"📈 **Lvl** {_fmt_num(d['level'])}")
        maxes = [
            f"{label} {_fmt_num(d[key])}"
            for label, key in (("HP", "maxHealth"), ("EN", "maxEnergy"), ("ST", "maxStamina"))
            if key in d
        ]
        if maxes:
            derived_bits.append("❤️ " + " / ".join(maxes))
    if derived_bits:
        value += "\n" + " · ".join(derived_bits)
    elif report.fmt == "text":
        value += "\n*Derived BP / Level / Max HP need the JSON dump.*"

    embed.add_field(name="📊 Stats", value=_truncate(value, 1024), inline=False)


def _add_resources(embed: discord.Embed, report: DebugReport) -> None:
    res = report.sections.get("Resources")
    if not isinstance(res, dict) or not res:
        return
    parts = []
    for key, label in _RESOURCE_FIELDS:
        if key in res:
            parts.append(f"**{label}** {_fmt_num(res[key])}")
    if parts:
        embed.add_field(name="🔋 Resources",
                        value=_truncate(" · ".join(parts), 1024), inline=False)


def _add_state(embed: discord.Embed, report: DebugReport) -> None:
    status = report.sections.get("Status")
    if not isinstance(status, dict) or not status:
        return

    lines: list[str] = []
    alive = as_bool(status.get("IsAlive"))
    if alive is False:
        lines.append("💀 **Dead**")
    elif alive is True:
        lines.append("❤️ Alive")

    active = [label for key, label in _STATE_FLAGS if as_bool(status.get(key)) is True]
    if active:
        lines.append("🎏 " + " · ".join(active))

    if lines:
        embed.add_field(name="🩺 State", value=_truncate("\n".join(lines), 1024), inline=False)


def _add_skills(embed: discord.Embed, report: DebugReport) -> None:
    skills = report.sections.get("Skills")
    skill_list = skills.get("SkillsList") if isinstance(skills, dict) else None
    if not isinstance(skill_list, list) or not skill_list:
        return

    entries = []
    for s in skill_list:
        if not isinstance(s, dict):
            continue
        name = s.get("Name", "?")
        lvl = _fmt_num(s.get("Level")) if s.get("Level") is not None else "?"
        cap = s.get("MaxLevel")
        cap_txt = f"/{_fmt_num(cap)}" if cap is not None else ""
        star = "⭐" if as_bool(s.get("IsActive")) else ""
        entries.append(f"{star}`{name}` {lvl}{cap_txt}".strip())

    if entries:
        embed.add_field(name=f"📚 Skills ({len(entries)})",
                        value=_truncate(" · ".join(entries), 1024), inline=False)


def _add_techniques(embed: discord.Embed, report: DebugReport) -> None:
    tech = report.sections.get("Techniques")
    if not isinstance(tech, dict) or not tech:
        return

    lines: list[str] = []
    slots = tech.get("EquippedSlots")
    if isinstance(slots, dict):
        equipped = [
            f"{i}:`{slots[f'Slot{i}']}`"
            for i in range(10)
            if isinstance(slots.get(f"Slot{i}"), str) and slots[f"Slot{i}"].strip()
        ]
        if equipped:
            lines.append("🎰 " + " · ".join(equipped))

    unlocked = tech.get("UnlockedTechniques")
    if isinstance(unlocked, list) and unlocked:
        lines.append(f"🔓 {len(unlocked)} unlocked")

    if as_bool(tech.get("TechniqueCharging")):
        pct = tech.get("TechniqueChargePercent")
        charging = tech.get("ChargingTechniqueId") or "?"
        pct_txt = f" ({_fmt_num(pct)}%)" if pct is not None else ""
        lines.append(f"⚡ Charging `{charging}`{pct_txt}")

    if lines:
        embed.add_field(name="🌀 Techniques",
                        value=_truncate("\n".join(lines), 1024), inline=False)


def _add_form(embed: discord.Embed, report: DebugReport) -> None:
    char = report.sections.get("Character")
    group = form = None
    if isinstance(char, dict):
        group = _get(char, "CurrentFormGroup")
        form = _get(char, "CurrentForm")
    if report.derived:
        group = group or report.derived.get("currentFormGroup")
        form = form or report.derived.get("currentForm")
    if not (group or form):
        return

    lines = [f"Current: `{group or '?'}` / `{form or '?'}`"]
    if isinstance(char, dict):
        prev = _get(char, "PreviousForm")
        if prev:
            lines.append(f"Previous: `{prev}`")
    embed.add_field(name="🔥 Form", value=_truncate("\n".join(lines), 1024), inline=False)


def _add_modifiers(embed: discord.Embed, report: DebugReport) -> None:
    lines: list[str] = []

    effects = report.sections.get("Effects")
    effect_list = effects.get("EffectsList") if isinstance(effects, dict) else None
    if isinstance(effect_list, list) and effect_list:
        names = []
        for e in effect_list[:6]:
            if isinstance(e, dict):
                nm = e.get("Name", "?")
                dur = e.get("Duration")
                names.append(f"{nm} ({_fmt_num(dur)}t)" if dur is not None else str(nm))
        lines.append("✨ **Effects:** " + ", ".join(names))

    cooldowns = diag.summarize_cooldowns(report)
    if cooldowns:
        cd_text = ", ".join(f"{name} {secs}s" for name, secs in cooldowns[:8])
        lines.append("⏳ **Cooldowns:** " + cd_text)

    bonus = report.sections.get("BonusStats")
    if isinstance(bonus, dict):
        active = [k for k, v in bonus.items() if isinstance(v, list) and v]
        if active:
            lines.append("➕ **BonusStats on:** " + ", ".join(sorted(active)))

    if lines:
        embed.add_field(name="🧪 Active Modifiers",
                        value=_truncate("\n".join(lines), 1024), inline=False)


def _add_quests(embed: discord.Embed, report: DebugReport) -> None:
    q = report.quests
    lines: list[str] = []

    if isinstance(q, dict) and ("tracked" in q or "completed" in q):
        # Hand-written summary (text format).
        tracked = q.get("tracked")
        lines.append(f"🎯 Tracked: `{tracked}`" if tracked else "🎯 Tracked: none")
        counts = (
            f"✅ {len(q.get('completed') or [])} completed · "
            f"🔄 {len(q.get('in_progress') or [])} accepted · "
            f"❌ {len(q.get('failed') or [])} failed"
        )
        lines.append(counts)

    objectives = diag.tracked_quest_objectives(report)
    if objectives:
        obj_lines = []
        for idx, cur, req in objectives["objectives"][:6]:
            obj_lines.append(f"  `{idx}`: {_fmt_num(cur)}/{_fmt_num(req)}")
        if obj_lines:
            lines.append(f"📋 `{objectives['quest_id']}` ({objectives.get('status')}):")
            lines.extend(obj_lines)
        if objectives["unclaimed"]:
            lines.append("🎁 Unclaimed rewards: " + ", ".join(objectives["unclaimed"]))

    if lines:
        embed.add_field(name="🗺️ Quests", value=_truncate("\n".join(lines), 1024), inline=False)


def _add_party(embed: discord.Embed, report: DebugReport, desync: list[str]) -> None:
    party = report.party
    if not party and not desync:
        return

    lines: list[str] = []
    if party:
        client = party.get("client") or {}
        server = party.get("server") or {}
        c_members = client.get("members") or []
        s_members = server.get("members") or []
        c_in = "in party" if client.get("in_party") else "no party"
        s_in = "in party" if server.get("in_party") else "no party"
        lines.append(f"👤 **Client:** {c_in} ({len(c_members)} members)")
        if c_members:
            lines.append("   " + _member_names(c_members))
        lines.append(f"🖥️ **Server:** {s_in} ({len(s_members)} members)")
        if s_members:
            lines.append("   " + _member_names(s_members))

    if desync:
        lines.append("⚠️ **Desync detected:**")
        lines.extend(f"• {d}" for d in desync)
    elif party:
        lines.append("✅ Client and server party state agree.")

    embed.add_field(name="🤝 Party", value=_truncate("\n".join(lines), 1024), inline=False)


def _add_warnings(embed: discord.Embed, warnings: list[str]) -> None:
    if not warnings:
        return
    text = "\n".join(f"• {w}" for w in warnings[:8])
    embed.add_field(name="🚨 Warnings", value=_truncate(text, 1024), inline=False)


# ── Cog ──────────────────────────────────────────────────────────────────────

class DmzDebugCog(commands.Cog):
    """Detects and renders ``/dmzdebug`` dumps posted as attachments."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        for attachment in message.attachments:
            if not self._is_candidate(attachment):
                continue

            try:
                raw = await attachment.read()
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                log.exception("Failed to read attachment %s", attachment.filename)
                continue

            if not looks_like_dmzdebug(text):
                continue

            log.info(
                "Parsing dmzdebug dump %s (%s) uploaded by %s in #%s",
                attachment.filename,
                detect_format(text),
                message.author,
                getattr(message.channel, "name", "DM"),
            )

            try:
                async with message.channel.typing():
                    report = parse_debug(text)
                    embed = build_embed(report, attachment.filename, attachment.size)
                await message.reply(embed=embed, mention_author=False)
            except Exception:
                log.exception("Failed to render dmzdebug dump %s", attachment.filename)

    @staticmethod
    def _is_candidate(attachment: discord.Attachment) -> bool:
        name = attachment.filename.lower()
        if not any(name.endswith(ext) for ext in ALLOWED_EXTENSIONS):
            return False
        if attachment.size > MAX_FILE_SIZE:
            log.warning(
                "Skipping oversized dmzdebug candidate %s (%d bytes)",
                attachment.filename, attachment.size,
            )
            return False
        return True


def setup(bot: discord.Bot):
    bot.add_cog(DmzDebugCog(bot))
