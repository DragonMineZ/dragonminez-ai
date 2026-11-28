import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bulmaai.cogs import tickets as cog_module
from bulmaai.github.github_service import GitHubService
from bulmaai.services import patreon_perks, perk_ship
from bulmaai.services.perk_ship import ShipError
from bulmaai.ui import patreon_perks as perks_ui
from test_patreon_perks import YUSEIX, _claim, _cog
from test_tickets import click, make_ticket, member
from v2_helpers import buttons, sent_message, text

PNG = perk_ship.PNG_MAGIC + b"pixels"
TAG = '{\r\n  "replace": false,\r\n  "values": [\r\n    {\r\n      "id": "dragonminez:zamasu_armor_boots",\r\n      "required": false\r\n    }\r\n  ]\r\n}'
FILES = {
    perk_ship.MAIN_ITEMS: (
        "public class MainItems {\n"
        '\tpublic static final Map<ArmorItem.Type, RegistryObject<Item>> ZAMASU_ARMOR = fullArmorNoHelmetSet("zamasu_armor", "zamasu_gi");\n'
        '\tpublic static final RegistryObject<Item> PATTERN = regItem("pattern");\n}\n'
    ),
    perk_ship.ITEM_MODELS: (
        "    void armor() {\n        generateArmorSetModels(MainItems.ZAMASU_ARMOR);\n"
        "        //generateArmorSetModels(MainItems.OLD_ARMOR);\n    }\n"
    ),
    perk_ship.LANG: '{\n  "item.dragonminez.zamasu_armor_boots": "Zamasu Boots",\n  "item.dragonminez.sword": "Sword"\n}\n',
    **{path: TAG for path in perk_ship.TAGS.values()},
}
HAIR = (
    "public class HairPresets {\n\tstatic {\n\t\t// Goku\n\t\tregister(1, \"DMZF1:aaa\");\n"
    "\t\t// Gojo Patreon (Re:Wheezer)\n\t\tregister(27, \"DMZF1:bbb\");\n\t}\n"
    "\tpublic static void register(int presetId, String code) {\n\t}\n}\n"
)


def _armor(**overrides):
    values = dict(display_name="Ultra Saiyan Gi", layer1=PNG, layer2=PNG, patron="Goku", claim_id=7)
    return perk_ship.armor_change(FILES, **(values | overrides))


