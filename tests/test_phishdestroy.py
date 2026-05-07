import unittest
from unittest.mock import patch

from bulmaai.services.phishdestroy import (
    PhishDestroyClient,
    PhishDestroyUnavailable,
    normalize_domain,
)


class FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self) -> dict:
        return dict(self._payload)


class PhishDestroyClientTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_domain_strips_url_userinfo_port_and_subdomain_case(self) -> None:
        self.assertEqual(
            normalize_domain("HTTPS://user:pass@Sub.Example.COM:443/path"),
            "sub.example.com",
        )

    async def test_check_domain_returns_threat_verdict(self) -> None:
        async def fake_request(method: str, url: str, **kwargs):
            return FakeResponse(
                payload={
                    "domain": "bad.example",
                    "threat": True,
                    "risk_score": 85,
                    "severity": "critical",
                    "active": True,
                    "flags": ["curated_blocklist"],
                }
            )

        client = PhishDestroyClient(timeout_seconds=2)

        with patch("bulmaai.services.phishdestroy.http.request", side_effect=fake_request):
            verdict = await client.check_domain("bad.example")

        self.assertTrue(verdict.threat)
        self.assertEqual(verdict.domain, "bad.example")
        self.assertEqual(verdict.risk_score, 85)
        self.assertEqual(verdict.severity, "critical")
        self.assertEqual(verdict.flags, ("curated_blocklist",))

    async def test_check_domain_uses_memory_cache(self) -> None:
        calls = 0

        async def fake_request(method: str, url: str, **kwargs):
            nonlocal calls
            calls += 1
            return FakeResponse(payload={"domain": "safe.example", "threat": False})

        client = PhishDestroyClient(safe_ttl_seconds=60)

        with patch("bulmaai.services.phishdestroy.http.request", side_effect=fake_request):
            first = await client.check_domain("safe.example")
            second = await client.check_domain("safe.example")

        self.assertFalse(first.threat)
        self.assertIs(first, second)
        self.assertEqual(calls, 1)

    async def test_server_error_marks_api_unavailable(self) -> None:
        async def fake_request(method: str, url: str, **kwargs):
            return FakeResponse(status_code=500, payload={"error": "down"})

        client = PhishDestroyClient()

        with patch("bulmaai.services.phishdestroy.http.request", side_effect=fake_request):
            with self.assertRaises(PhishDestroyUnavailable):
                await client.check_domain("bad.example")

    async def test_healthcheck_raises_when_api_is_unavailable(self) -> None:
        async def fake_request(method: str, url: str, **kwargs):
            return FakeResponse(status_code=503)

        client = PhishDestroyClient()

        with patch("bulmaai.services.phishdestroy.http.request", side_effect=fake_request):
            with self.assertRaises(PhishDestroyUnavailable):
                await client.healthcheck()


if __name__ == "__main__":
    unittest.main()
