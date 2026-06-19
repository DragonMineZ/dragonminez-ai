import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from requests import HTTPError

from bulmaai.cogs.patreon_whitelist_flow import PatreonWhitelistFlowCog
from bulmaai.services.discord_oauth import build_discord_oauth_state
from bulmaai.services.patreon_access import (
    PatreonIdentity,
    PatreonMemberStatus,
    build_patreon_oauth_state,
    parse_patreon_oauth_state,
)
from bulmaai.services.patreon_grants import PatreonGrant, PatreonGrantKind, PatreonLink
from bulmaai.ui.patreon_views import AdminPRView


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


class FakeGitHub:
    def __init__(self):
        self.base_branch = "main"
        self.created_branches = []
        self.put_calls = []
        self.merged_prs = []
        self.closed_prs = []
        self.removed_branches = []
        self.comments = []

    async def create_branch(self, new_branch, from_branch):
        self.created_branches.append((new_branch, from_branch))

    async def get_whitelist_file(self, ref):
        if ref == "main":
            return "ExistingUser\n", "base-sha"
        return "ExistingUser\n", "branch-sha"

    async def put_whitelist_file(self, *, branch, new_text, sha, message):
        self.put_calls.append(
            {
                "branch": branch,
                "new_text": new_text,
                "sha": sha,
                "message": message,
            }
        )

    async def create_pr(self, *, head_branch, title, body):
        return {"number": 12, "html_url": "https://example.test/pr/12"}

    async def create_or_get_pr(self, *, head_branch, title, body):
        return await self.create_pr(head_branch=head_branch, title=title, body=body)

    async def merge_pr(self, pr_number):
        self.merged_prs.append(pr_number)

    async def close_pr(self, pr_number):
        self.closed_prs.append(pr_number)

    async def add_pr_comment(self, pr_number, comment):
        self.comments.append((pr_number, comment))

    async def remove_branch(self, branch):
        self.removed_branches.append(branch)


class FakeGitHubWithExistingBranchNick(FakeGitHub):
    async def get_whitelist_file(self, ref):
        if ref == "main":
            return "ExistingUser\n", "base-sha"
        return "ExistingUser\nNewTester\n", "branch-sha"


class FakeGitHubWithGrantNames(FakeGitHub):
    async def get_whitelist_file(self, ref):
        return "OwnerMC\nGiftedMC\nKeepMe\n", "sha"


class FakeGitHubWithBaseNick(FakeGitHub):
    async def get_whitelist_file(self, ref):
        if ref == "main":
            return "ExistingUser\nNewTester\n", "base-sha"
        return "ExistingUser\nNewTester\n", "branch-sha"


class FakeGitHubWithOldSelfGrantNick(FakeGitHub):
    async def get_whitelist_file(self, ref):
        return "ExistingUser\nOldTester\n", "sha"


