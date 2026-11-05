import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-anthropic-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

import discord

from bulmaai.cogs import mail_inbox as cog_module
from bulmaai.cogs.mail_inbox import REPLIED_MSG, MailInboxCog
from bulmaai.services import mail_inbox as store
from bulmaai.ui import mail_cards
from tests.v2_helpers import buttons, text

NOW = datetime.now(timezone.utc)
USER = "support@dragonminez.com"


def mail(**overrides):
    values = dict(
        id=5,
        message_id="<abc@example.com>",
        from_addr="goku@example.com",
        from_name="Son Goku",
        subject="Server help",
        body="Hello there",
        attachments=[],
        received_at=NOW - timedelta(minutes=3),
        created_at=NOW,
    )
    values.update(overrides)
    return store.Mail(**values)


def raw(message: EmailMessage) -> bytes:
    return bytes(message)


def plain(**headers) -> EmailMessage:
    message = EmailMessage()
    message["From"] = headers.get("From", "Son Goku <goku@example.com>")
    message["To"] = USER
    message["Subject"] = headers.get("Subject", "Server help")
    message["Date"] = "Wed, 04 Nov 2026 10:00:00 +0000"
    if headers.get("Message-ID", "<abc@example.com>"):
        message["Message-ID"] = headers.get("Message-ID", "<abc@example.com>")
    message.set_content(headers.get("body", "Hi,\n\n\n\n\nmy server crashes.\n"))
    return message


class ParseTests(unittest.TestCase):
    def test_plain_mail(self):
        parsed = store.parse_message(raw(plain()))
        self.assertEqual(parsed.message_id, "<abc@example.com>")
        self.assertEqual((parsed.from_name, parsed.from_addr), ("Son Goku", "goku@example.com"))
        self.assertEqual(parsed.subject, "Server help")
        self.assertEqual(parsed.body, "Hi,\n\nmy server crashes.")
        self.assertEqual(parsed.received_at, datetime(2026, 11, 4, 10, tzinfo=timezone.utc))
        self.assertEqual(parsed.attachments, [])

    def test_html_only_mail(self):
        message = plain()
        message.set_content(
            "<html><head><style>p{}</style></head><body><p>Hello &amp; welcome</p><br><div>Line two</div>"
            "<script>alert(1)</script></body></html>",
            subtype="html",
        )
        body = store.parse_message(raw(message)).body
        self.assertIn("Hello & welcome", body)
        self.assertIn("Line two", body)
        self.assertNotIn("<", body)
        self.assertNotIn("alert", body)
        self.assertNotIn("p{}", body)

    def test_multipart_with_attachment(self):
        message = plain(body="See the log")
        message.add_alternative("<p>See the <b>log</b></p>", subtype="html")
        message.add_attachment(b"crash", maintype="text", subtype="plain", filename="latest.log")
        message.add_attachment(b"\x89PNG", maintype="image", subtype="png", filename="shot.png")
        parsed = store.parse_message(raw(message))
        self.assertEqual(parsed.body, "See the log")
        self.assertEqual(parsed.attachments, ["latest.log", "shot.png"])

    def test_missing_message_id_gets_a_stable_key(self):
        first = store.parse_message(raw(plain(**{"Message-ID": ""})))
        second = store.parse_message(raw(plain(**{"Message-ID": ""})))
        other = store.parse_message(raw(plain(**{"Message-ID": "", "Subject": "Other"})))
        self.assertTrue(first.message_id.startswith("sha256:"))
        self.assertEqual(first.message_id, second.message_id)
        self.assertNotEqual(first.message_id, other.message_id)

    def test_encoded_subject_and_name(self):
        data = (
            b"From: =?utf-8?b?Sm9zw6k=?= <jose@example.com>\r\n"
            b"Subject: =?utf-8?q?Ayuda_con_el_servidor_=C2=A1ya!?=\r\n"
            b"Message-ID: <x1@example.com>\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\nHola\r\n"
        )
        parsed = store.parse_message(data)
        self.assertEqual(parsed.subject, "Ayuda con el servidor ¡ya!")
        self.assertEqual(parsed.from_name, "José")
        self.assertIsNone(parsed.received_at)

    def test_malformed_mail_is_skipped(self):
        self.assertIsNone(store.parse_message(b"Subject: no sender\r\n\r\nbody"))
        self.assertIsNone(store.parse_message(b"\x00\xff garbage"))

    def test_subject_has_no_line_breaks(self):
        parsed = store.parse_message(b"From: a@b.c\r\nSubject: one\r\n two\r\n\r\nbody")
        self.assertEqual(parsed.subject, "one two")

    def test_fetch_is_read_only_and_skips_known_and_own(self):
        heads = {
            b"1": b"Message-ID: <old@x>\r\nFrom: a@b.c\r\n\r\n",
            b"2": f"Message-ID: <own@x>\r\nFrom: Support <{USER.upper()}>\r\n\r\n".encode(),
            b"3": b"Message-ID: <new@x>\r\nFrom: a@b.c\r\n\r\n",
        }
        imap = MagicMock()
        imap.search.return_value = ("OK", [b"1 2 3"])
        imap.fetch.side_effect = lambda num, what: (
            "OK",
            [(b"head", heads[num]) if "HEADER.FIELDS" in what else (b"body", b"full " + num), b")"],
        )
        with patch.object(store.imaplib, "IMAP4_SSL", return_value=imap) as connect:
            out = store.fetch_new("imap.example", USER, "pw", {"<old@x>"})
        self.assertEqual(out, [b"full 3"])
        connect.assert_called_once_with("imap.example", 993, timeout=30)
        imap.select.assert_called_once_with("INBOX", readonly=True)
        self.assertTrue(all("PEEK" in call.args[1] for call in imap.fetch.call_args_list))
        imap.logout.assert_called_once()

    def test_imap_date_is_locale_free(self):
        self.assertEqual(store.imap_date(datetime(2026, 1, 1).date()), "01-Jan-2026")


class CardTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_card_has_reply_and_defangs(self):
        view = mail_cards.mail_card(
            mail(body="@everyone visit https://evil.example now", attachments=["a.pdf", "<@&1>.png"])
        )
        content = text(view)
        self.assertNotIn("@everyone", content)
        self.assertNotIn("https://evil", content)
        self.assertNotIn("<@&1>", content)
        self.assertIn("📧 Server help", content)
        self.assertIn("Son Goku <goku@example.com>", content)
        self.assertIn("-# 📎 a.pdf", content)
        self.assertEqual([b.custom_id for b in buttons(view)], ["mail:reply:5"])
        self.assertEqual(buttons(view)[0].style, discord.ButtonStyle.primary)

    async def test_no_subject(self):
        self.assertIn("(no subject)", text(mail_cards.mail_card(mail(subject=""))))

    async def test_replied_card(self):
        view = mail_cards.mail_card(mail(replied_by=111, replied_at=NOW, reply_text="Try again https://x.example"))
        content = text(view)
        self.assertEqual(buttons(view), [])
        self.assertIn("Replied by <@111>", content)
        self.assertIn("> Try again", content)
        self.assertNotIn("https://x", content)

    async def test_huge_body_fits(self):
        view = mail_cards.mail_card(
            mail(subject="s" * 500, body="word " * 5000, reply_text="r" * 4000, replied_by=1, replied_at=NOW)
        )
        self.assertLess(len(text(view)), 4000)

    async def test_modal(self):
        modal = mail_cards.reply_modal(mail(from_addr="a-very-long-address-for-testing@example-domain.com"), USER)
        self.assertEqual(modal.custom_id, "mail_form:reply:5")
        self.assertLessEqual(len(modal.title), 45)
        self.assertTrue(modal.title.startswith("Reply to "))

    def test_ids_round_trip(self):
        self.assertEqual(mail_cards.parse_custom_id(mail_cards.custom_id("reply", 9)), ("reply", 9))
        self.assertEqual(mail_cards.parse_form_id(mail_cards.form_id("reply", 9)), ("reply", 9))
        for junk in (None, "", "mail:reply", "mail:send:9", "mail:reply:x", "mail_form:reply:9", "banappeal:reply:9"):
            self.assertIsNone(mail_cards.parse_custom_id(junk), junk)
        self.assertIsNone(mail_cards.parse_form_id("mail:reply:9"))

    def test_read_text(self):
        components = [{"type": 18, "component": {"type": 4, "custom_id": "text", "value": "  Hi there \n"}}]
        self.assertEqual(mail_cards.read_text(components), "Hi there")
        self.assertEqual(mail_cards.read_text(None), "")


