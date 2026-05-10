import unittest
from types import SimpleNamespace

from bulmaai.cogs.patreon_whitelist_flow import PatreonWhitelistFlowCog


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


class PatreonWhitelistFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_confirm_uses_user_id_branch_and_branch_file_sha(self) -> None:
        staff_channel = FakeChannel()
        bot = SimpleNamespace(
            settings=SimpleNamespace(patreon_access_role_ids=(123,)),
            get_channel=lambda channel_id: staff_channel,
        )
        cog = PatreonWhitelistFlowCog.__new__(PatreonWhitelistFlowCog)
        cog.bot = bot
        cog.gh = FakeGitHub()

        request_channel = FakeChannel()
        member = SimpleNamespace(
            id=456,
            name="bad/name",
            mention="<@456>",
            roles=[SimpleNamespace(id=123)],
            guild_permissions=SimpleNamespace(administrator=False),
        )

        result = await cog.start_whitelist_flow_for_user(
            member,
            request_channel,
            "NewTester",
        )

        self.assertEqual(result, "flow_started")
        view = request_channel.sent[0][1]["view"]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=456, name="bad/name", mention="<@456>"),
            followup=FakeFollowup(),
        )

        await view.on_confirm(interaction, "NewTester")

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
