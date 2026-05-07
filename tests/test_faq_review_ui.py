import os
import unittest


os.environ.setdefault("DISCORD_TOKEN", "dummy-discord-token")
os.environ.setdefault("OPENAI_KEY", "dummy-openai-key")
os.environ.setdefault("GH_APP_PRIVATE_KEY_PEM", "dummy-github-key")

from bulmaai.services.faq_knowledge import FAQReviewCandidate
from bulmaai.ui.faq_review_views import (
    FAQReviewView,
    build_faq_review_embed,
    parse_faq_review_custom_id,
)


class FAQReviewUITests(unittest.IsolatedAsyncioTestCase):
    def _candidate(self, status: str = "pending") -> FAQReviewCandidate:
        return FAQReviewCandidate(
            id=42,
            status=status,
            lang="en",
            canonical_question="How do I transform?",
            answer="Use the configured form key.",
            tags=["forms", "controls"],
            source_ticket_channel_id=123,
            source_question_message_ids=[456],
            source_answer_message_ids=[789],
            proposed_by=99,
            reviewed_by=None,
            review_reason=None,
            approved_faq_id=None,
            review_channel_id=None,
            review_message_id=None,
            created_at=None,
            updated_at=None,
        )

    async def test_review_view_has_persistent_approve_reject_modify_buttons(self) -> None:
        view = FAQReviewView(candidate_id=42)
        custom_ids = [item.custom_id for item in view.children]

        self.assertIn("faq_review:approve:42", custom_ids)
        self.assertIn("faq_review:reject:42", custom_ids)
        self.assertIn("faq_review:modify:42", custom_ids)
        self.assertIsNone(view.timeout)

    async def test_review_view_disables_buttons_for_completed_candidate(self) -> None:
        view = FAQReviewView(candidate_id=42, status="approved")

        self.assertTrue(all(item.disabled for item in view.children))

    def test_parse_faq_review_custom_id(self) -> None:
        action, candidate_id = parse_faq_review_custom_id("faq_review:approve:42")

        self.assertEqual(action, "approve")
        self.assertEqual(candidate_id, 42)

        with self.assertRaises(ValueError):
            parse_faq_review_custom_id("faq_review:approve:not-an-id")

    def test_build_faq_review_embed_includes_candidate_details(self) -> None:
        embed = build_faq_review_embed(self._candidate())

        self.assertEqual(embed.title, "FAQ Review Candidate #42")
        self.assertIn("How do I transform?", embed.description)
        field_names = [field.name for field in embed.fields]
        self.assertIn("Answer", field_names)
        self.assertIn("Tags", field_names)
        self.assertIn("Source", field_names)


if __name__ == "__main__":
    unittest.main()