class ReplyTests(unittest.TestCase):
    def test_headers(self):
        message = store.build_reply(mail(), USER, "Thanks, fixed.")
        self.assertEqual(message["From"], USER)
        self.assertEqual(message["To"], "goku@example.com")
        self.assertEqual(message["Subject"], "Re: Server help")
        self.assertEqual(message["In-Reply-To"], "<abc@example.com>")
        self.assertEqual(message["References"], "<abc@example.com>")
        self.assertIn("Thanks, fixed.", message.get_content())

    def test_re_prefix_is_not_doubled(self):
        self.assertEqual(store.build_reply(mail(subject="RE: hi"), USER, "x")["Subject"], "RE: hi")
        self.assertEqual(store.build_reply(mail(subject="re:hi"), USER, "x")["Subject"], "re:hi")

    def test_no_threading_headers_for_a_stand_in_id(self):
        message = store.build_reply(mail(message_id="sha256:abc"), USER, "x")
        self.assertIsNone(message["In-Reply-To"])
        self.assertIsNone(message["References"])

    def test_line_breaks_in_subject_are_dropped(self):
        message = store.build_reply(mail(subject="hi\r\nBcc: evil@example.com"), USER, "x")
        self.assertEqual(message["Subject"], "Re: hi Bcc: evil@example.com")
        self.assertIsNone(message["Bcc"])


def make_cog():
    settings = SimpleNamespace(
        mail_channel_id=900, mail_user=USER, mail_password="hunter2",
        mail_imap_host="imap.example", mail_smtp_host="smtp.example",
    )
    return MailInboxCog(SimpleNamespace(settings=settings))


def submit(mail_id=5, value="Thanks!"):
    inter = MagicMock()
    inter.user = SimpleNamespace(id=111)
    inter.data = {"components": [{"type": 18, "component": {"custom_id": "text", "value": value}}]}
    inter.response.send_message = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    inter.message.edit = AsyncMock()
    return inter


class CogTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_sends_and_updates_card(self):
        cog = make_cog()
        claimed = mail(replied_by=111, replied_at=NOW, reply_text="Thanks!")
        send = MagicMock()
        with (
            patch.object(store, "claim", AsyncMock(return_value=claimed)) as claim,
            patch.object(store, "send_reply", send),
        ):
            inter = submit()
            await cog._submit(inter, 5)
        claim.assert_awaited_once_with(5, 111, "Thanks!")
        host, user, password, message = send.call_args.args
        self.assertEqual((host, user, password), ("smtp.example", USER, "hunter2"))
        self.assertEqual(message["To"], "goku@example.com")
        inter.message.edit.assert_awaited_once()
        inter.followup.send.assert_awaited_once_with("Sent.", ephemeral=True)

    async def test_failed_send_releases_the_claim_without_the_password(self):
        cog = make_cog()
        claimed = mail(replied_by=111, replied_at=NOW, reply_text="Thanks!")
        with (
            patch.object(store, "claim", AsyncMock(return_value=claimed)),
            patch.object(store, "release", AsyncMock()) as release,
            patch.object(store, "send_reply", MagicMock(side_effect=OSError("bad login hunter2"))),
            patch.object(cog_module.log, "warning") as warning,
        ):
            inter = submit()
            await cog._submit(inter, 5)
        release.assert_awaited_once_with(5, 111)
        sent = inter.followup.send.await_args.args[0]
        self.assertTrue(sent.startswith("Couldn't send the reply: "))
        self.assertNotIn("hunter2", sent)
        self.assertNotIn("hunter2", str(warning.call_args))
        inter.message.edit.assert_not_awaited()

    async def test_second_reply_loses(self):
        cog = make_cog()
        replied = mail(replied_by=222, replied_at=NOW, reply_text="first")
        with (
            patch.object(store, "claim", AsyncMock(return_value=None)),
            patch.object(store, "get", AsyncMock(return_value=replied)),
            patch.object(store, "send_reply", MagicMock()) as send,
        ):
            inter = submit()
            await cog._submit(inter, 5)
        send.assert_not_called()
        inter.response.edit_message.assert_awaited_once()
        inter.followup.send.assert_awaited_once_with(REPLIED_MSG, ephemeral=True)

    async def test_non_admin_is_refused(self):
        cog = make_cog()
        inter = MagicMock()
        inter.type = discord.InteractionType.component
        inter.data = {"custom_id": "mail:reply:5"}
        inter.user = SimpleNamespace(id=1, guild_permissions=SimpleNamespace(administrator=False))
        inter.response.send_message = AsyncMock()
        with patch.object(store, "get", AsyncMock()) as get:
            await cog.on_interaction(inter)
        get.assert_not_awaited()
        inter.response.send_message.assert_awaited_once_with("Only admins can answer mail.", ephemeral=True)

    async def test_poll_posts_new_mail_and_skips_own(self):
        cog = make_cog()
        mine = raw(plain(From=USER, **{"Message-ID": "<mine@x>"}))
        theirs = raw(plain())
        channel = SimpleNamespace(id=900, send=AsyncMock(return_value=SimpleNamespace(id=77)))
        cog.bot.get_channel = lambda _id: channel
        inserted = mail()
        with (
            patch.object(store, "known_ids", AsyncMock(return_value=set())),
            patch.object(store, "fetch_new", MagicMock(return_value=[mine, theirs])),
            patch.object(store, "insert", AsyncMock(side_effect=[inserted])) as insert,
            patch.object(store, "set_card", AsyncMock()) as set_card,
        ):
            await cog._poll()
        insert.assert_awaited_once()
        self.assertEqual(insert.await_args.args[0].from_addr, "goku@example.com")
        self.assertIs(channel.send.await_args.kwargs["allowed_mentions"].everyone, False)
        set_card.assert_awaited_once_with(5, 900, 77)

    async def test_failed_post_deletes_the_row_and_warns_once(self):
        cog = make_cog()
        channel = SimpleNamespace(id=900, send=AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403), "no")))
        cog.bot.get_channel = lambda _id: channel
        with (
            patch.object(store, "delete", AsyncMock()) as delete,
            patch.object(cog_module.log, "log") as log_call,
        ):
            await cog._post(mail())
            await cog._post(mail())
        self.assertEqual(delete.await_count, 2)
        self.assertEqual([c.args[0] for c in log_call.call_args_list], [logging.WARNING, logging.INFO])

    async def test_read_failure_warns_once(self):
        cog = make_cog()
        with (
            patch.object(store, "known_ids", AsyncMock(return_value=set())),
            patch.object(store, "fetch_new", MagicMock(side_effect=OSError("timed out"))),
            patch.object(cog_module.log, "log") as log_call,
        ):
            await cog._poll()
            await cog._poll()
        self.assertEqual([c.args[0] for c in log_call.call_args_list], [logging.WARNING, logging.INFO])

    async def test_off_without_password(self):
        cog = make_cog()
        cog.bot.settings.mail_password = None
        with patch.object(store, "known_ids", AsyncMock()) as known:
            await cog._poll()
        known.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
