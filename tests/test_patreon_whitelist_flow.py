import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from requests import HTTPError

from bulmaai.cogs.patreon_whitelist_flow import PatreonWhitelistFlowCog
from bulmaai.services.discord_oauth import build_discord_oauth_state, parse_discord_oauth_state
from bulmaai.services.patreon_access import (
    PatreonIdentity,
    PatreonMemberStatus,
    build_patreon_oauth_state,
    parse_patreon_oauth_state,
    patreon_link_confirm_token,
)
from bulmaai.services.patreon_grants import PatreonGrant, PatreonGrantKind, PatreonLink


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.guild = SimpleNamespace(get_role=lambda role_id: None)

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeCommandContext:
    def __init__(self, *, author, channel):
        self.author = author
        self.channel = channel
        self.deferred = []
        self.followup = FakeFollowup()

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)


class CapturingPatreonWhitelistFlowCog(PatreonWhitelistFlowCog):
    def __init__(self):
        self.calls = []

    async def start_whitelist_flow_for_user(self, member, destination, initial_nickname, *, ephemeral=False):
        self.calls.append((member, destination, initial_nickname, ephemeral))


class FakeGuild:
    def __init__(self, member, *, guild_id=111):
        self.id = guild_id
        self.member = member

    def get_member(self, user_id):
        if self.member and self.member.id == user_id:
            return self.member
        return None

    async def fetch_member(self, user_id):
        if self.member and self.member.id == user_id:
            return self.member
        raise RuntimeError("member not found")


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeInteractionResponse:
    def __init__(self):
        self.deferred = []
        self.sent = []

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeButtonInteraction:
    def __init__(self, *, user):
        self.user = user
        self.response = FakeInteractionResponse()
        self.followup = FakeFollowup()
        self.message = FakeMessage()


class FakeUser:
    def __init__(self, *, user_id=456, name="Requester", mention="<@456>"):
        self.id = user_id
        self.name = name
        self.mention = mention
        self.dms = []

    def __str__(self):
        return self.name

    async def send(self, content):
        self.dms.append(content)


COMMIT_URL = "https://example.test/commit/abc"


def _conflict() -> HTTPError:
    error = HTTPError("409 Client Error: Conflict")
    error.response = SimpleNamespace(status_code=409)
    return error


class FakeGitHub:
    """The whitelist file on main; every edit is a direct commit guarded by the file sha."""

    initial_text = "ExistingUser\n"

    def __init__(self):
        self.base_branch = "main"
        self.text = self.initial_text
        self.put_calls = []

    async def get_whitelist_file(self, ref):
        return self.text, f"sha-{len(self.put_calls)}"

    async def put_whitelist_file(self, *, branch, new_text, sha, message):
        self.put_calls.append({"branch": branch, "new_text": new_text, "sha": sha, "message": message})
        self.text = new_text
        return COMMIT_URL


class FakeGitHubWithGrantNames(FakeGitHub):
    initial_text = "OwnerMC\nGiftedMC\nKeepMe\n"


class FakeGitHubWithBaseNick(FakeGitHub):
    initial_text = "ExistingUser\nNewTester\n"


class FakeGitHubWithOldSelfGrantNick(FakeGitHub):
    initial_text = "ExistingUser\nOldTester\n"


class FakeGitHubSlowCommit(FakeGitHub):
    async def put_whitelist_file(self, **kwargs):
        await asyncio.sleep(0.01)
        return await super().put_whitelist_file(**kwargs)


class FakeGitHubStaleShaOnce(FakeGitHub):
    """Someone else commits OtherGift between our read and our write."""

    async def put_whitelist_file(self, **kwargs):
        if not self.put_calls:
            self.put_calls.append({**kwargs, "rejected": True})
            self.text = "ExistingUser\nOtherGift\n"
            raise _conflict()
        return await super().put_whitelist_file(**kwargs)


class FakeGitHubAlwaysStale(FakeGitHub):
    async def put_whitelist_file(self, **kwargs):
        self.put_calls.append(kwargs)
        raise _conflict()


class CapturingOAuthPatreonWhitelistFlowCog(PatreonWhitelistFlowCog):
    def __init__(self):
        self.calls = []
        self.staff_logs = []

    async def start_whitelist_flow_for_user(
        self,
        member,
        destination,
        initial_nickname,
        *,
        ephemeral=False,
        active_link=None,
    ):
        self.calls.append((member, destination, initial_nickname, ephemeral, active_link))

    async def _log_staff_info(self, content: str) -> None:
        self.staff_logs.append(content)

    async def _resolve_member(self, guild_id: int, user_id: int):
        return self.member


class PatreonWhitelistFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # ponytail: default every test to a resolved Mojang account so the
        # warn-but-allow flagging path only fires in tests that opt into it.
        mojang_patcher = patch(
            "bulmaai.cogs.patreon_whitelist_flow.minecraft_username_exists",
            AsyncMock(return_value=True),
        )
        self.mojang_lookup = mojang_patcher.start()
        self.addCleanup(mojang_patcher.stop)

    def _settings(self):
        return SimpleNamespace(
            discord_oauth_client_id="discord-client-id",
            discord_oauth_client_secret="discord-client-secret",
            discord_oauth_redirect_uri="https://downloads.example.test/beta-access/discord/callback",
            patreon_access_role_ids=(1287877272224665640, 1287877305259130900),
            dev_jar_tester_role_ids=(1286814599215317034,),
            patreon_eligible_tier_ids=("1287877272224665640", "1287877305259130900"),
            patreon_oauth_client_id="patreon-client-id",
            patreon_oauth_client_secret="patreon-client-secret",
            patreon_oauth_redirect_uri="https://downloads.dragonminez.com/patreon/oauth/callback",
            PATREON_CAMPAIGN_ID="12861895",
            PATREON_CREATOR_TOKEN="creator-token",
            patreon_staff_channel_id=1493390527004147876,
            patreon_ai_log_channel_id=1493390527004147999,
            patreon_admin_ping_role_id=1309022450671161476,
            patreon_contributor_role_id=1287877272224665640,
            patreon_benefactor_role_id=1287877305259130900,
        )

    def test_beta_access_start_redirects_to_discord_oauth(self) -> None:
        bot = SimpleNamespace(settings=self._settings())
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot

        response = cog._handle_beta_access_start({"username": ["NewTester"]})

        self.assertEqual(response.status, 302)
        headers = dict(response.headers)
        self.assertIn("Location", headers)
        self.assertIn("https://discord.com/oauth2/authorize?", headers["Location"])
        self.assertIn("client_id=discord-client-id", headers["Location"])
        self.assertIn("scope=identify", headers["Location"])
        cookie = headers["Set-Cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=Lax"):
            self.assertIn(flag, cookie)
        state = parse_qs(urlparse(headers["Location"]).query)["state"][0]
        parsed = parse_discord_oauth_state("discord-client-secret", state, now=lambda: 0)
        self.assertEqual(cookie.split(";")[0], f"dmz_beta_access_nonce={parsed.nonce}")

    async def test_beta_access_callback_rejects_state_started_in_another_browser(self) -> None:
        cog = CapturingPatreonWhitelistFlowCog()
        cog.bot = SimpleNamespace(settings=self._settings(), guilds=[])
        state = build_discord_oauth_state(
            secret="discord-client-secret",
            minecraft_username="AttackerMC",
            expires_at=2000,
            nonce="attacker-browser-nonce",
        )

        with patch(
            "bulmaai.cogs.patreon_whitelist_flow.DiscordOAuthClient.fetch_user_id_for_code",
            AsyncMock(return_value=456),
        ) as fetch_user_id:
            response = await cog._handle_beta_access_discord_callback(
                code="oauth-code",
                state=state,
                nonce="",
                now=lambda: 1999,
            )

        self.assertEqual(response.status, 403)
        fetch_user_id.assert_not_awaited()
        self.assertEqual(cog.calls, [])

    async def test_beta_access_callback_passes_verified_member_to_whitelist_flow(self) -> None:
        member = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
            guild=SimpleNamespace(id=111),
        )
        bot = SimpleNamespace(settings=self._settings(), guilds=[FakeGuild(member)])
        cog = CapturingPatreonWhitelistFlowCog()
        cog.bot = bot
        state = build_discord_oauth_state(
            secret="discord-client-secret",
            minecraft_username="NewTester",
            expires_at=2000,
            nonce="browser-nonce",
        )

        with patch(
            "bulmaai.cogs.patreon_whitelist_flow.DiscordOAuthClient.fetch_user_id_for_code",
            AsyncMock(return_value=456),
        ):
            response = await cog._handle_beta_access_discord_callback(
                code="oauth-code",
                state=state,
                nonce="browser-nonce",
                now=lambda: 1999,
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(cog.calls), 1)
        called_member, destination, nickname, ephemeral = cog.calls[0]
        self.assertIs(called_member, member)
        self.assertEqual(nickname, "NewTester")
        self.assertFalse(ephemeral)
        self.assertIn("Verification request accepted", response.body.decode("utf-8"))
        self.assertIn("Minecraft username: NewTester", response.body.decode("utf-8"))
        self.assertEqual(destination.messages, [])

    async def test_beta_access_member_resolution_prefers_guild_member_with_patreon_role(self) -> None:
        same_user_without_access = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[],
            guild_permissions=SimpleNamespace(administrator=False),
            guild=SimpleNamespace(id=111),
        )
        same_user_with_access = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
            guild=SimpleNamespace(id=222),
        )
        bot = SimpleNamespace(
            settings=self._settings(),
            guilds=[
                FakeGuild(same_user_without_access, guild_id=111),
                FakeGuild(same_user_with_access, guild_id=222),
            ],
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot

        member = await cog._resolve_member_across_guilds(456)

        self.assertIs(member, same_user_with_access)

    async def test_beta_access_command_keeps_confirmation_ephemeral(self) -> None:
        bot = SimpleNamespace(settings=self._settings())
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()
        channel = FakeChannel()
        author = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
            guild=SimpleNamespace(id=111),
        )
        ctx = FakeCommandContext(author=author, channel=channel)

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", SimpleNamespace),
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=None)),
        ):
            await cog._handle_beta_access_command(ctx, "NewTester")

        self.assertEqual(ctx.deferred, [{"ephemeral": True}])
        self.assertEqual(channel.sent, [])
        self.assertEqual(len(ctx.followup.sent), 1)
        args, kwargs = ctx.followup.sent[0]
        self.assertIn("Authorize with Patreon", args[0])
        self.assertIn("https://www.patreon.com/oauth2/authorize?", args[0])
        self.assertTrue(kwargs["ephemeral"])

    async def test_beta_access_patreon_prompt_carries_username_and_does_not_require_rerun(self) -> None:
        bot = SimpleNamespace(settings=self._settings())
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()
        destination = FakeFollowup()
        member = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
            guild=SimpleNamespace(id=111),
        )

        with patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=None)):
            await cog.start_whitelist_flow_for_user(
                member,
                destination,
                "NewTester",
                ephemeral=True,
            )

        args, kwargs = destination.sent[0]
        message = args[0]
        self.assertIn("Authorize with Patreon", message)
        self.assertNotIn("run the command again", message)
        state = parse_qs(urlparse(message.rsplit(" ", 1)[-1]).query)["state"][0]
        parsed = parse_patreon_oauth_state(
            "patreon-client-secret",
            state,
            now=lambda: 0,
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.action, "beta_access")
        self.assertEqual(parsed.minecraft_username, "NewTester")
        self.assertTrue(kwargs["ephemeral"])

    async def test_patreon_callback_resumes_beta_access_when_state_has_username(self) -> None:
        staff_channel = FakeChannel()
        member = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
            guild=SimpleNamespace(id=111),
        )
        guild = FakeGuild(member, guild_id=111)
        bot = SimpleNamespace(
            settings=self._settings(),
            get_guild=lambda guild_id: guild if guild_id == 111 else None,
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()
        state = build_patreon_oauth_state(
            secret="patreon-client-secret",
            discord_user_id=456,
            guild_id=111,
            action="beta_access",
            expires_at=9999999999,
            minecraft_username="NewTester",
        )
        identity = PatreonIdentity(
            access_token="patreon-access-token",
            status=PatreonMemberStatus(
                patreon_user_id="patreon-user-1",
                member_id="member-1",
                full_name="Patron User",
                patron_status="active_patron",
                tier_ids=("1287877272224665640",),
                last_charge_date=None,
            ),
        )

        with (
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.PatreonOAuthClient.fetch_identity_for_code",
                AsyncMock(return_value=identity),
            ),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_patreon_link", AsyncMock()),
            patch("bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner", AsyncMock(return_value=[])),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            response = await cog._handle_patreon_oauth_callback(
                "oauth-code",
                state,
                confirm=patreon_link_confirm_token("patreon-client-secret", "oauth-code", state),
            )

        body = response.body.decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("NewTester", body)
        self.assertIn("has been whitelisted", body)
        self.assertEqual(cog.gh.text, "ExistingUser\nNewTester\n")
        self.assertEqual(upsert_grant.await_args.args[0].minecraft_username, "NewTester")

    async def test_beta_access_command_passes_ephemeral_destination_to_flow(self) -> None:
        cog = CapturingPatreonWhitelistFlowCog()
        channel = FakeChannel()
        author = FakeUser(user_id=456, name="Requester", mention="<@456>")
        ctx = FakeCommandContext(author=author, channel=channel)

        with patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", FakeUser):
            await cog._handle_beta_access_command(ctx, "NewTester")

        self.assertEqual(ctx.deferred, [{"ephemeral": True}])
        self.assertEqual(cog.calls, [(author, ctx.followup, "NewTester", True)])
        self.assertEqual(channel.sent, [])

    async def test_admin_without_patreon_role_cannot_bypass_beta_access_role_check(self) -> None:
        bot = SimpleNamespace(settings=SimpleNamespace(
                patreon_access_role_ids=(123,),
                dev_jar_tester_role_ids=(),
                patreon_staff_channel_id=1493390527004147876,
                patreon_admin_ping_role_id=1309022450671161476,
                patreon_contributor_role_id=1287877272224665640,
                patreon_benefactor_role_id=1287877305259130900,
            ))
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()

        request_channel = FakeChannel()
        member = SimpleNamespace(
            id=456,
            name="AdminNoPatreon",
            mention="<@456>",
            roles=[],
            guild_permissions=SimpleNamespace(administrator=True),
        )

        await cog.start_whitelist_flow_for_user(
            member,
            request_channel,
            "NewTester",
        )

        self.assertIn("You don't have a Patreon beta access role yet", request_channel.sent[0][0][0])

    async def test_beta_access_auto_merges_for_active_linked_patron(self) -> None:
        staff_channel = FakeChannel()
        author = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        bot = SimpleNamespace(
            settings=self._settings(),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()
        destination = FakeFollowup()
        link = PatreonLink(
            discord_user_id=456,
            discord_username="Requester",
            patreon_user_id="patreon-user-1",
            patreon_member_id="member-1",
            patreon_full_name="Patron User",
            patron_status="active_patron",
            tier_ids=("1287877272224665640",),
            last_charge_date=None,
            entitlement_active=True,
        )

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=link)),
            patch("bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner", AsyncMock(return_value=[])),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog.start_whitelist_flow_for_user(
                author,
                destination,
                "NewTester",
                ephemeral=True,
            )

        self.assertEqual(cog.gh.put_calls[0]["branch"], "main")
        self.assertEqual(cog.gh.text, "ExistingUser\nNewTester\n")
        self.assertEqual(upsert_grant.await_args.args[0].kind, PatreonGrantKind.SELF)
        self.assertEqual(upsert_grant.await_args.args[0].source_pr_url, COMMIT_URL)
        self.assertIn("approved automatically", destination.sent[-1][0][0])

    async def test_duplicate_active_beta_access_approval_runs_once(self) -> None:
        staff_channel = FakeChannel()
        author = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        bot = SimpleNamespace(
            settings=self._settings(),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHubSlowCommit()
        link = PatreonLink(
            discord_user_id=456,
            discord_username="Requester",
            patreon_user_id="patreon-user-1",
            patreon_member_id="member-1",
            patreon_full_name="Patron User",
            patron_status="active_patron",
            tier_ids=("1287877272224665640",),
            last_charge_date=None,
            entitlement_active=True,
        )
        first_destination = FakeFollowup()
        second_destination = FakeFollowup()

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=link)),
            patch("bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner", AsyncMock(return_value=[])),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await asyncio.gather(
                cog.start_whitelist_flow_for_user(
                    author,
                    first_destination,
                    "NewTester",
                    ephemeral=True,
                ),
                cog.start_whitelist_flow_for_user(
                    author,
                    second_destination,
                    "NewTester",
                    ephemeral=True,
                ),
            )

        self.assertEqual(len(cog.gh.put_calls), 1)
        self.assertEqual(upsert_grant.await_count, 1)
        all_messages = [call[0][0] for call in first_destination.sent + second_destination.sent]
        self.assertIn("`NewTester` was approved automatically for Patreon beta access.", all_messages)
        self.assertIn("`NewTester` is already whitelisted. Nothing to do.", all_messages)
        self.assertEqual(len(staff_channel.sent), 1)

    async def test_concurrent_beta_access_for_different_names_prompts_for_username_update(self) -> None:
        staff_channel = FakeChannel()
        author = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        bot = SimpleNamespace(
            settings=self._settings(),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHubSlowCommit()
        link = PatreonLink(
            discord_user_id=456,
            discord_username="Requester",
            patreon_user_id="patreon-user-1",
            patreon_member_id="member-1",
            patreon_full_name="Patron User",
            patron_status="active_patron",
            tier_ids=("1287877272224665640",),
            last_charge_date=None,
            entitlement_active=True,
        )
        first_destination = FakeFollowup()
        second_destination = FakeFollowup()
        active_grants = []

        async def list_grants(owner_id):
            return list(active_grants)

        async def upsert_grant(grant):
            active_grants[:] = [grant]

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=link)),
            patch("bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner", AsyncMock(side_effect=list_grants)),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock(side_effect=upsert_grant)) as upsert_grant_mock,
        ):
            await asyncio.gather(
                cog.start_whitelist_flow_for_user(
                    author,
                    first_destination,
                    "NewTester",
                    ephemeral=True,
                ),
                cog.start_whitelist_flow_for_user(
                    author,
                    second_destination,
                    "OtherTester",
                    ephemeral=True,
                ),
            )

        self.assertEqual(len(cog.gh.put_calls), 1)
        self.assertEqual(upsert_grant_mock.await_count, 1)
        all_messages = [call[0][0] for call in first_destination.sent + second_destination.sent]
        self.assertTrue(any("approved automatically" in message for message in all_messages))
        self.assertTrue(any("already are whitelisted" in message for message in all_messages))
        self.assertTrue(any("view" in call[1] for call in first_destination.sent + second_destination.sent))

    async def test_auto_approval_does_not_record_grant_when_username_already_whitelisted(self) -> None:
        staff_channel = FakeChannel()
        author = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        bot = SimpleNamespace(
            settings=self._settings(),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHubWithBaseNick()
        destination = FakeFollowup()
        link = PatreonLink(
            discord_user_id=456,
            discord_username="Requester",
            patreon_user_id="patreon-user-1",
            patreon_member_id="member-1",
            patreon_full_name="Patron User",
            patron_status="active_patron",
            tier_ids=("1287877272224665640",),
            last_charge_date=None,
            entitlement_active=True,
        )

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=link)),
            patch("bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner", AsyncMock(return_value=[])),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog.start_whitelist_flow_for_user(
                author,
                destination,
                "NewTester",
                ephemeral=True,
            )

        self.assertEqual(upsert_grant.await_count, 0)
        self.assertEqual(destination.sent[-1][0][0], "`NewTester` is already whitelisted. Nothing to do.")
        self.assertEqual(staff_channel.sent, [])

    async def test_auto_approval_gives_up_after_repeated_stale_sha(self) -> None:
        staff_channel = FakeChannel()
        author = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        bot = SimpleNamespace(
            settings=self._settings(),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHubAlwaysStale()
        destination = FakeFollowup()
        link = PatreonLink(
            discord_user_id=456,
            discord_username="Requester",
            patreon_user_id="patreon-user-1",
            patreon_member_id="member-1",
            patreon_full_name="Patron User",
            patron_status="active_patron",
            tier_ids=("1287877272224665640",),
            last_charge_date=None,
            entitlement_active=True,
        )

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=link)),
            patch("bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner", AsyncMock(return_value=[])),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog.start_whitelist_flow_for_user(
                author,
                destination,
                "NewTester",
                ephemeral=True,
            )

        self.assertEqual(len(cog.gh.put_calls), 3)
        self.assertEqual(upsert_grant.await_count, 0)
        self.assertIn("could not submit the whitelist change", destination.sent[-1][0][0])

    async def test_auto_approval_rereads_and_retries_after_stale_sha(self) -> None:
        staff_channel = FakeChannel()
        author = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        bot = SimpleNamespace(
            settings=self._settings(),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHubStaleShaOnce()
        destination = FakeFollowup()
        link = PatreonLink(
            discord_user_id=456,
            discord_username="Requester",
            patreon_user_id="patreon-user-1",
            patreon_member_id="member-1",
            patreon_full_name="Patron User",
            patron_status="active_patron",
            tier_ids=("1287877272224665640",),
            last_charge_date=None,
            entitlement_active=True,
        )

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=link)),
            patch("bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner", AsyncMock(return_value=[])),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog.start_whitelist_flow_for_user(
                author,
                destination,
                "NewTester",
                ephemeral=True,
            )

        self.assertEqual(len(cog.gh.put_calls), 2)
        self.assertEqual(cog.gh.text, "ExistingUser\nOtherGift\nNewTester\n")
        self.assertEqual(upsert_grant.await_count, 1)
        self.assertIn("approved automatically", destination.sent[-1][0][0])

    async def test_existing_self_grant_prompts_before_updating_minecraft_username(self) -> None:
        author = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        bot = SimpleNamespace(
            settings=self._settings(),
            get_channel=lambda channel_id: FakeChannel(),
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHubWithOldSelfGrantNick()
        destination = FakeFollowup()
        link = PatreonLink(
            discord_user_id=456,
            discord_username="Requester",
            patreon_user_id="patreon-user-1",
            patreon_member_id="member-1",
            patreon_full_name="Patron User",
            patron_status="active_patron",
            tier_ids=("1287877272224665640",),
            last_charge_date=None,
            entitlement_active=True,
        )
        grant = PatreonGrant(
            owner_discord_user_id=456,
            beneficiary_discord_user_id=456,
            beneficiary_discord_username="Requester",
            minecraft_username="OldTester",
            kind=PatreonGrantKind.SELF,
            active=True,
            source_pr_url="https://example.test/pr/1",
        )

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=link)),
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner",
                AsyncMock(return_value=[grant]),
                create=True,
            ),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog.start_whitelist_flow_for_user(
                author,
                destination,
                "NewTester",
                ephemeral=True,
            )

        args, kwargs = destination.sent[-1]
        self.assertIn("already are whitelisted", args[0])
        self.assertIn("`OldTester`", args[0])
        self.assertIn("`NewTester`", args[0])
        self.assertIn("view", kwargs)
        self.assertEqual(cog.gh.put_calls, [])
        self.assertEqual(upsert_grant.await_count, 0)

    async def test_confirmed_self_grant_username_update_replaces_old_whitelist_entry(self) -> None:
        staff_channel = FakeChannel()
        author = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        bot = SimpleNamespace(
            settings=self._settings(),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHubWithOldSelfGrantNick()
        destination = FakeFollowup()
        link = PatreonLink(
            discord_user_id=456,
            discord_username="Requester",
            patreon_user_id="patreon-user-1",
            patreon_member_id="member-1",
            patreon_full_name="Patron User",
            patron_status="active_patron",
            tier_ids=("1287877272224665640",),
            last_charge_date=None,
            entitlement_active=True,
        )
        grant = PatreonGrant(
            owner_discord_user_id=456,
            beneficiary_discord_user_id=456,
            beneficiary_discord_username="Requester",
            minecraft_username="OldTester",
            kind=PatreonGrantKind.SELF,
            active=True,
            source_pr_url="https://example.test/pr/1",
        )

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=link)),
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner",
                AsyncMock(return_value=[grant]),
                create=True,
            ),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog.start_whitelist_flow_for_user(
                author,
                destination,
                "NewTester",
                ephemeral=True,
            )
            update_view = destination.sent[-1][1]["view"]
            interaction = FakeButtonInteraction(user=author)
            await update_view.children[0].callback(interaction)

        self.assertEqual(cog.gh.put_calls[0]["new_text"], "ExistingUser\nNewTester\n")
        self.assertEqual(cog.gh.put_calls[0]["message"], "Update beta tester: OldTester -> NewTester")
        self.assertEqual(upsert_grant.await_args.args[0].minecraft_username, "NewTester")
        self.assertIn("updated from `OldTester` to `NewTester`", interaction.followup.sent[-1][0][0])
        self.assertIn("updated Patreon beta access", staff_channel.sent[-1][0][0])

    async def test_patreon_callback_shows_receiving_discord_account_before_linking(self) -> None:
        cog = CapturingOAuthPatreonWhitelistFlowCog()
        cog.bot = SimpleNamespace(settings=self._settings())
        cog.member = FakeUser(user_id=789, name="attacker")
        state = build_patreon_oauth_state(
            secret="patreon-client-secret",
            discord_user_id=789,
            guild_id=111,
            action="link",
            expires_at=9999999999,
        )

        with (
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.PatreonOAuthClient.fetch_identity_for_code",
                AsyncMock(),
            ) as fetch_identity,
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_patreon_link", AsyncMock()) as upsert_link,
        ):
            response = await cog._handle_patreon_oauth_callback("oauth-code", state)

        body = response.body.decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("@attacker", body)
        self.assertNotIn("789", body)
        self.assertIn(patreon_link_confirm_token("patreon-client-secret", "oauth-code", state), body)
        fetch_identity.assert_not_awaited()
        upsert_link.assert_not_awaited()

    async def test_patreon_token_exchange_failure_logs_one_line_without_traceback(self) -> None:
        cog = CapturingOAuthPatreonWhitelistFlowCog()
        cog.bot = SimpleNamespace(settings=self._settings())
        state = build_patreon_oauth_state(
            secret="patreon-client-secret",
            discord_user_id=456,
            guild_id=111,
            action="link",
            expires_at=9999999999,
        )
        confirm = patreon_link_confirm_token("patreon-client-secret", "junk-code", state)

        with (
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.PatreonOAuthClient.fetch_identity_for_code",
                AsyncMock(side_effect=HTTPError("401 Client Error")),
            ),
            self.assertLogs("bulmaai.cogs.patreon_whitelist_flow", "WARNING") as logs,
        ):
            response = await cog._handle_patreon_oauth_callback("junk-code", state, confirm=confirm)

        self.assertEqual(response.status, 502)
        self.assertEqual([record.exc_info for record in logs.records], [None])

    async def test_replayed_patreon_oauth_state_is_processed_once(self) -> None:
        member = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
            guild=SimpleNamespace(id=111),
        )
        bot = SimpleNamespace(settings=self._settings())
        cog = CapturingOAuthPatreonWhitelistFlowCog()
        cog.bot = bot
        cog.member = member
        state = build_patreon_oauth_state(
            secret="patreon-client-secret",
            discord_user_id=456,
            guild_id=111,
            action="beta_access",
            expires_at=9999999999,
            minecraft_username="NewTester",
        )
        identity = PatreonIdentity(
            access_token="patreon-access-token",
            status=PatreonMemberStatus(
                patreon_user_id="patreon-user-1",
                member_id="member-1",
                full_name="Patron User",
                patron_status="active_patron",
                tier_ids=("1287877272224665640",),
                last_charge_date=None,
            ),
        )

        with (
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.PatreonOAuthClient.fetch_identity_for_code",
                AsyncMock(return_value=identity),
            ) as fetch_identity,
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_patreon_link", AsyncMock()) as upsert_link,
        ):
            confirm = patreon_link_confirm_token("patreon-client-secret", "oauth-code", state)
            first_response = await cog._handle_patreon_oauth_callback("oauth-code", state, confirm=confirm)
            second_response = await cog._handle_patreon_oauth_callback("oauth-code", state, confirm=confirm)

        self.assertEqual(first_response.status, 200)
        self.assertEqual(second_response.status, 200)
        self.assertEqual(fetch_identity.await_count, 1)
        self.assertEqual(upsert_link.await_count, 1)
        self.assertEqual(len(cog.staff_logs), 1)
        self.assertEqual(len(cog.calls), 1)
        self.assertIn("already processed", second_response.body.decode("utf-8"))

    async def test_gift_beta_auto_merges_for_active_patron(self) -> None:
        staff_channel = FakeChannel()
        author = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        recipient = SimpleNamespace(
            id=789,
            name="Gifted",
            mention="<@789>",
            bot=False,
        )
        bot = SimpleNamespace(
            settings=self._settings(),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()
        ctx = FakeCommandContext(author=author, channel=FakeChannel())
        link = PatreonLink(
            discord_user_id=456,
            discord_username="Requester",
            patreon_user_id="patreon-user-1",
            patreon_member_id="member-1",
            patreon_full_name="Patron User",
            patron_status="active_patron",
            tier_ids=("1287877272224665640",),
            last_charge_date=None,
            entitlement_active=True,
        )

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", SimpleNamespace),
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=link)),
            patch("bulmaai.cogs.patreon_whitelist_flow.count_active_gifts_for_owner", AsyncMock(return_value=0)),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog._handle_gift_beta_command(ctx, recipient, "GiftedMC")

        self.assertEqual(ctx.deferred, [{"ephemeral": True}])
        self.assertEqual(cog.gh.put_calls[0]["message"], "Gift beta tester: GiftedMC")
        self.assertEqual(upsert_grant.await_args.args[0].source_pr_url, COMMIT_URL)
        self.assertEqual(upsert_grant.await_args.args[0].kind, PatreonGrantKind.GIFT)
        self.assertEqual(upsert_grant.await_args.args[0].beneficiary_discord_user_id, 789)
        self.assertIn("Gift approved automatically", ctx.followup.sent[-1][0][0])
        self.assertIn("auto-approved", staff_channel.sent[-1][0][0])

    async def test_concurrent_gifts_cannot_both_pass_the_gift_limit(self) -> None:
        author = SimpleNamespace(id=456, name="Requester", mention="<@456>", roles=[SimpleNamespace(id=1287877272224665640)])
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = SimpleNamespace(settings=self._settings(), get_channel=lambda channel_id: FakeChannel())
        cog.gh = FakeGitHub()
        link = SimpleNamespace(entitlement_active=True)
        granted = []

        async def slow_lookup(_nickname):
            await asyncio.sleep(0)  # lets the other gift run up to its own limit check
            return True

        async def upsert(grant):
            granted.append(grant)

        def recipient(i):
            return SimpleNamespace(id=789 + i, name=f"Gifted{i}", mention=f"<@{789 + i}>", bot=False)

        self.mojang_lookup.side_effect = slow_lookup
        contexts = [FakeCommandContext(author=author, channel=FakeChannel()) for _ in range(2)]
        count_gifts = AsyncMock(side_effect=lambda _owner_id: len(granted))
        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", SimpleNamespace),
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=link)),
            patch("bulmaai.cogs.patreon_whitelist_flow.count_active_gifts_for_owner", count_gifts),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", upsert),
        ):
            await asyncio.gather(
                *(cog._handle_gift_beta_command(ctx, recipient(i), f"GiftedMC{i}") for i, ctx in enumerate(contexts))
            )

        self.assertEqual(len(granted), 1)
        self.assertIn("gift limit", contexts[1].followup.sent[-1][0][0])

    async def test_gift_beta_flags_ai_log_when_mojang_lookup_fails(self) -> None:
        staff_channel = FakeChannel()
        ai_log_channel = FakeChannel()
        author = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=1287877272224665640)],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        recipient = SimpleNamespace(
            id=789,
            name="Gifted",
            mention="<@789>",
            bot=False,
        )
        settings = self._settings()

        def get_channel(channel_id):
            return ai_log_channel if channel_id == settings.patreon_ai_log_channel_id else staff_channel

        bot = SimpleNamespace(settings=settings, get_channel=get_channel)
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()
        ctx = FakeCommandContext(author=author, channel=FakeChannel())
        link = PatreonLink(
            discord_user_id=456,
            discord_username="Requester",
            patreon_user_id="patreon-user-1",
            patreon_member_id="member-1",
            patreon_full_name="Patron User",
            patron_status="active_patron",
            tier_ids=("1287877272224665640",),
            last_charge_date=None,
            entitlement_active=True,
        )
        self.mojang_lookup.return_value = False

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", SimpleNamespace),
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=link)),
            patch("bulmaai.cogs.patreon_whitelist_flow.count_active_gifts_for_owner", AsyncMock(return_value=0)),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()),
        ):
            await cog._handle_gift_beta_command(ctx, recipient, "GiftedMC")

        self.assertIn("Gift approved automatically", ctx.followup.sent[-1][0][0])
        self.assertEqual(len(ai_log_channel.sent), 1)
        self.assertIn("did not resolve", ai_log_channel.sent[0][0][0])

    def _edit_gift_cog(self, *, gh=None):
        staff_channel = FakeChannel()
        bot = SimpleNamespace(
            settings=self._settings(),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = gh or FakeGitHub()
        return cog, staff_channel

    def _gift_grant(self, *, beneficiary_id=789, nickname="GiftedMC"):
        return PatreonGrant(
            owner_discord_user_id=456,
            beneficiary_discord_user_id=beneficiary_id,
            beneficiary_discord_username="Gifted",
            minecraft_username=nickname,
            kind=PatreonGrantKind.GIFT,
            active=True,
            source_pr_url="https://example.test/pr/1",
        )

    async def test_edit_gift_changes_only_username_for_matching_recipient(self) -> None:
        cog, _staff = self._edit_gift_cog()
        author = SimpleNamespace(id=456, name="Owner", mention="<@456>")
        recipient = SimpleNamespace(id=789, name="Gifted", mention="<@789>", bot=False)
        ctx = FakeCommandContext(author=author, channel=FakeChannel())

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", SimpleNamespace),
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner",
                AsyncMock(return_value=[self._gift_grant()]),
            ),
            patch("bulmaai.cogs.patreon_whitelist_flow.deactivate_gift_grant", AsyncMock()) as deactivate,
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog._handle_edit_gift_command(ctx, recipient, None, "NewMC")

        self.assertEqual(deactivate.await_count, 0)
        self.assertEqual(len(cog.gh.put_calls), 1)
        grant = upsert_grant.await_args.args[0]
        self.assertEqual(grant.beneficiary_discord_user_id, 789)
        self.assertEqual(grant.minecraft_username, "NewMC")
        self.assertIn("from `GiftedMC` to `NewMC`", ctx.followup.sent[-1][0][0])

    async def test_edit_gift_moves_gift_to_new_recipient_without_github_change(self) -> None:
        cog, _staff = self._edit_gift_cog()
        author = SimpleNamespace(id=456, name="Owner", mention="<@456>")
        recipient = SimpleNamespace(id=789, name="Gifted", mention="<@789>", bot=False)
        new_recipient = SimpleNamespace(id=999, name="NewGifted", mention="<@999>", bot=False)
        ctx = FakeCommandContext(author=author, channel=FakeChannel())

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", SimpleNamespace),
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner",
                AsyncMock(return_value=[self._gift_grant()]),
            ),
            patch("bulmaai.cogs.patreon_whitelist_flow.deactivate_gift_grant", AsyncMock()) as deactivate,
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog._handle_edit_gift_command(ctx, recipient, new_recipient, None)

        deactivate.assert_awaited_once_with(456, 789)
        self.assertEqual(cog.gh.put_calls, [])
        grant = upsert_grant.await_args.args[0]
        self.assertEqual(grant.beneficiary_discord_user_id, 999)
        self.assertEqual(grant.minecraft_username, "GiftedMC")
        self.assertIn("Moved the Patreon beta gift", ctx.followup.sent[-1][0][0])

    async def test_edit_gift_changes_recipient_and_username_together(self) -> None:
        cog, _staff = self._edit_gift_cog()
        author = SimpleNamespace(id=456, name="Owner", mention="<@456>")
        recipient = SimpleNamespace(id=789, name="Gifted", mention="<@789>", bot=False)
        new_recipient = SimpleNamespace(id=999, name="NewGifted", mention="<@999>", bot=False)
        ctx = FakeCommandContext(author=author, channel=FakeChannel())

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", SimpleNamespace),
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner",
                AsyncMock(return_value=[self._gift_grant()]),
            ),
            patch("bulmaai.cogs.patreon_whitelist_flow.deactivate_gift_grant", AsyncMock()) as deactivate,
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog._handle_edit_gift_command(ctx, recipient, new_recipient, "NewMC")

        deactivate.assert_awaited_once_with(456, 789)
        self.assertEqual(len(cog.gh.put_calls), 1)
        grant = upsert_grant.await_args.args[0]
        self.assertEqual(grant.beneficiary_discord_user_id, 999)
        self.assertEqual(grant.minecraft_username, "NewMC")
        self.assertIn("Moved the Patreon beta gift", ctx.followup.sent[-1][0][0])
        self.assertIn("`GiftedMC` to `NewMC`", ctx.followup.sent[-1][0][0])

    async def test_edit_gift_falls_back_to_single_gift_for_unmatched_recipient(self) -> None:
        cog, _staff = self._edit_gift_cog()
        author = SimpleNamespace(id=456, name="Owner", mention="<@456>")
        # Owner names the member they want the gift to land on; it doesn't match
        # the original (mis-)gifted recipient (789), but it's their only gift.
        target = SimpleNamespace(id=999, name="Correct", mention="<@999>", bot=False)
        ctx = FakeCommandContext(author=author, channel=FakeChannel())

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", SimpleNamespace),
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner",
                AsyncMock(return_value=[self._gift_grant(beneficiary_id=789)]),
            ),
            patch("bulmaai.cogs.patreon_whitelist_flow.deactivate_gift_grant", AsyncMock()) as deactivate,
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog._handle_edit_gift_command(ctx, target, None, None)

        deactivate.assert_awaited_once_with(456, 789)
        grant = upsert_grant.await_args.args[0]
        self.assertEqual(grant.beneficiary_discord_user_id, 999)
        self.assertIn("Moved the Patreon beta gift", ctx.followup.sent[-1][0][0])

    async def test_edit_gift_reports_nothing_to_change(self) -> None:
        cog, _staff = self._edit_gift_cog()
        author = SimpleNamespace(id=456, name="Owner", mention="<@456>")
        recipient = SimpleNamespace(id=789, name="Gifted", mention="<@789>", bot=False)
        ctx = FakeCommandContext(author=author, channel=FakeChannel())

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", SimpleNamespace),
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner",
                AsyncMock(return_value=[self._gift_grant()]),
            ),
            patch("bulmaai.cogs.patreon_whitelist_flow.deactivate_gift_grant", AsyncMock()) as deactivate,
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog._handle_edit_gift_command(ctx, recipient, None, "GiftedMC")

        self.assertEqual(deactivate.await_count, 0)
        self.assertEqual(upsert_grant.await_count, 0)
        self.assertIn("Nothing to change", ctx.followup.sent[-1][0][0])

    async def test_edit_gift_without_any_gift_reports_clearly(self) -> None:
        cog, _staff = self._edit_gift_cog()
        author = SimpleNamespace(id=456, name="Owner", mention="<@456>")
        recipient = SimpleNamespace(id=789, name="Gifted", mention="<@789>", bot=False)
        ctx = FakeCommandContext(author=author, channel=FakeChannel())

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", SimpleNamespace),
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner",
                AsyncMock(return_value=[]),
            ),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog._handle_edit_gift_command(ctx, recipient, None, "NewMC")

        self.assertEqual(upsert_grant.await_count, 0)
        self.assertIn("don't have any active Patreon gift", ctx.followup.sent[-1][0][0])

    async def test_edit_gift_requires_recipient_choice_with_multiple_gifts(self) -> None:
        cog, _staff = self._edit_gift_cog()
        author = SimpleNamespace(id=456, name="Owner", mention="<@456>")
        unknown = SimpleNamespace(id=555, name="Unknown", mention="<@555>", bot=False)
        ctx = FakeCommandContext(author=author, channel=FakeChannel())

        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.discord.Member", SimpleNamespace),
            patch(
                "bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner",
                AsyncMock(
                    return_value=[
                        self._gift_grant(beneficiary_id=789, nickname="GiftA"),
                        self._gift_grant(beneficiary_id=790, nickname="GiftB"),
                    ]
                ),
            ),
            patch("bulmaai.cogs.patreon_whitelist_flow.upsert_whitelist_grant", AsyncMock()) as upsert_grant,
        ):
            await cog._handle_edit_gift_command(ctx, unknown, None, "NewMC")

        self.assertEqual(upsert_grant.await_count, 0)
        self.assertIn("don't have an active Patreon gift for", ctx.followup.sent[-1][0][0])

    async def test_expired_patreon_removal_removes_self_and_gifted_whitelist_entries(self) -> None:
        staff_channel = FakeChannel()
        bot = SimpleNamespace(
            settings=self._settings(),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHubWithGrantNames()
        grants = [
            PatreonGrant(
                owner_discord_user_id=456,
                beneficiary_discord_user_id=456,
                beneficiary_discord_username="Requester",
                minecraft_username="OwnerMC",
                kind=PatreonGrantKind.SELF,
                active=True,
                source_pr_url=None,
            ),
            PatreonGrant(
                owner_discord_user_id=456,
                beneficiary_discord_user_id=789,
                beneficiary_discord_username="Gifted",
                minecraft_username="GiftedMC",
                kind=PatreonGrantKind.GIFT,
                active=True,
                source_pr_url=None,
            ),
        ]

        await cog._remove_whitelist_grants(456, grants, "declined_patron")

        self.assertEqual(len(cog.gh.put_calls), 1)
        self.assertEqual(cog.gh.put_calls[0]["new_text"], "KeepMe\n")
        self.assertNotIn("456", cog.gh.put_calls[0]["message"])  # public repo: no Discord IDs

    def _revoke_cog(self, gh):
        staff_channel = FakeChannel()
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = SimpleNamespace(settings=self._settings(), get_channel=lambda channel_id: staff_channel, guilds=[])
        cog.gh = gh
        grants = [
            PatreonGrant(456, 456, "Requester", "OwnerMC", PatreonGrantKind.SELF, True),
            PatreonGrant(456, 789, "Gifted", "GiftedMC", PatreonGrantKind.GIFT, True),
        ]
        return cog, grants

    def _link(self):
        return PatreonLink(456, "Requester", "p-user", "member-1", "Pat", "active_patron", ("1287877272224665640",), None, True)

    async def test_webhook_lapse_removes_self_and_gift_even_when_member_lookup_404s(self) -> None:
        cog, grants = self._revoke_cog(FakeGitHubWithGrantNames())
        cog.bot.settings.patreon_webhook_secret = "secret"
        not_found = HTTPError("404")
        not_found.response = SimpleNamespace(status_code=404)
        body = (
            b'{"data":{"id":"member-1","type":"member","attributes":{"patron_status":"former_patron"},'
            b'"relationships":{"user":{"data":{"id":"p-user","type":"user"}},"currently_entitled_tiers":{"data":[]}}}}'
        )
        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.verify_patreon_webhook_signature", return_value=True),
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link_by_member_id", AsyncMock(return_value=self._link())),
            patch("bulmaai.cogs.patreon_whitelist_flow.PatreonCreatorClient") as client_cls,
            patch("bulmaai.cogs.patreon_whitelist_flow.update_link_entitlement", AsyncMock()) as update_link,
            patch("bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner", AsyncMock(return_value=grants)),
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=self._link())),
            patch("bulmaai.cogs.patreon_whitelist_flow.deactivate_grants_for_owner", AsyncMock()) as deactivate,
        ):
            client_cls.return_value.fetch_member_status = AsyncMock(side_effect=not_found)
            response = await cog._handle_patreon_webhook(body, {})

        self.assertEqual(response.status, 202)
        self.assertFalse(update_link.await_args.kwargs["entitlement_active"])
        self.assertEqual(cog.gh.put_calls[0]["new_text"], "KeepMe\n")
        deactivate.assert_awaited_once_with(456)

    async def test_revoke_keeps_grants_active_when_github_fails(self) -> None:
        cog, grants = self._revoke_cog(FakeGitHubWithGrantNames())
        cog.gh.put_whitelist_file = AsyncMock(side_effect=RuntimeError("github down"))
        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner", AsyncMock(return_value=grants)),
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=None)),
            patch("bulmaai.cogs.patreon_whitelist_flow.deactivate_grants_for_owner", AsyncMock()) as deactivate,
        ):
            with self.assertRaises(RuntimeError):
                await cog.revoke_owner_access(456, "former_patron")
        deactivate.assert_not_called()

    async def test_losing_patreon_role_revokes_self_and_gift(self) -> None:
        cog, grants = self._revoke_cog(FakeGitHubWithGrantNames())
        before = SimpleNamespace(id=456, roles=[SimpleNamespace(id=1287877272224665640)])
        after = SimpleNamespace(id=456, roles=[])
        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.ROLE_LOSS_GRACE_SECONDS", 0),
            patch("bulmaai.cogs.patreon_whitelist_flow.list_active_grants_for_owner", AsyncMock(return_value=grants)),
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=None)),
            patch("bulmaai.cogs.patreon_whitelist_flow.deactivate_grants_for_owner", AsyncMock()) as deactivate,
        ):
            await cog.on_member_update(before, after)
        self.assertEqual(cog.gh.put_calls[0]["new_text"], "KeepMe\n")
        deactivate.assert_awaited_once_with(456)

    async def test_revoke_skipped_while_patreon_reports_active(self) -> None:
        cog, grants = self._revoke_cog(FakeGitHubWithGrantNames())
        cog._patreon_verdict = AsyncMock(return_value=(True, None))
        with patch("bulmaai.cogs.patreon_whitelist_flow.deactivate_grants_for_owner", AsyncMock()) as deactivate:
            self.assertEqual(await cog.revoke_owner_access(456, "Patreon role missing"), [])
        self.assertEqual(cog.gh.put_calls, [])
        deactivate.assert_not_called()

    async def test_role_swap_within_grace_period_keeps_access(self) -> None:
        cog, _grants = self._revoke_cog(FakeGitHubWithGrantNames())
        before = SimpleNamespace(id=456, roles=[SimpleNamespace(id=1287877272224665640)])
        after = SimpleNamespace(id=456, roles=[])
        upgraded = SimpleNamespace(id=456, roles=[SimpleNamespace(id=1287877305259130900)])
        cog._resolve_member_across_guilds = AsyncMock(return_value=upgraded)
        cog.revoke_owner_access = AsyncMock()
        with patch("bulmaai.cogs.patreon_whitelist_flow.ROLE_LOSS_GRACE_SECONDS", 0):
            await cog.on_member_update(before, after)
        cog.revoke_owner_access.assert_not_called()

    async def test_sync_revokes_owner_whose_patreon_lapsed(self) -> None:
        cog, _grants = self._revoke_cog(FakeGitHub())
        cog._resolve_member_across_guilds = AsyncMock(
            return_value=SimpleNamespace(id=456, roles=[SimpleNamespace(id=1287877272224665640)])
        )
        cog.revoke_owner_access = AsyncMock()
        lapsed = PatreonMemberStatus("p-user", "member-1", "Pat", "declined_patron", (), None)
        with (
            patch("bulmaai.cogs.patreon_whitelist_flow.list_linked_owner_ids_with_active_grants", AsyncMock(return_value=[456])),
            patch("bulmaai.cogs.patreon_whitelist_flow.get_patreon_link", AsyncMock(return_value=self._link())),
            patch("bulmaai.cogs.patreon_whitelist_flow.PatreonCreatorClient") as client_cls,
            patch("bulmaai.cogs.patreon_whitelist_flow.update_link_entitlement", AsyncMock()),
            patch("bulmaai.cogs.patreon_whitelist_flow.asyncio.sleep", AsyncMock()),
        ):
            client_cls.return_value.fetch_member_status = AsyncMock(return_value=lapsed)
            await cog.sync_patreon_access.coro(cog)
        cog.revoke_owner_access.assert_awaited_once_with(456, "Patreon status `declined_patron`")

    async def test_beta_access_rejects_invalid_minecraft_username_immediately(self) -> None:
        bot = SimpleNamespace(settings=SimpleNamespace(
                patreon_access_role_ids=(123,),
                dev_jar_tester_role_ids=(),
                patreon_staff_channel_id=1493390527004147876,
                patreon_admin_ping_role_id=1309022450671161476,
                patreon_contributor_role_id=1287877272224665640,
                patreon_benefactor_role_id=1287877305259130900,
            ))
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()

        request_channel = FakeChannel()
        member = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=123)],
            guild_permissions=SimpleNamespace(administrator=False),
        )

        await cog.start_whitelist_flow_for_user(
            member,
            request_channel,
            "bad/name",
        )

        self.assertIn("Invalid Minecraft username", request_channel.sent[0][0][0])

    async def test_start_flow_rejects_missing_minecraft_username_safely(self) -> None:
        bot = SimpleNamespace(settings=SimpleNamespace(
                patreon_access_role_ids=(123,),
                dev_jar_tester_role_ids=(),
                patreon_staff_channel_id=1493390527004147876,
                patreon_admin_ping_role_id=1309022450671161476,
                patreon_contributor_role_id=1287877272224665640,
                patreon_benefactor_role_id=1287877305259130900,
            ))
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()

        request_channel = FakeChannel()
        member = SimpleNamespace(
            id=456,
            name="Requester",
            mention="<@456>",
            roles=[SimpleNamespace(id=123)],
            guild_permissions=SimpleNamespace(administrator=False),
        )

        await cog.start_whitelist_flow_for_user(
            member,
            request_channel,
            None,  # type: ignore[arg-type]
        )

        self.assertIn("Invalid Minecraft username", request_channel.sent[0][0][0])


if __name__ == "__main__":
    unittest.main()


class ManualWhitelistKeysTests(unittest.TestCase):
    def test_names_above_auto_header_are_protected(self):
        from bulmaai.cogs.patreon_whitelist_flow import _manual_whitelist_keys

        lines = ["# Manually added", "Dev", "KyoSleep", "# Auto-added by the bot", "SomePatron"]
        self.assertEqual(_manual_whitelist_keys(lines), {"dev", "kyosleep"})
        self.assertEqual(_manual_whitelist_keys(["Dev", "SomePatron"]), set())
