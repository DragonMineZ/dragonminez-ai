import time
import unittest

from bulmaai.utils.dmzdebug_parser import _RE_MEMBER, _parse_pending_invite
from bulmaai.utils.log_parser import parse_log


class ParserBacktrackingTests(unittest.TestCase):
    """Uploaded logs are member input parsed on the bot's event loop; a quadratic regex froze the whole bot."""

    def test_hostile_logs_parse_in_linear_time(self) -> None:
        spaces = " " * 200_000
        started = time.perf_counter()
        parse_log("minecraft\n" + ("x\n" + spaces + "\n") * 3)
        parse_log(("[12:00:00] [main/INFO]: minecraft loading assets\n") * 40_000)
        _RE_MEMBER.match("0a (x)" + spaces + "z")
        _parse_pending_invite("from x" + spaces + "y")
        self.assertLess(time.perf_counter() - started, 3.0)

    def test_mod_table_and_pending_invite_still_parse(self) -> None:
        report = parse_log("minecraft\n| xenon.jar | Xenon | xenon | 0.3.31 | DONE |\n")
        self.assertIsNotNone(report)
        self.assertEqual(
            _parse_pending_invite("from Goku (bar) (0a1b-2c)"), {"name": "Goku (bar)", "uuid": "0a1b-2c", "expired": False}
        )
