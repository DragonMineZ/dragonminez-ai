import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs.mod_commands import ModCommandsCog, purge_check, role_refusal
from bulmaai.services.mod_actions import MAX_TIMEOUT_SECONDS, ActionResult, ModActionError
from bulmaai.services.mod_cases import ModCase

HELPER_ROLE, MOD_ROLE, ADMIN_ROLE = 1, 2, 3
OWNER_ID, HELPER_ID, MOD_ID, MOD2_ID, ADMIN_ID, RANDOM_ID, BOT_ID = 111, 222, 444, 445, 666, 333, 999
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_member(user_id, guild, role_ids=(), position=0):
    return SimpleNamespace(
        id=user_id,
        name=f"user{user_id}",
        guild=guild,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        top_role=SimpleNamespace(position=position),
        guild_permissions=SimpleNamespace(administrator=False),
        add_roles=AsyncMock(),
        remove_roles=AsyncMock(),
    )


def make_role(position=5, managed=False, default=False, **perms):
    permissions = dict(administrator=False, manage_guild=False, manage_roles=False, ban_members=False) | perms
    return SimpleNamespace(
        position=position,
        managed=managed,
        is_default=lambda: default,
        permissions=SimpleNamespace(**permissions),
        mention="@role",
    )


def make_message(author_id=5, *, bot=False, content="", attachments=(), embeds=(), pinned=False):
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id, bot=bot),
        content=content,
        attachments=list(attachments),
        embeds=list(embeds),
        pinned=pinned,
        id=12345,
        delete=AsyncMock(),
    )