class FakeGitHubSlowMerge(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.base_text = "ExistingUser\n"
        self.branch_text = "ExistingUser\n"

    async def get_whitelist_file(self, ref):
        if ref == "main":
            return self.base_text, "base-sha"
        return self.branch_text, "branch-sha"

    async def put_whitelist_file(self, *, branch, new_text, sha, message):
        await super().put_whitelist_file(branch=branch, new_text=new_text, sha=sha, message=message)
        self.branch_text = new_text

    async def merge_pr(self, pr_number):
        self.merged_prs.append(pr_number)
        await asyncio.sleep(0.01)
        self.base_text = self.branch_text


class FakeGitHubMergeMethodNotAllowed(FakeGitHub):
    async def merge_pr(self, pr_number):
        self.merged_prs.append(pr_number)
        error = HTTPError("405 Client Error: Method Not Allowed")
        error.response = SimpleNamespace(status_code=405)
        raise error

    async def get_pr(self, pr_number):
        return {
            "number": pr_number,
            "html_url": "https://example.test/pr/12",
            "merged": False,
            "state": "open",
        }


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


class FakeAdminMember:
    id = 999

    def __init__(self):
        self.guild_permissions = SimpleNamespace(administrator=True)


class PatreonWhitelistFlowTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self):
        return SimpleNamespace(
            discord_oauth_client_id="discord-client-id",
            discord_oauth_client_secret="discord-client-secret",
            discord_oauth_redirect_uri="https://downloads.example.test/beta-access/discord/callback",
            patreon_access_role_ids=(1287877272224665640, 1287877305259130900),
            patreon_eligible_tier_ids=("1287877272224665640", "1287877305259130900"),
            patreon_oauth_client_id="patreon-client-id",
            patreon_oauth_client_secret="patreon-client-secret",
            patreon_oauth_redirect_uri="https://downloads.dragonminez.com/patreon/oauth/callback",
            PATREON_CAMPAIGN_ID="12861895",
            PATREON_CREATOR_TOKEN="creator-token",
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
        )

        with patch(
            "bulmaai.cogs.patreon_whitelist_flow.DiscordOAuthClient.fetch_user_id_for_code",
            AsyncMock(return_value=456),
        ):
            response = await cog._handle_beta_access_discord_callback(
                code="oauth-code",
                state=state,
                now=lambda: 1999,
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(cog.calls), 1)
        called_member, destination, nickname, ephemeral = cog.calls[0]
        self.assertIs(called_member, member)
        self.assertEqual(nickname, "NewTester")
        self.assertFalse(ephemeral)
        self.assertIn("Verification request accepted", response.body.decode("utf-8"))
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
            response = await cog._handle_patreon_oauth_callback("oauth-code", state)

        body = response.body.decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("NewTester", body)
        self.assertIn("approved automatically", body)
        self.assertEqual(cog.gh.merged_prs, [12])
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
        bot = SimpleNamespace(settings=SimpleNamespace(patreon_access_role_ids=(123,)))
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

        self.assertEqual(cog.gh.merged_prs, [12])
        self.assertEqual(cog.gh.removed_branches, ["patreon/user-456"])
        self.assertEqual(upsert_grant.await_args.args[0].kind, PatreonGrantKind.SELF)
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
        cog.gh = FakeGitHubSlowMerge()
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

        self.assertEqual(cog.gh.merged_prs, [12])
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
        cog.gh = FakeGitHubSlowMerge()
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

        self.assertEqual(cog.gh.merged_prs, [12])
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

    async def test_auto_approval_falls_back_to_staff_review_when_github_refuses_merge(self) -> None:
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
        cog.gh = FakeGitHubMergeMethodNotAllowed()
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

        self.assertEqual(cog.gh.merged_prs, [12])
        self.assertEqual(upsert_grant.await_count, 0)
        self.assertIn(
            "Whitelist PR created, but GitHub would not auto-merge it yet.",
            destination.sent[-1][0][0],
        )
        self.assertIn("https://example.test/pr/12", destination.sent[-1][0][0])
        self.assertIn("could not be auto-merged", staff_channel.sent[-1][0][0])

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
        self.assertEqual(cog.gh.merged_prs, [])
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

        self.assertEqual(cog.gh.created_branches, [("patreon/user-456", "main")])
        self.assertEqual(cog.gh.put_calls[0]["new_text"], "ExistingUser\nNewTester\n")
        self.assertEqual(cog.gh.put_calls[0]["message"], "Update beta tester: OldTester -> NewTester")
        self.assertEqual(cog.gh.merged_prs, [12])
        self.assertEqual(upsert_grant.await_args.args[0].minecraft_username, "NewTester")
        self.assertIn("updated from `OldTester` to `NewTester`", interaction.followup.sent[-1][0][0])
        self.assertIn("updated Patreon beta access", staff_channel.sent[-1][0][0])

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
            first_response = await cog._handle_patreon_oauth_callback("oauth-code", state)
            second_response = await cog._handle_patreon_oauth_callback("oauth-code", state)

        self.assertEqual(first_response.status, 200)
        self.assertEqual(second_response.status, 200)
        self.assertEqual(fetch_identity.await_count, 1)
        self.assertEqual(upsert_link.await_count, 1)
        self.assertEqual(len(cog.staff_logs), 1)
        self.assertEqual(len(cog.calls), 1)
        self.assertIn("already processed", second_response.body.decode("utf-8"))

    async def test_gift_beta_creates_staff_approval_pr_for_active_patron(self) -> None:
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
        ):
            await cog._handle_gift_beta_command(ctx, recipient, "GiftedMC")

        self.assertEqual(ctx.deferred, [{"ephemeral": True}])
        self.assertEqual(cog.gh.created_branches, [("patreon/gift-456-789", "main")])
        self.assertIn("Gift submitted for staff approval", ctx.followup.sent[-1][0][0])
        self.assertIn("view", staff_channel.sent[0][1])

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
        self.assertEqual(cog.gh.merged_prs, [12])
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
        self.assertEqual(cog.gh.merged_prs, [])
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
        self.assertEqual(cog.gh.merged_prs, [12])
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

        self.assertEqual(cog.gh.created_branches, [("patreon/remove-456", "main")])
        self.assertEqual(cog.gh.put_calls[0]["new_text"], "KeepMe\n")
        self.assertEqual(cog.gh.merged_prs, [12])
        self.assertEqual(cog.gh.removed_branches, ["patreon/remove-456"])

    async def test_beta_access_rejects_invalid_minecraft_username_immediately(self) -> None:
        bot = SimpleNamespace(settings=SimpleNamespace(patreon_access_role_ids=(123,)))
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
        bot = SimpleNamespace(settings=SimpleNamespace(patreon_access_role_ids=(123,)))
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

    async def test_user_confirm_uses_user_id_branch_and_branch_file_sha(self) -> None:
        staff_channel = FakeChannel()
        bot = SimpleNamespace(
            settings=SimpleNamespace(patreon_access_role_ids=(123,)),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=456, name="bad/name", mention="<@456>"),
            message=FakeMessage(),
            followup=FakeFollowup(),
        )

        await cog._submit_whitelist_request(
            interaction=interaction,
            initial_nick="NewTester",
        )

        self.assertEqual(cog.gh.created_branches, [("patreon/user-456", "main")])
        self.assertEqual(cog.gh.put_calls[0]["branch"], "patreon/user-456")
        self.assertEqual(cog.gh.put_calls[0]["sha"], "branch-sha")
        self.assertEqual(cog.gh.put_calls[0]["new_text"], "ExistingUser\nNewTester\n")

    async def test_user_confirm_skips_branch_write_when_retry_branch_already_has_nick(self) -> None:
        staff_channel = FakeChannel()
        bot = SimpleNamespace(
            settings=SimpleNamespace(patreon_access_role_ids=(123,)),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHubWithExistingBranchNick()
        interaction = SimpleNamespace(
            user=FakeUser(name="Requester"),
            message=FakeMessage(),
            followup=FakeFollowup(),
        )

        await cog._submit_whitelist_request(
            interaction=interaction,
            initial_nick="NewTester",
        )

        self.assertEqual(cog.gh.put_calls, [])

    async def test_existing_whitelisted_user_edits_prompt_instead_of_followup(self) -> None:
        bot = SimpleNamespace(
            settings=SimpleNamespace(patreon_access_role_ids=(123,)),
            get_channel=lambda channel_id: FakeChannel(),
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()
        user_message = FakeMessage()
        interaction = SimpleNamespace(
            user=FakeUser(name="Requester"),
            message=user_message,
            followup=FakeFollowup(),
        )

        await cog._submit_whitelist_request(
            interaction=interaction,
            initial_nick="ExistingUser",
        )

        self.assertEqual(interaction.followup.sent, [])
        self.assertEqual(
            user_message.edits[-1],
            {
                "content": "`ExistingUser` is already whitelisted. Nothing to do.",
                "view": None,
            },
        )

    async def test_submitted_request_edits_prompt_instead_of_followup(self) -> None:
        staff_channel = FakeChannel()
        bot = SimpleNamespace(
            settings=SimpleNamespace(patreon_access_role_ids=(123,)),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()
        user_message = FakeMessage()
        interaction = SimpleNamespace(
            user=FakeUser(name="Requester"),
            message=user_message,
            followup=FakeFollowup(),
        )

        await cog._submit_whitelist_request(
            interaction=interaction,
            initial_nick="NewTester",
        )

        self.assertEqual(interaction.followup.sent, [])
        self.assertEqual(
            user_message.edits[-1],
            {
                "content": "Request submitted. Please wait for an administrator to approve.",
                "view": None,
            },
        )

    async def test_admin_approval_updates_user_message_and_dms_requester(self) -> None:
        staff_channel = FakeChannel()
        bot = SimpleNamespace(
            settings=SimpleNamespace(patreon_access_role_ids=(123,)),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()
        requester = FakeUser(name="Requester")
        user_message = FakeMessage()
        user_interaction = SimpleNamespace(
            user=requester,
            message=user_message,
            followup=FakeFollowup(),
        )

        await cog._submit_whitelist_request(
            interaction=user_interaction,
            initial_nick="NewTester",
        )
        admin_view = staff_channel.sent[0][1]["view"]
        admin_interaction = SimpleNamespace(
            user=FakeUser(user_id=999, name="Staffer", mention="<@999>"),
            followup=FakeFollowup(),
        )

        await admin_view.on_confirm(admin_interaction)

        self.assertEqual(user_message.edits[-1], {"content": "Success", "view": None})
        self.assertEqual(
            requester.dms,
            [
                "Congratulations, Staffer has approved your request and you now have access to the latest previews!"
            ],
        )

    async def test_admin_rejection_updates_user_message_and_dms_requester(self) -> None:
        staff_channel = FakeChannel()
        bot = SimpleNamespace(
            settings=SimpleNamespace(patreon_access_role_ids=(123,)),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()
        requester = FakeUser(name="Requester")
        user_message = FakeMessage()
        user_interaction = SimpleNamespace(
            user=requester,
            message=user_message,
            followup=FakeFollowup(),
        )

        await cog._submit_whitelist_request(
            interaction=user_interaction,
            initial_nick="NewTester",
        )
        admin_view = staff_channel.sent[0][1]["view"]
        admin_interaction = SimpleNamespace(
            user=FakeUser(user_id=999, name="Staffer", mention="<@999>"),
            followup=FakeFollowup(),
        )

        await admin_view.on_reject(admin_interaction)

        self.assertEqual(user_message.edits[-1], {"content": "Rejected", "view": None})
        self.assertEqual(
            requester.dms,
            [
                "Your Patreon whitelist request was rejected by Staffer. Please contact staff if you think this was a mistake."
            ],
        )

    async def test_admin_confirm_button_disables_before_callback_and_ignores_second_click(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        confirm_calls = 0

        async def on_confirm(_interaction):
            nonlocal confirm_calls
            confirm_calls += 1
            started.set()
            await release.wait()

        async def on_edit(_interaction, _new_nick):
            raise AssertionError("edit should not run")

        async def on_reject(_interaction):
            raise AssertionError("reject should not run")

        view = AdminPRView(
            pr_number=12,
            nickname="NewTester",
            branch="patreon/user-456",
            on_confirm=on_confirm,
            on_edit=on_edit,
            on_reject=on_reject,
        )
        first_interaction = FakeButtonInteraction(user=FakeAdminMember())
        second_interaction = FakeButtonInteraction(user=FakeAdminMember())

        with patch("bulmaai.ui.patreon_views.discord.Member", FakeAdminMember):
            first_task = asyncio.create_task(view.children[0].callback(first_interaction))
            await started.wait()
            self.assertTrue(all(child.disabled for child in view.children))

            second_task = asyncio.create_task(view.children[0].callback(second_interaction))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first_task, second_task)

        self.assertEqual(confirm_calls, 1)
        self.assertIn("already being processed", second_interaction.response.sent[0][0][0])


if __name__ == "__main__":
    unittest.main()