class ArmorChangeTests(unittest.TestCase):
    def test_every_file_gets_the_handmade_lines(self):
        change = _armor()
        main = change.texts[perk_ship.MAIN_ITEMS]
        self.assertIn(
            '\t// ULTRA SAIYAN GI (Patreon: Goku)\n\tpublic static final Map<ArmorItem.Type, RegistryObject<Item>> '
            'ULTRA_SAIYAN_GI_ARMOR = fullArmorNoHelmetSet("ultra_saiyan_gi_armor", "ultra_saiyan_gi");\n\tpublic static final RegistryObject',
            main,
        )
        models = change.texts[perk_ship.ITEM_MODELS]
        self.assertIn("ZAMASU_ARMOR);\n        generateArmorSetModels(MainItems.ULTRA_SAIYAN_GI_ARMOR);\n        //generate", models)
        lang = json.loads(change.texts[perk_ship.LANG])
        self.assertEqual(lang["item.dragonminez.ultra_saiyan_gi_armor_leggings"], "Ultra Saiyan Gi Leggings")
        for piece, path in perk_ship.TAGS.items():
            tag = change.texts[path]
            self.assertNotIn("\n", tag.replace("\r\n", ""))  # CRLF kept
            self.assertEqual(json.loads(tag)["values"][-1], {"id": f"dragonminez:ultra_saiyan_gi_armor_{piece}", "required": False})
        self.assertEqual(sorted(change.binaries), [
            "src/main/resources/assets/dragonminez/textures/armor/ultra_saiyan_gi_layer1.png",
            "src/main/resources/assets/dragonminez/textures/armor/ultra_saiyan_gi_layer2.png",
        ])
        self.assertEqual((change.title, change.branch), ("feat: patreon armor Ultra Saiyan Gi", "perk/7-ultra_saiyan_gi"))

    def test_refusals(self):
        cases = {
            "must be PNG": dict(layer1=b"GIF89a"),
            "over 512 KB": dict(layer2=PNG + b"x" * perk_ship.MAX_TEXTURE_BYTES),
            "already exists": dict(display_name="Zamasu"),
            "must start with a letter": dict(display_name="123 Gi"),
            "can only use": dict(display_name="!!"),
        }
        for message, overrides in cases.items():
            with self.subTest(message), self.assertRaisesRegex(ShipError, message):
                _armor(**overrides)
        taken = frozenset({"src/main/resources/assets/dragonminez/textures/armor/zamasu_gi_layer1.png"})
        with self.assertRaisesRegex(ShipError, "already exists"):
            _armor(display_name="Zamasu Gi", existing_paths=taken)

    def test_unsafe_names_never_reach_the_source(self):
        for overrides in (dict(display_name=r"Evil \u000a Gi"), dict(patron=r"Goku\u000aint"), dict(display_name='Say "hi"'),
                          dict(patron="a`@b")):
            with self.subTest(overrides), self.assertRaisesRegex(ShipError, "can only use"):
                _armor(**overrides)
        with self.assertRaisesRegex(ShipError, "can only use"):
            perk_ship.hair_change(HAIR, preset_name=r"x\u000ay", code="DMZF1:a", patron="Goku", claim_id=1)

    def test_a_change_that_would_break_json_is_refused(self):
        last_key = FILES | {perk_ship.LANG: '{\n  "item.dragonminez.zamasu_armor_boots": "Zamasu Boots"\n}\n'}
        with self.assertRaisesRegex(ShipError, "would break en_us.json"):
            perk_ship.armor_change(last_key, display_name="New Gi", layer1=PNG, layer2=PNG, patron="x", claim_id=1)

    def test_a_statement_split_over_lines_is_not_an_anchor(self):
        split = FILES | {
            perk_ship.MAIN_ITEMS: '\tpublic static final Map<ArmorItem.Type, RegistryObject<Item>> A = fullArmorNoHelmetSet(\n\t\t"a_armor", "a");\n'
        }
        with self.assertRaisesRegex(ShipError, "armor registration"):
            perk_ship.armor_change(split, display_name="New Gi", layer1=PNG, layer2=PNG, patron="x", claim_id=1)

    def test_a_file_that_changed_shape_is_refused(self):
        broken = FILES | {perk_ship.ITEM_MODELS: "nothing here\n"}
        with self.assertRaisesRegex(ShipError, "the item model"):
            perk_ship.armor_change(broken, display_name="New Gi", layer1=PNG, layer2=PNG, patron="x", claim_id=1)


class HairChangeTests(unittest.TestCase):
    def test_next_id_after_the_last_preset(self):
        change = perk_ship.hair_change(HAIR, preset_name="Spiky Blue", code=" DMZF1:abc-_9 ", patron="Goku", claim_id=8)
        self.assertIn(
            '\t\tregister(27, "DMZF1:bbb");\n\t\t// Spiky Blue Patreon (Goku)\n\t\tregister(28, "DMZF1:abc-_9");\n\t}',
            change.texts[perk_ship.HAIR_PRESETS],
        )
        self.assertEqual(change.branch, "perk/8-spiky-blue")

    def test_bad_codes_are_refused(self):
        for code in ("hello", "DMZF1:has space", "DMZF1:" + "a" * perk_ship.MAX_HAIR_CODE, 'DMZF1:a");evil("'):
            with self.subTest(code[:20]), self.assertRaises(ShipError):
                perk_ship.hair_change(HAIR, preset_name="X", code=code, patron="x", claim_id=1)


