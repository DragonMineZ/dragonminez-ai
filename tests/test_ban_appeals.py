import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord
from aiohttp.test_utils import TestClient, TestServer

from bulmaai.cogs.ban_appeals import HANDLED_MSG, BanAppealsCog
from bulmaai.services import ban_appeals as store
from bulmaai.services import mod_actions
from bulmaai.ui import appeal_cards
from bulmaai.web import appeal_pages, routes_appeals
from bulmaai.web.core import sign_session
from bulmaai.web.server import create_app
from tests.test_admin_panel import SECRET, make_bot
from tests.v2_helpers import buttons, text

HOST = "appeals.example"
H = {"Host": HOST}
POST_H = {"Host": HOST, "Origin": f"http://{HOST}"}
USER_ID = 4242
NOW = datetime.now(timezone.utc)


def appeal(**overrides):
    values = dict(
        id=7,
        user_id=USER_ID,
        user_name="goku",
        kind="ban",
        ban_reason="spam",
        punished_at=NOW - timedelta(days=3),
        ends_at=None,
        answers={"why": "I am sorry, it will not happen again."},
        created_at=NOW - timedelta(hours=1),
    )
    values.update(overrides)
    return store.Appeal(**values)


class Member:
    id = USER_ID
    communication_disabled_until = NOW + timedelta(hours=5)

    def __str__(self):
        return "goku"


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown ban")


class AppealSiteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        routes_appeals._hits.clear()
        self.bot = make_bot()
        for key, value in (
            ("appeals_public_url", f"http://{HOST}"),
            ("appeals_channel_id", 555),
            ("appeals_invite_url", "https://discord.gg/dmz"),
            ("appeals_cooldown_days", 30),
            ("discord_oauth_client_id", "123"),
            ("discord_oauth_client_secret", "shh"),
        ):
            object.__setattr__(self.bot.settings, key, value)
        self.guild = self.bot.get_guild(1)
        self.guild.fetch_ban = AsyncMock(side_effect=not_found())
        self.guild.fetch_member = AsyncMock(side_effect=not_found())
        self.client = TestClient(TestServer(create_app(self.bot)))
        await self.client.start_server()
        self.cases = patch("bulmaai.web.routes_appeals.mod_cases.list_cases", AsyncMock(return_value=[]))
        self.cases.start()
        self.addCleanup(self.cases.stop)

    async def asyncTearDown(self):
        await self.client.close()

    def login(self):
        value = sign_session(SECRET + ":appeals", USER_ID)
        self.client.session.cookie_jar.update_cookies({routes_appeals.SESSION_COOKIE: value})
        return routes_appeals.csrf_token(self.bot.settings, value)

    def banned(self, reason="spam"):
        self.guild.fetch_ban = AsyncMock(return_value=SimpleNamespace(reason=reason, user="goku#0001"))

    async def page(self, path="/appeal/", **headers):
        response = await self.client.get(path, headers={**H, **headers}, allow_redirects=False)
        return response.status, await response.text()

    # --- hosts ---

    async def test_appeals_host_serves_only_appeal_pages(self):
        response = await self.client.get("/", headers=H, allow_redirects=False)
        self.assertEqual((response.status, response.headers["Location"]), (302, "/appeal/"))
        self.assertEqual((await self.client.get("/api/me", headers=H)).status, 404)
        self.assertEqual((await self.client.get("/static/panel.js", headers=H)).status, 404)
        self.assertEqual((await self.client.get("/t/" + "a" * 32, headers=H)).status, 404)
        self.assertEqual((await self.client.get("/appeal/")).status, 404)  # never on the panel's own host
        self.assertEqual((await self.client.get("/")).status, 200)

    async def test_landing_and_language(self):
        response = await self.client.get("/appeal/", headers={**H, "Accept-Language": "es-MX,es;q=0.9,en;q=0.5"})
        status, body = response.status, await response.text()
        self.assertEqual(status, 200)
        # Forms need the Origin header the middleware checks; no-referrer would send "Origin: null".
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("Iniciar sesión con Discord", body)
        self.assertEqual(appeal_pages.pick_language("pt-BR"), "pt")
        self.assertEqual(appeal_pages.pick_language("fr, en;q=0.1, pt;q=0.5"), "pt")
        self.assertEqual(appeal_pages.pick_language(None), "en")

    async def test_not_set_up_and_closed(self):
        object.__setattr__(self.bot.settings, "discord_oauth_client_secret", None)
        status, body = await self.page()
        self.assertEqual(status, 503)
        self.assertIn("not set up", body)
        object.__setattr__(self.bot.settings, "discord_oauth_client_secret", "shh")
        object.__setattr__(self.bot.settings, "appeals_channel_id", None)
        status, body = await self.page()
        self.assertIn("closed", body)

    # --- login ---

    async def test_login_sets_state_and_callback_checks_it(self):
        response = await self.client.get("/appeal/login", headers=H, allow_redirects=False)
        self.assertEqual(response.status, 302)
        location = response.headers["Location"]
        self.assertIn("scope=identify", location)
        self.assertIn("redirect_uri=http%3A%2F%2Fappeals.example%2Fappeal%2Fcallback", location)
        state = response.cookies[routes_appeals.STATE_COOKIE]
        self.assertTrue(state["httponly"])
        self.assertEqual(state["samesite"], "Lax")

        exchange = AsyncMock(return_value=USER_ID)
        with patch.object(routes_appeals.DiscordOAuthClient, "fetch_user_id_for_code", exchange):
            self.client.session.cookie_jar.update_cookies({routes_appeals.STATE_COOKIE: state.value})
            bad = await self.client.get("/appeal/callback?code=c&state=forged", headers=H, allow_redirects=False)
            self.assertEqual(bad.status, 400)
            exchange.assert_not_awaited()
            good = await self.client.get(
                f"/appeal/callback?code=c&state={state.value}", headers=H, allow_redirects=False
            )
        self.assertEqual(good.status, 303)
        exchange.assert_awaited_once_with("c")
        session = good.cookies[routes_appeals.SESSION_COOKIE]
        self.assertTrue(session["httponly"])
        self.assertIsNone(routes_appeals.read_session(SECRET, session.value))  # not a panel session

    # --- the appeal flow ---

    async def test_not_banned_has_nothing_to_appeal(self):
        self.login()
        with patch.object(store, "latest", AsyncMock(return_value=None)):
            status, body = await self.page()
        self.assertEqual(status, 200)
        self.assertIn("nothing to appeal", body)
        self.assertIn("https://discord.gg/dmz", body)

    async def test_timed_out_member_gets_the_form(self):
        self.login()
        member = Member()
        with (
            patch.object(store, "latest", AsyncMock(return_value=None)),
            patch("bulmaai.web.routes_appeals.resolve_member", AsyncMock(return_value=member)),
        ):
            status, body = await self.page()
        self.assertIn("timed out", body)
        self.assertIn('action="/appeal/submit"', body)

    async def test_banned_form_escapes_the_reason(self):
        self.login()
        self.banned("<script>alert(1)</script>")
        with patch.object(store, "latest", AsyncMock(return_value=None)):
            status, body = await self.page()
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>alert", body)
        self.assertIn('name="csrf"', body)

    async def test_submit_saves_the_appeal_and_posts_the_card(self):
        csrf = self.login()
        self.banned()
        created = appeal()
        record = AsyncMock()
        with (
            patch.object(store, "latest", AsyncMock(return_value=None)),
            patch.object(store, "create", AsyncMock(return_value=created)) as create,
            patch.object(store, "post_card", AsyncMock(return_value=True)) as post_card,
            patch("bulmaai.web.routes_appeals.mod_cases.record_case", record),
        ):
            response = await self.client.post(
                "/appeal/submit",
                data={"csrf": csrf, "why": "I am sorry, it will not happen again.", "extra": "thanks"},
                headers=POST_H,
                allow_redirects=False,
            )
        self.assertEqual(response.status, 303)
        kwargs = create.await_args.kwargs
        self.assertEqual((kwargs["user_id"], kwargs["kind"], kwargs["ban_reason"]), (USER_ID, "ban", "spam"))
        self.assertEqual(kwargs["answers"], {"why": "I am sorry, it will not happen again.", "extra": "thanks"})
        post_card.assert_awaited_once_with(self.bot, created)
        self.assertEqual(record.await_args.kwargs["action"], "appeal")

    async def test_submit_needs_csrf_and_a_real_reason(self):
        csrf = self.login()
        self.banned()
        with (
            patch.object(store, "latest", AsyncMock(return_value=None)),
            patch.object(store, "create", AsyncMock()) as create,
        ):
            forged = await self.client.post("/appeal/submit", data={"csrf": "nope", "why": "x" * 50}, headers=POST_H)
            short = await self.client.post("/appeal/submit", data={"csrf": csrf, "why": "pls"}, headers=POST_H)
            too_long = await self.client.post("/appeal/submit", data={"csrf": csrf, "why": "x" * 2000}, headers=POST_H)
        self.assertEqual(forged.status, 403)
        self.assertIn("at least 20 characters", await short.text())
        self.assertIn("too long", await too_long.text())
        create.assert_not_awaited()

    async def test_one_open_appeal_shows_its_status(self):
        csrf = self.login()
        self.banned()
        open_appeal = appeal(card_message_id=1, answers={"why": "<b>sorry</b>"})
        with (
            patch.object(store, "latest", AsyncMock(return_value=open_appeal)),
            patch.object(store, "create", AsyncMock()) as create,
        ):
            status, body = await self.page()
            response = await self.client.post(
                "/appeal/submit", data={"csrf": csrf, "why": "x" * 50}, headers=POST_H, allow_redirects=False
            )
        self.assertIn("Staff will review it", body)
        self.assertIn("&lt;b&gt;sorry&lt;/b&gt;", body)
        self.assertEqual(response.status, 303)
        create.assert_not_awaited()

    async def test_denied_appeal_holds_a_cooldown(self):
        csrf = self.login()
        self.banned()
        denied = appeal(outcome="denied", outcome_by=1, outcome_at=NOW - timedelta(days=2), staff_note="<i>no</i>")
        with (
            patch.object(store, "latest", AsyncMock(return_value=denied)),
            patch.object(store, "create", AsyncMock()) as create,
        ):
            _, body = await self.page()
            await self.client.post("/appeal/submit", data={"csrf": csrf, "why": "x" * 50}, headers=POST_H)
        self.assertIn("was denied", body)
        self.assertIn("&lt;i&gt;no&lt;/i&gt;", body)
        self.assertIn((denied.outcome_at + timedelta(days=30)).strftime("%Y-%m-%d"), body)
        create.assert_not_awaited()
        old = appeal(outcome="denied", outcome_by=1, outcome_at=NOW - timedelta(days=31))
        with patch.object(store, "latest", AsyncMock(return_value=old)):
            _, body = await self.page()
        self.assertIn('action="/appeal/submit"', body)

    async def test_question_and_reply(self):
        csrf = self.login()
        asked = appeal(card_message_id=1, question="Why <did> you spam?", question_by=9)
        with patch.object(store, "latest", AsyncMock(return_value=asked)):
            _, body = await self.page()
        self.assertIn("Why &lt;did&gt; you spam?", body)
        self.assertIn('action="/appeal/reply"', body)
        answered = appeal(question="q", user_reply="because")
        with (
            patch.object(store, "reply", AsyncMock(return_value=answered)) as save,
            patch.object(store, "note_reply", AsyncMock()) as note,
        ):
            response = await self.client.post(
                "/appeal/reply", data={"csrf": csrf, "appeal": "7", "reply": "because"}, headers=POST_H,
                allow_redirects=False,
            )
        self.assertEqual(response.status, 303)
        save.assert_awaited_once_with(7, USER_ID, "because")
        note.assert_awaited_once_with(self.bot, answered)

    async def test_rate_limit(self):
        for _ in range(routes_appeals.RATE_LIMITS["get"][0]):
            await self.client.get("/appeal/", headers=H)
        self.assertEqual((await self.client.get("/appeal/", headers=H)).status, 429)