def make_attachment(hashable=True):
    """Make a mock attachment that can optionally be hashed."""
    return SimpleNamespace(
        content_type="image/png" if hashable else "application/pdf",
        width=200 if hashable else None,
        height=200 if hashable else None,
        proxy_url="https://example.com/image.png",
        url="https://example.com/image.png",
        size=1000,
    )


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings = SimpleNamespace(
            panel_admin_role_ids=(ADMIN_ROLE,),
            panel_moderator_role_ids=(MOD_ROLE,),
            panel_helper_role_ids=(HELPER_ROLE,),
            panel_owner_role_ids=(),
            panel_guild_id=1,
            dev_guild_id=None,
            moderation_warn_ladder="2/7d=24h, 5/30d=3d, 7/30d=ban",
        )
        self.guild = SimpleNamespace(id=1, owner_id=OWNER_ID)
        self.members = {
            OWNER_ID: make_member(OWNER_ID, self.guild, position=100),
            HELPER_ID: make_member(HELPER_ID, self.guild, [HELPER_ROLE], position=10),
            MOD_ID: make_member(MOD_ID, self.guild, [MOD_ROLE], position=20),
            MOD2_ID: make_member(MOD2_ID, self.guild, [MOD_ROLE], position=20),
            ADMIN_ID: make_member(ADMIN_ID, self.guild, [ADMIN_ROLE], position=30),
            RANDOM_ID: make_member(RANDOM_ID, self.guild, position=1),
            BOT_ID: make_member(BOT_ID, self.guild, position=50),
        }
        self.guild.get_member = self.members.get
        self.guild.me = self.members[BOT_ID]
        self.bot = SimpleNamespace(
            settings=settings,
            user=SimpleNamespace(id=BOT_ID),
            get_guild=lambda guild_id: self.guild if guild_id == 1 else None,
        )
        self.cog = ModCommandsCog(self.bot)
        self.target = SimpleNamespace(id=RANDOM_ID, name="target")

    def ctx(self, user_id):
        member = self.members[user_id]
        return SimpleNamespace(
            author=member,
            user=member,
            guild=self.guild,
            channel=SimpleNamespace(),
            respond=AsyncMock(),
            defer=AsyncMock(),
            response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            send_modal=AsyncMock(),
        )

    def reply(self, ctx):
        return ctx.respond.await_args.args[0]

    async def test_helper_cannot_ban(self):
        ctx = self.ctx(HELPER_ID)
        with patch("bulmaai.services.mod_actions.perform", AsyncMock()) as perform:
            await self.cog.ban.callback(self.cog, ctx, self.target, "raid")
        perform.assert_not_awaited()
        ctx.respond.assert_awaited_once_with("Your staff tier can't do that.", ephemeral=True)

    async def test_ban_duration_and_message_deletion(self):
        ctx = self.ctx(ADMIN_ID)
        with patch("bulmaai.services.mod_actions.perform", AsyncMock(return_value=ActionResult("ban", 7))) as perform:
            await self.cog.ban.callback(self.cog, ctx, self.target, "raid", "soon")
            perform.assert_not_awaited()
            self.assertIn("Couldn't read that duration", self.reply(ctx))

            await self.cog.ban.callback(self.cog, ctx, self.target, "raid", "7d", "24h")
        kwargs = perform.await_args.kwargs
        self.assertEqual((kwargs["duration_seconds"], kwargs["delete_message_seconds"]), (7 * 86400, 86400))
        self.assertEqual(self.reply(ctx), f"Banned <@{RANDOM_ID}> for 7d (case #7).")

    async def test_warn_calls_perform_and_renders_escalation(self):
        ctx = self.ctx(HELPER_ID)
        escalation = SimpleNamespace(action="timeout", case_id=13, duration_seconds=86400)
        result = ActionResult("warn", 12, dm_sent=True, escalation=escalation)
        with patch("bulmaai.services.mod_actions.perform", AsyncMock(return_value=result)) as perform:
            await self.cog.warn.callback(self.cog, ctx, self.target, "spam")
        perform.assert_awaited_once_with(
            self.bot, self.guild, action="warn", target_id=RANDOM_ID, moderator=ctx.user, reason="spam"
        )
        self.assertEqual(self.reply(ctx), f"Warned <@{RANDOM_ID}> (case #12). DM delivered.\n→ auto timeout 1d (case #13)")

        failing = AsyncMock(side_effect=ModActionError("That user's panel tier is equal to or above yours.", 403))
        with patch("bulmaai.services.mod_actions.perform", failing):
            await self.cog.warn.callback(self.cog, ctx, self.target, "spam")
        ctx.respond.assert_awaited_with("That user's panel tier is equal to or above yours.", ephemeral=True)

    async def test_mute_rejects_bad_duration_and_caps_long_ones(self):
        ctx = self.ctx(MOD_ID)
        with patch("bulmaai.services.mod_actions.perform", AsyncMock(return_value=ActionResult("timeout", 5))) as perform:
            await self.cog.mute.callback(self.cog, ctx, self.target, "for a while", "spam")
            perform.assert_not_awaited()
            self.assertIn("Couldn't read that duration", self.reply(ctx))

            await self.cog.mute.callback(self.cog, ctx, self.target, "60d", "spam")
        self.assertEqual(perform.await_args.kwargs["action"], "timeout")
        self.assertEqual(perform.await_args.kwargs["duration_seconds"], MAX_TIMEOUT_SECONDS)
        self.assertEqual(self.reply(ctx), f"Timed out <@{RANDOM_ID}> for 28d (case #5).")

    async def test_role_add_respects_tier_and_hierarchy(self):
        role = make_role(position=5)
        ctx = self.ctx(HELPER_ID)
        await self.cog.role_add.callback(self.cog, ctx, self.target, role)
        self.assertEqual(self.reply(ctx), "Your staff tier can't do that.")

        ctx = self.ctx(MOD_ID)
        await self.cog.role_add.callback(self.cog, ctx, SimpleNamespace(id=MOD2_ID), role)
        self.assertEqual(self.reply(ctx), "That user's panel tier is equal to or above yours.")
        self.members[MOD2_ID].add_roles.assert_not_awaited()

        await self.cog.role_add.callback(self.cog, ctx, self.target, make_role(position=5, manage_roles=True))
        self.assertIn("can't be changed with /role", self.reply(ctx))
        self.members[RANDOM_ID].add_roles.assert_not_awaited()

        await self.cog.role_add.callback(self.cog, ctx, self.target, role)
        self.members[RANDOM_ID].add_roles.assert_awaited_once()
        self.assertEqual(self.reply(ctx), f"Gave @role to <@{RANDOM_ID}>.")

    async def test_tempban_expiry(self):
        due = [
            ModCase(1, 1, 50, MOD_ID, "ban", "raid", 3600, "command", NOW, expires_at=NOW),  # unbanned by hand
            ModCase(2, 1, 51, MOD_ID, "ban", "raid", 3600, "command", NOW, expires_at=NOW),  # lifted
            ModCase(3, 99, 52, MOD_ID, "ban", "raid", 3600, "command", NOW, expires_at=NOW),  # guild not visible
            ModCase(4, 1, 53, MOD_ID, "ban", "raid", 3600, "command", NOW, expires_at=NOW),  # bot lacks permission
        ]
        perform = AsyncMock(
            side_effect=[
                ModActionError("That user isn't banned.", 404),
                ActionResult("unban", 8),
                ModActionError("Discord refused: the bot is missing permissions for that.", 409),
            ]
        )
        deactivate = AsyncMock()
        with (
            patch("bulmaai.services.mod_cases.due_expirations", AsyncMock(return_value=due)),
            patch("bulmaai.services.mod_cases.deactivate_case", deactivate),
            patch("bulmaai.services.mod_actions.perform", perform),
            self.assertLogs("bulmaai.cogs.mod_commands", "WARNING") as logs,
        ):
            await self.cog.expire_tempbans()
        self.assertEqual([call.kwargs["target_id"] for call in perform.await_args_list], [50, 51, 53])
        self.assertEqual(
            perform.await_args_list[0].kwargs,
            dict(
                action="unban",
                target_id=50,
                moderator=None,
                reason="Tempban expired (case #1)",
                source="tempban",
                notify=False,
            ),
        )
        deactivate.assert_awaited_once_with(1, 1)
        self.assertIn("case #4", logs.output[0])

    async def test_expiry_survives_a_database_outage(self):
        with (
            patch("bulmaai.services.mod_cases.due_expirations", AsyncMock(side_effect=OSError("db down"))),
            self.assertLogs("bulmaai.cogs.mod_commands", "ERROR"),
        ):
            await self.cog.expire_tempbans()

    async def test_every_command_is_hidden_from_regular_members(self):
        for command in self.cog.get_commands():
            self.assertTrue(command.default_member_permissions.moderate_members, command.name)

    async def test_mark_scam_image_with_two_images(self):
        ctx = self.ctx(MOD_ID)
        message = make_message(
            author_id=RANDOM_ID,
            attachments=[make_attachment(hashable=True), make_attachment(hashable=True)],
        )

        with (
            patch("bulmaai.services.scam_images.is_hashable", return_value=True),
            patch("bulmaai.services.scam_images.hash_attachment", AsyncMock(side_effect=[123456, 789012])),
            patch("bulmaai.services.scam_images.add", AsyncMock(side_effect=[1, 2])),
        ):
            await self.cog.mark_scam_image_menu.callback(self.cog, ctx, message)

        message.delete.assert_awaited_once()
        self.assertIn("Added scam image #1, #2 and deleted the message", self.reply(ctx))
        self.assertIn("#1", self.reply(ctx))
        self.assertIn("#2", self.reply(ctx))
        self.assertIn("deleted the message", self.reply(ctx))

    async def test_mark_scam_image_with_no_hashable_images(self):
        ctx = self.ctx(MOD_ID)
        message = make_message(
            author_id=RANDOM_ID,
            attachments=[make_attachment(hashable=False)],
        )

        with patch("bulmaai.services.scam_images.is_hashable", return_value=False):
            await self.cog.mark_scam_image_menu.callback(self.cog, ctx, message)

        message.delete.assert_not_awaited()
        self.assertEqual(self.reply(ctx), "No usable images on that message.")

    async def test_mark_scam_image_helper_tier_refused(self):
        ctx = self.ctx(HELPER_ID)
        message = make_message(
            author_id=RANDOM_ID,
            attachments=[make_attachment(hashable=True)],
        )

        await self.cog.mark_scam_image_menu.callback(self.cog, ctx, message)

        ctx.respond.assert_awaited_once_with("Your staff tier can't do that.", ephemeral=True)
        message.delete.assert_not_awaited()

    async def test_scamimage_remove_unknown_id(self):
        ctx = self.ctx(MOD_ID)

        with patch("bulmaai.services.scam_images.remove", AsyncMock(return_value=False)):
            await self.cog.scamimage_remove.callback(self.cog, ctx, 999)

        self.assertIn("doesn't exist", self.reply(ctx))