class OpenPrTests(unittest.IsolatedAsyncioTestCase):
    def github(self):
        return SimpleNamespace(
            head_sha=AsyncMock(return_value="base1"),
            get_file=AsyncMock(side_effect=lambda path, ref, strict=False: (FILES.get(path, HAIR), "blob")),
            list_tree=AsyncMock(return_value=[]),
            open_pr_with_files=AsyncMock(return_value="https://github.com/o/r/pull/9"),
        )

    async def test_clothing_reads_the_base_and_opens_one_pr(self):
        github = self.github()
        url = await perk_ship.open_pr(
            github, base="main", claim_id=7, perk="clothing", perk_label="Custom clothing", patron="Goku",
            ticket_url="https://discord.com/channels/1/900", made_by="Yuse", display_name="Ultra Saiyan Gi",
            layer1=PNG, layer2=PNG,
        )
        self.assertEqual(url, "https://github.com/o/r/pull/9")
        self.assertTrue(all(call.args[1] == "base1" for call in github.get_file.await_args_list))
        kwargs = github.open_pr_with_files.await_args.kwargs
        self.assertEqual((kwargs["base_sha"], kwargs["base"], kwargs["branch"]), ("base1", "main", "perk/7-ultra_saiyan_gi"))
        self.assertEqual(len(kwargs["files"]), 8)  # 6 text files + 2 textures
        self.assertTrue(all(isinstance(content, bytes) for content in kwargs["files"].values()))
        self.assertIn("the bot never merges", kwargs["body"])
        self.assertNotIn("@", kwargs["body"])  # no GitHub pings

    async def test_test_users_get_a_draft_marked_test(self):
        github = self.github()
        await perk_ship.open_pr(
            github, base="main", claim_id=7, perk="hairstyle", perk_label="Custom hairstyle", patron="Goku",
            ticket_url="x", made_by="Yuse", display_name="Spiky", code="DMZF1:abc", test=True,
        )
        kwargs = github.open_pr_with_files.await_args.kwargs
        self.assertEqual((kwargs["title"], kwargs["draft"]), ("[TEST] feat: patreon hair preset Spiky", True))

    async def test_npc_skins_cant_ship(self):
        with self.assertRaises(ShipError):
            await perk_ship.open_pr(self.github(), base="main", claim_id=1, perk="npc_skin", perk_label="x", patron="x",
                                    ticket_url="x", made_by="x")


class GitHubPrTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_commit_on_a_new_branch_then_the_pr(self):
        service = GitHubService(auth=SimpleNamespace(get_installation_token=AsyncMock(return_value="t")), owner="o", repo="r")
        service.list_prs = AsyncMock(return_value=[])
        replies = iter([
            {"tree": {"sha": "tree0"}}, {"sha": "blob1"}, {"sha": "tree1"}, {"sha": "commit1"}, {}, {"html_url": "https://pr"},
        ])
        calls = []

        async def fake_request(method, url, **kwargs):
            calls.append((method, url.removeprefix("https://api.github.com/repos/o/r"), kwargs.get("json")))
            payload = next(replies)
            return MagicMock(json=lambda: payload)

        with patch("bulmaai.github.github_service.request", fake_request):
            url = await service.open_pr_with_files(
                base_sha="base1", base="main", branch="perk/1-x", files={"a.png": b"\x89PNG"}, message="m", title="t", body="b"
            )
        self.assertEqual(url, "https://pr")
        self.assertEqual([(m, u) for m, u, _ in calls], [
            ("GET", "/git/commits/base1"), ("POST", "/git/blobs"), ("POST", "/git/trees"), ("POST", "/git/commits"),
            ("POST", "/git/refs"), ("POST", "/pulls"),
        ])
        self.assertEqual(calls[2][2]["tree"], [{"path": "a.png", "mode": "100644", "type": "blob", "sha": "blob1"}])
        self.assertEqual(calls[3][2]["parents"], ["base1"])
        self.assertEqual(calls[4][2], {"ref": "refs/heads/perk/1-x", "sha": "commit1"})
        self.assertEqual(calls[5][2]["head"], "perk/1-x")


class GitHubRetryTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        return GitHubService(auth=SimpleNamespace(get_installation_token=AsyncMock(return_value="t")), owner="o", repo="r")

    async def test_a_retry_returns_the_pr_already_open(self):
        service = self.service()
        service.list_prs = AsyncMock(return_value=[{"html_url": "https://pr/1"}])
        with patch("bulmaai.github.github_service.request", AsyncMock()) as request:
            url = await service.open_pr_with_files(base_sha="b", base="main", branch="perk/1-x", files={}, message="m", title="t", body="b")
        self.assertEqual(url, "https://pr/1")
        request.assert_not_awaited()
        service.list_prs.assert_awaited_once_with(state="open", per_page=1, head="o:perk/1-x")

    async def test_a_leftover_branch_is_moved_to_the_new_commit(self):
        service = self.service()
        service.list_prs = AsyncMock(return_value=[])
        replies = iter([(200, {"tree": {"sha": "t0"}}), (200, {"sha": "t1"}), (200, {"sha": "c1"}), (422, {}), (200, {}),
                        (201, {"html_url": "https://pr/2"})])
        calls = []

        async def fake_request(method, url, **kwargs):
            calls.append((method, url.rsplit("/repos/o/r", 1)[-1], kwargs.get("json")))
            status, payload = next(replies)
            return MagicMock(status_code=status, json=lambda: payload)

        with patch("bulmaai.github.github_service.request", fake_request):
            url = await service.open_pr_with_files(base_sha="b", base="main", branch="perk/1-x", files={}, message="m", title="t", body="b")
        self.assertEqual(url, "https://pr/2")
        self.assertEqual(calls[4][:2], ("PATCH", "/git/refs/heads/perk/1-x"))
        self.assertEqual(calls[4][2], {"sha": "c1", "force": True})

    async def test_a_truncated_tree_is_an_error(self):
        service = self.service()
        reply = MagicMock(json=lambda: {"truncated": True, "tree": []})
        with patch("bulmaai.github.github_service.request", AsyncMock(return_value=reply)), self.assertRaises(RuntimeError):
            await service.list_tree("main")


class ShipCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepted_shippable_claims_get_ship_it_until_the_pr_exists(self):
        card = perks_ui.claim_card(perk="clothing", claim_id=7, status="accepted", makers=[], decided_by=1)
        self.assertEqual([b.custom_id for b in buttons(card)], ["perk_claim:ship:7"])
        shipped = perks_ui.claim_card(perk="clothing", claim_id=7, status="accepted", makers=[], decided_by=1, pr_url="https://pr")
        self.assertEqual(buttons(shipped), [])
        self.assertIn("[Mod pull request](https://pr)", text(shipped))
        npc = perks_ui.claim_card(perk="npc_skin", claim_id=7, status="accepted", makers=[], decided_by=1)
        self.assertEqual(buttons(npc), [])

    async def test_ship_it_opens_the_perks_form_once(self):
        cog = _cog()
        card = perks_ui.claim_card(perk="hairstyle", claim_id=7, status="accepted", makers=[], decided_by=1)
        for claim, opened in ((replace(_claim("hairstyle", "accepted"), claim_id=7), True),
                              (replace(_claim("hairstyle", "accepted"), claim_id=7, pr_url="https://pr"), False)):
            interaction = click(member(YUSEIX), "perk_claim:ship:7", card)
            interaction.response.send_modal = AsyncMock()
            with patch.object(patreon_perks, "get_claim", AsyncMock(return_value=claim)):
                await cog.on_interaction(interaction)
            if opened:
                self.assertIsInstance(interaction.response.send_modal.await_args.args[0], perks_ui.HairShipModal)
            else:
                interaction.response.send_modal.assert_not_awaited()


