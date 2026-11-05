import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from bulmaai.ui.ticket_views import ticket_log_card
from tests.v2_helpers import buttons, texts


def _ticket():
    return SimpleNamespace(
        ticket_id=7, owner_id=111, category="bug", language="es", created_at=datetime(2026, 11, 5, tzinfo=timezone.utc)
    )


class TicketLogCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_summary_and_transcript(self):
        summary = SimpleNamespace(title="Crash on load", problem="Game crashed", resolution="Updated Forge")
        view = ticket_log_card(
            _ticket(), closed_by_id=222, resolved=True, message_count=12, summary=summary, url="https://t.example/t/x"
        )
        text = "\n".join(texts(view))
        self.assertIn("#0007", text)
        self.assertIn("<@111>", text)
        self.assertIn("<@222>", text)
        self.assertIn("Updated Forge", text)
        self.assertEqual([b.url for b in buttons(view)], ["https://t.example/t/x"])

    async def test_without_summary_or_page(self):
        view = ticket_log_card(_ticket(), closed_by_id=None, resolved=False, message_count=0, summary=None, url=None)
        self.assertIn("automatic", "\n".join(texts(view)))
        self.assertEqual(buttons(view), [])