class AppealRulesTests(unittest.TestCase):
    def test_cooldown(self):
        self.assertIsNone(store.next_allowed(None, 30))
        self.assertIsNone(store.next_allowed(appeal(), 30))
        recent = appeal(outcome="denied", outcome_at=NOW - timedelta(days=1))
        self.assertAlmostEqual(store.next_allowed(recent, 30), recent.outcome_at + timedelta(days=30))
        self.assertIsNone(store.next_allowed(recent, 0))
        self.assertIsNone(store.next_allowed(appeal(outcome="accepted", outcome_at=NOW), 30))

    def test_site_url_needs_channel_and_url(self):
        settings = SimpleNamespace(appeals_public_url="https://appeals.dmz/", appeals_channel_id=None)
        self.assertIsNone(store.site_url(settings))
        settings.appeals_channel_id = 5
        self.assertEqual(store.site_url(settings), "https://appeals.dmz/appeal/")
        settings.appeals_public_url = ""
        self.assertIsNone(store.site_url(settings))


class AppealCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_card_quotes_defanged_text_and_has_buttons(self):
        view = appeal_cards.appeal_card(appeal(answers={"why": "@everyone see https://evil.example now"}))
        content = text(view)
        self.assertNotIn("https://evil", content)
        self.assertNotIn("@everyone", content)
        self.assertIn(f"<@{USER_ID}>", content)
        self.assertEqual(
            [b.custom_id for b in buttons(view)], ["banappeal:accept:7", "banappeal:deny:7", "banappeal:ask:7"]
        )
        self.assertEqual(appeal_cards.parse_custom_id("banappeal:deny:7"), ("deny", 7))
        self.assertIsNone(appeal_cards.parse_custom_id("banappeal:nuke:7"))

    async def test_decided_card_collapses(self):
        view = appeal_cards.appeal_card(appeal(outcome="denied", outcome_by=9, outcome_at=NOW, staff_note="no"))
        self.assertEqual(buttons(view), [])
        self.assertIn("Denied by <@9>", text(view))
        self.assertNotIn("I am sorry", text(view))


class AppealButtonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = make_bot()
        self.guild = self.bot.get_guild(1)
        self.cog = BanAppealsCog(self.bot)
        self.staff = SimpleNamespace(id=111)

    def interaction(self):
        inter = MagicMock()
        inter.user = self.staff
        inter.response.is_done = MagicMock(return_value=True)
        inter.edit_original_response = AsyncMock()
        inter.followup.send = AsyncMock()
        return inter

    async def test_accept_unbans_and_second_click_loses(self):
        claimed = appeal(outcome="accepted", outcome_by=111, outcome_at=NOW)
        perform = AsyncMock()
        with (
            patch.object(store, "claim", AsyncMock(side_effect=[claimed, None])),
            patch.object(store, "get", AsyncMock(return_value=claimed)),
            patch.object(store, "render", AsyncMock(return_value="card")),
            patch.object(mod_actions, "perform", perform),
        ):
            first, second = self.interaction(), self.interaction()
            await self.cog.accept(first, 7)
            await self.cog.accept(second, 7)
        perform.assert_awaited_once()
        self.assertEqual(perform.await_args.kwargs["action"], "unban")
        self.assertEqual(perform.await_args.kwargs["target_id"], USER_ID)
        self.assertEqual(perform.await_args.kwargs["source"], "appeal")
        first.edit_original_response.assert_awaited_once_with(view="card")
        second.followup.send.assert_awaited_once_with(HANDLED_MSG, ephemeral=True)

    async def test_accept_timeout_and_failure_releases_claim(self):
        claimed = appeal(kind="timeout", outcome="accepted", outcome_by=111, outcome_at=NOW)
        perform = AsyncMock(side_effect=mod_actions.ModActionError("Discord refused", 409))
        with (
            patch.object(store, "claim", AsyncMock(return_value=claimed)),
            patch.object(store, "release", AsyncMock()) as release,
            patch.object(mod_actions, "perform", perform),
            patch("bulmaai.cogs.ban_appeals.unbusy", AsyncMock()),
        ):
            inter = self.interaction()
            await self.cog.accept(inter, 7)
        self.assertEqual(perform.await_args.kwargs["action"], "untimeout")
        release.assert_awaited_once_with(7, 111)
        inter.followup.send.assert_awaited_once_with("Discord refused", ephemeral=True)

    async def test_only_moderators(self):
        inter = MagicMock()
        inter.type = discord.InteractionType.component
        inter.data = {"custom_id": "banappeal:accept:7"}
        inter.guild = self.guild
        inter.user = self.guild.get_member(222)  # a helper
        inter.response.send_message = AsyncMock()
        with patch.object(store, "claim", AsyncMock()) as claim:
            await self.cog.on_interaction(inter)
        claim.assert_not_awaited()
        inter.response.send_message.assert_awaited_once()


class AppealDmTests(unittest.TestCase):
    def test_ban_and_timeout_dms_link_to_the_site(self):
        settings = SimpleNamespace(
            appeals_public_url="https://appeals.dmz", appeals_channel_id=5, panel_guild_id=1,
        )
        guild = SimpleNamespace(id=1)
        for action in ("ban", "timeout"):
            button = mod_actions._appeal_button(settings, guild, action)
            self.assertEqual(button.url, "https://appeals.dmz/appeal/")
        self.assertIsNone(mod_actions._appeal_button(settings, guild, "warn"))
        settings.appeals_channel_id = None
        self.assertIsNone(mod_actions._appeal_button(settings, guild, "ban"))
        self.assertIsNone(mod_actions._appeal_button(settings, guild, "timeout"))


if __name__ == "__main__":
    unittest.main()
