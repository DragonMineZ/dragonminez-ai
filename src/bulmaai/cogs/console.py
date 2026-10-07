"""VPS console for the running bot: scripts/dmz-bot sends one command over a Unix socket and prints the reply.

The socket is chmod 600, so file permissions are the auth: only the bot's own user (and root) can connect.
Console actions are private: no case, no mod-log post, nothing in the panel. Each command is reported only to
Bruno's personal channel, and nothing is logged at WARNING+ (that level is forwarded to a staff channel).
"""

import asyncio
import json
import logging
import os
import re
from contextlib import suppress
from pathlib import Path

import discord

from bulmaai.services import mod_actions, mod_cases
from bulmaai.services.mod_actions import ModActionError
from bulmaai.utils.lifecycle import ReloadableCog


log = logging.getLogger(__name__)

SOCKET_PATH = Path(os.environ.get("DMZ_BOT_SOCKET") or Path(__file__).resolve().parents[3] / ".dmz-bot.sock")
SOURCE = "console"
READ_TIMEOUT_SECONDS = 10
PRIVATE_LOG_CHANNEL_ID = 1557490771328770198  # Bruno's Spaceship, personal
MENTION_RE = re.compile(r"^<@!?(\d+)>$")

HELP = """dmz-bot commands:
  ban <user> [reason] [--delete-days N]
  unban <user> [reason]
  timeout <user> <duration> [reason]     e.g. 10m, 2h, 1d
  role add <user> <role>                 role name or ID
  role remove <user> <role>
  cases <user>
  restart | status | logs                handled by systemd, not the bot
<user> is a Discord ID, a mention, or an exact username."""


class ConsoleCog(ReloadableCog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._server: asyncio.AbstractServer | None = None

    async def on_startup(self) -> None:
        if not hasattr(asyncio, "start_unix_server"):
            return  # Windows dev machines: no Unix sockets, no console
        with suppress(FileNotFoundError):
            SOCKET_PATH.unlink()  # stale socket from a crash
        self._server = await asyncio.start_unix_server(self._handle, path=str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        log.info("Console listening on %s", SOCKET_PATH)

    async def on_shutdown(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        with suppress(FileNotFoundError):
            SOCKET_PATH.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT_SECONDS)
            argv = json.loads(line or b"[]")
            if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
                reply = "Bad request."
            else:
                reply = await self.run(argv)
                await self._report(argv, reply)
        except (asyncio.TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            reply = "Bad request."
        except Exception as error:
            log.info("Console command failed", exc_info=True)  # INFO: journal only, never forwarded
            reply = f"Failed: {error}"
        writer.write(reply.encode() + b"\n")
        with suppress(ConnectionError):
            await writer.drain()
        writer.close()

    async def run(self, argv: list[str]) -> str:
        if not argv or argv[0] in ("help", "-h", "--help"):
            return HELP
        guild = self.bot.get_guild(self.bot.settings.panel_guild_id)
        if guild is None:
            return "The bot isn't connected to the DragonMineZ server yet."
        command, args = argv[0], argv[1:]
        try:
            if command == "ban":
                return await self._ban(guild, args)
            if command == "unban" and args:
                return await self._act(guild, "unban", args[0], " ".join(args[1:]))
            if command == "timeout" and len(args) >= 2:
                seconds = mod_actions.parse_duration_seconds(args[1])
                if not seconds:
                    return f"Couldn't read the duration {args[1]!r}. Use something like 10m, 2h or 1d."
                return await self._act(guild, "timeout", args[0], " ".join(args[2:]), duration_seconds=seconds)
            if command == "role" and len(args) >= 3 and args[0] in ("add", "remove"):
                return await self._role(guild, args[0], args[1], " ".join(args[2:]))
            if command == "cases" and args:
                return await self._cases(guild, args[0])
        except ModActionError as error:
            return str(error)
        return f"Unknown or incomplete command.\n\n{HELP}"

    async def _ban(self, guild: discord.Guild, args: list[str]) -> str:
        if not args:
            return "Usage: ban <user> [reason] [--delete-days N]"
        delete_days = 0
        if "--delete-days" in args:
            index = args.index("--delete-days")
            try:
                delete_days = max(0, min(7, int(args[index + 1])))
            except (IndexError, ValueError):
                return "--delete-days needs a number from 0 to 7."
            del args[index : index + 2]
        return await self._act(
            guild, "ban", args[0], " ".join(args[1:]), delete_message_seconds=delete_days * 86400
        )

    async def _act(self, guild: discord.Guild, action: str, who: str, reason: str, **kwargs) -> str:
        user_id = await self._user_id(guild, who)
        result = await mod_actions.perform(
            self.bot, guild, action=action, target_id=user_id, moderator=None,
            reason=reason, source=SOURCE, record=False, **kwargs,
        )
        case = f", case #{result.case_id}" if result.case_id else ""
        return f"{action.title()} done for {await self._label(user_id)}{case}."

    async def _role(self, guild: discord.Guild, verb: str, who: str, role_text: str) -> str:
        member = await mod_actions.resolve_member(guild, await self._user_id(guild, who))
        if member is None:
            return "That user isn't in the server."
        role = guild.get_role(int(role_text)) if role_text.isdigit() else None
        role = role or discord.utils.find(lambda r: r.name.casefold() == role_text.casefold(), guild.roles)
        if role is None:
            return f"No role named {role_text!r}."
        if role >= guild.me.top_role or role.managed:
            return f"The bot can't manage {role.name}: it's managed by an integration or not below the bot's top role."
        try:
            if verb == "add":
                await member.add_roles(role)
            else:
                await member.remove_roles(role)
        except discord.Forbidden:
            return "Discord refused: the bot is missing Manage Roles."
        return f"{'Added' if verb == 'add' else 'Removed'} {role.name} {'to' if verb == 'add' else 'from'} {member}."

    async def _cases(self, guild: discord.Guild, who: str) -> str:
        user_id = await self._user_id(guild, who)
        cases = await mod_cases.list_cases(guild.id, user_id=user_id, limit=15)
        if not cases:
            return f"No cases for {await self._label(user_id)}."
        lines = [f"Cases for {await self._label(user_id)} (newest first):"]
        for case in cases:
            state = "" if case.active else " [inactive]"
            when = case.created_at.strftime("%Y-%m-%d")
            lines.append(f"  #{case.id} {when} {case.action}{state} via {case.source}: {case.reason or 'no reason'}")
        return "\n".join(lines)

    async def _user_id(self, guild: discord.Guild, who: str) -> int:
        if match := MENTION_RE.match(who):
            return int(match.group(1))
        if who.isdigit():
            return int(who)
        member = guild.get_member_named(who)
        if member is None:
            raise ModActionError(f"No member named {who!r}. Use their Discord ID instead.", 404)
        return member.id

    async def _report(self, argv: list[str], reply: str) -> None:
        if argv and argv[0] in ("help", "-h", "--help"):
            return
        try:
            channel = self.bot.get_channel(PRIVATE_LOG_CHANNEL_ID) or await self.bot.fetch_channel(PRIVATE_LOG_CHANNEL_ID)
            command = " ".join(argv).replace("`", "'")[:500]
            await channel.send(
                f"`dmz-bot {command}`\n{reply[:1500]}", allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            log.info("Console report to the private channel failed", exc_info=True)

    async def _label(self, user_id: int) -> str:
        user = self.bot.get_user(user_id)
        return f"{user} ({user_id})" if user is not None else str(user_id)


def setup(bot: discord.Bot):
    bot.add_cog(ConsoleCog(bot))
