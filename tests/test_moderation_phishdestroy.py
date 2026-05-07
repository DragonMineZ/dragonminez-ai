import unittest

from bulmaai.cogs.moderation import ModerationCog
from bulmaai.services.moderation import MessageSignal, ModerationAction
from bulmaai.services.phishdestroy import PhishDestroyUnavailable, PhishDestroyVerdict


class DummyBot:
    def __init__(self, settings):
        self.settings = settings

    async def wait_until_ready(self) -> None:
        return None


class FakePhishDestroy:
    def __init__(self, result):
        self.result = result
        self.checked: list[str] = []

    async def check_domain(self, domain: str):
        self.checked.append(domain)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def healthcheck(self) -> None:
        if isinstance(self.result, Exception):
            raise self.result
        return None


class ModerationPhishDestroyTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self, **overrides):
        values = {
            "phishdestroy_enabled": False,
            "phishdestroy_action": "alert",
            "phishdestroy_api_base_url": "https://api.destroy.tools",
            "phishdestroy_timeout_seconds": 5,
            "phishdestroy_safe_ttl_seconds": 60,
            "phishdestroy_threat_ttl_seconds": 60,
            "phishdestroy_recovery_interval_seconds": 300,
            "moderation_allowed_domains": (),
        }
        values.update(overrides)
        return type("Settings", (), values)()

    def _cog(self, **settings):
        cog = ModerationCog(DummyBot(self._settings(**settings)))
        self.addCleanup(cog.cog_unload)
        return cog

    async def test_threat_verdict_returns_moderation_decision(self) -> None:
        cog = self._cog()
        cog._phishdestroy = FakePhishDestroy(
            PhishDestroyVerdict(
                domain="bad.example",
                threat=True,
                risk_score=85,
                severity="critical",
            )
        )

        decision = await cog._evaluate_phishdestroy(
            MessageSignal(guild_id=1, channel_id=2, author_id=3, content="https://bad.example/login")
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, ModerationAction.ALERT)
        self.assertEqual(decision.reason, "phishdestroy_domain")
        self.assertEqual(decision.source, "phishdestroy")
        self.assertEqual(decision.domains, ("bad.example",))

    async def test_allowed_domain_is_not_sent_to_phishdestroy(self) -> None:
        cog = self._cog(moderation_allowed_domains=("trusted.example",))
        client = FakePhishDestroy(
            PhishDestroyVerdict(domain="trusted.example", threat=True, risk_score=90)
        )
        cog._phishdestroy = client

        decision = await cog._evaluate_phishdestroy(
            MessageSignal(guild_id=1, channel_id=2, author_id=3, content="https://trusted.example")
        )

        self.assertIsNone(decision)
        self.assertEqual(client.checked, [])

    async def test_api_unavailable_pauses_future_phishdestroy_checks_and_logs(self) -> None:
        cog = self._cog(phishdestroy_recovery_interval_seconds=60)
        cog._phishdestroy = FakePhishDestroy(PhishDestroyUnavailable("HTTP 500"))

        with self.assertLogs("bulmaai.cogs.moderation", level="WARNING") as logs:
            decision = await cog._evaluate_phishdestroy(
                MessageSignal(guild_id=1, channel_id=2, author_id=3, content="https://bad.example")
            )

        self.assertIsNone(decision)
        self.assertTrue(cog._phishdestroy_down)
        self.assertEqual(logs.records[0].event, "phishdestroy_api_down")
        self.assertTrue(logs.records[0].discord_forward)


if __name__ == "__main__":
    unittest.main()
