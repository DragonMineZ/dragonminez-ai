import unittest

from bulmaai.services.support_intent import (
    SUPPORT_INTENT_PATREON_WHITELIST,
    SUPPORT_INTENT_SUPPORT_QUESTION,
    SUPPORT_INTENT_UNCLEAR,
    classify_support_intent,
)


class SupportIntentTests(unittest.TestCase):
    def test_classifies_patreon_whitelist_requests(self) -> None:
        self.assertEqual(
            classify_support_intent("Me das acceso patreon para la beta please"),
            SUPPORT_INTENT_PATREON_WHITELIST,
        )
        self.assertEqual(
            classify_support_intent("Can I get Patreon whitelist access? IGN Test_User"),
            SUPPORT_INTENT_PATREON_WHITELIST,
        )

    def test_classifies_dragonminez_support_questions(self) -> None:
        self.assertEqual(
            classify_support_intent("How do I install DragonMineZ on Forge 1.20.1?"),
            SUPPORT_INTENT_SUPPORT_QUESTION,
        )
        self.assertEqual(
            classify_support_intent("No puedo transformarme en super saiyan"),
            SUPPORT_INTENT_SUPPORT_QUESTION,
        )
        self.assertEqual(
            classify_support_intent("It still doesn't work"),
            SUPPORT_INTENT_SUPPORT_QUESTION,
        )
        self.assertEqual(
            classify_support_intent("It still does not work"),
            SUPPORT_INTENT_SUPPORT_QUESTION,
        )

    def test_classifies_images_as_support_questions(self) -> None:
        self.assertEqual(
            classify_support_intent("", has_image=True),
            SUPPORT_INTENT_SUPPORT_QUESTION,
        )

    def test_classifies_unrelated_mentions_as_unclear(self) -> None:
        self.assertEqual(classify_support_intent("20 + 20 + 20 + 7"), SUPPORT_INTENT_UNCLEAR)
        self.assertEqual(classify_support_intent("hola que tal"), SUPPORT_INTENT_UNCLEAR)
        self.assertEqual(classify_support_intent("tell me a joke"), SUPPORT_INTENT_UNCLEAR)


if __name__ == "__main__":
    unittest.main()