class PurgeCheckTests(unittest.TestCase):
    def test_kinds(self):
        image = SimpleNamespace(content_type="image/png")
        archive = SimpleNamespace(content_type="application/zip")
        cases = {
            "bots": (make_message(bot=True), make_message()),
            "humans": (make_message(), make_message(bot=True)),
            "links": (make_message(content="see https://example.com"), make_message(content="see example dot com")),
            "invites": (make_message(content="join discord.com/invite/abc"), make_message(content="discord.com/channels/1")),
            "images": (make_message(attachments=[image]), make_message(attachments=[archive])),
            "attachments": (make_message(attachments=[archive]), make_message()),
            "embeds": (make_message(embeds=[object()]), make_message()),
        }
        for kind, (match, miss) in cases.items():
            check = purge_check(kind=kind)
            self.assertTrue(check(match), kind)
            self.assertFalse(check(miss), kind)
        self.assertTrue(purge_check(kind="invites")(make_message(content="discord.gg/xyz")))

    def test_filters_combine_and_pins_are_kept(self):
        check = purge_check(user_id=5, contains="FREE nitro")
        self.assertTrue(check(make_message(5, content="get free Nitro here")))
        self.assertFalse(check(make_message(6, content="free nitro")))
        self.assertFalse(check(make_message(5, content="hello")))
        self.assertFalse(check(make_message(5, content="free nitro", pinned=True)))
        self.assertTrue(purge_check()(make_message()))


class RoleRefusalTests(unittest.TestCase):
    def setUp(self):
        self.guild = SimpleNamespace(owner_id=OWNER_ID, me=SimpleNamespace(top_role=SimpleNamespace(position=50)))
        self.moderator = SimpleNamespace(id=MOD_ID, top_role=SimpleNamespace(position=20))

    def test_refusals(self):
        cases = {
            "managed": make_role(managed=True),
            "@everyone": make_role(default=True),
            "admin": make_role(administrator=True),
            "manage server": make_role(manage_guild=True),
            "ban members": make_role(ban_members=True),
            "above moderator": make_role(position=20),
        }
        for label, role in cases.items():
            self.assertIsNotNone(role_refusal(role, self.moderator, self.guild), label)
        self.assertIsNone(role_refusal(make_role(position=19), self.moderator, self.guild))

    def test_owner_skips_own_role_check_but_not_the_bots(self):
        owner = SimpleNamespace(id=OWNER_ID, top_role=SimpleNamespace(position=1))
        self.assertIsNone(role_refusal(make_role(position=40), owner, self.guild))
        self.assertIn("bot's top role", role_refusal(make_role(position=50), owner, self.guild))


if __name__ == "__main__":
    unittest.main()
