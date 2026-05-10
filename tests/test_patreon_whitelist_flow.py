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


class FakeGitHub:
    def __init__(self):
        self.base_branch = "main"
        self.created_branches = []
        self.put_calls = []

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


if __name__ == "__main__":
    unittest.main()
