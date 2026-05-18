import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bulmaai.cogs.patreon_whitelist_flow import PatreonWhitelistFlowCog
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


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


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


class PatreonWhitelistFlowTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self):
        return SimpleNamespace(
            patreon_access_role_ids=(1287877272224665640, 1287877305259130900),
            patreon_eligible_tier_ids=("1287877272224665640", "1287877305259130900"),
            patreon_oauth_client_id="patreon-client-id",
            patreon_oauth_client_secret="patreon-client-secret",
            patreon_oauth_redirect_uri="https://downloads.dragonminez.com/patreon/oauth/callback",
            PATREON_CAMPAIGN_ID="12861895",
            PATREON_CREATOR_TOKEN="creator-token",
        )

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

        self.assertIn("You need a Patreon beta access role", request_channel.sent[0][0][0])

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


if __name__ == "__main__":
    unittest.main()