class SubmitShipTests(unittest.IsolatedAsyncioTestCase):
    async def ship(self, *, perk="clothing", outcome="https://github.com/o/r/pull/9", files=2, user=YUSEIX):
        cog = _cog()
        cog.settings.notion_token = "ntok"
        cog.settings.mod_source_branch = "main"
        claim = replace(_claim(perk, "accepted"), claim_id=7, minecraft_username="Goku", notion_page_id="abc-def")
        message = sent_message(perks_ui.claim_card(perk=perk, claim_id=7, status="accepted", makers=[], decided_by=YUSEIX))
        message.edit = AsyncMock()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=user, roles=[], display_name="Yuse"),
            guild_id=1,
            message=message,
            channel=SimpleNamespace(send=AsyncMock()),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        uploads = [SimpleNamespace(read=AsyncMock(return_value=PNG)) for _ in range(files)]
        stored = getattr(self, "stored", True)
        open_pr = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
        with patch.object(patreon_perks, "get_claim", AsyncMock(return_value=claim)), \
                patch.object(patreon_perks, "set_pr_url", AsyncMock(return_value=stored)) as set_pr, \
                patch.object(perk_ship, "open_pr", open_pr), \
                patch.object(cog_module, "mod_repo_github", return_value="gh"), \
                patch.object(cog_module.notion, "update_page", AsyncMock()) as update_page, \
                patch.object(cog_module, "get_ticket_by_channel", AsyncMock(return_value=make_ticket(language="pt"))):
            await cog.submit_perk_ship(interaction, 7, display_name="Ultra Saiyan Gi", files=uploads)
        return SimpleNamespace(interaction=interaction, open_pr=open_pr, set_pr=set_pr, update_page=update_page, message=message)

    async def test_success_stores_the_pr_and_updates_card_notion_and_member(self):
        done = await self.ship()
        kwargs = done.open_pr.await_args.kwargs
        self.assertEqual((kwargs["perk"], kwargs["layer1"], kwargs["base"], kwargs["patron"]), ("clothing", PNG, "main", "Goku"))
        self.assertFalse(kwargs["test"])
        done.set_pr.assert_awaited_once_with(7, "https://github.com/o/r/pull/9")
        self.assertIn("[Mod pull request](https://github.com/o/r/pull/9)", text(done.message.edit.await_args.kwargs["view"]))
        done.update_page.assert_awaited_once_with("ntok", "abc-def", status="In review", github="https://github.com/o/r/pull/9")
        self.assertIn("Seu benefício está pronto", done.interaction.channel.send.await_args.args[0])
        self.assertIn("Pull request opened", done.interaction.followup.send.await_args.args[0])

    async def test_input_problems_come_back_and_nothing_is_stored(self):
        done = await self.ship(outcome=ShipError("Both textures must be PNG files."))
        self.assertEqual(done.interaction.followup.send.await_args.args[0], "Both textures must be PNG files.")
        done.set_pr.assert_not_awaited()
        done.interaction.channel.send.assert_not_awaited()

    async def test_github_failures_are_explained(self):
        done = await self.ship(outcome=RuntimeError("403"))
        self.assertIn("GitHub refused", done.interaction.followup.send.await_args.args[0])
        done.set_pr.assert_not_awaited()

    async def test_clothing_needs_both_layers(self):
        done = await self.ship(files=1)
        done.open_pr.assert_not_awaited()

    async def test_a_claim_that_changed_meanwhile_is_reported_not_overwritten(self):
        self.stored = False
        done = await self.ship()
        self.assertIn("changed meanwhile", done.interaction.followup.send.await_args.args[0])
        done.message.edit.assert_not_awaited()
        done.interaction.channel.send.assert_not_awaited()

    async def test_only_the_perk_team_ships(self):
        done = await self.ship(user=42)
        done.open_pr.assert_not_awaited()
