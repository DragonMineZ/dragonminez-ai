import io
import unittest
from datetime import date, timedelta
from types import SimpleNamespace

from PIL import Image, ImageColor

from bulmaai.ui import ai_daily_chart as chart


def _point(day, answers=None, cost=None, opened=0, closed=0):
    return SimpleNamespace(
        day=day,
        answers={"answer": 0, "clarify": 0, "handoff": 0, "other": 0} | (answers or {}),
        cost={"support": 0.0, "tickets": 0.0, "staff": 0.0, "other": 0.0} | (cost or {}),
        tickets_opened=opened,
        tickets_closed=closed,
    )


def _zero_points(count):
    start = date(2026, 11, 13) - timedelta(days=count - 1)
    return [_point(start + timedelta(days=index)) for index in range(count)]


def _open(data):
    return Image.open(io.BytesIO(data))


def _close_to(pixel, color, tolerance=12):
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], ImageColor.getrgb(color)))


class RenderChartTests(unittest.TestCase):
    def test_returns_png_of_final_size(self):
        points = _zero_points(14)
        for index, point in enumerate(points):
            point.answers.update(answer=50 + index, clarify=12, handoff=6, other=3)
            point.cost.update(support=0.9, tickets=0.3, staff=0.1, other=0.05)
            point.tickets_opened = 20 + index
            point.tickets_closed = 30 - index
        data = chart.render_chart(points)
        self.assertIsInstance(data, bytes)
        image = _open(data)
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.size, (chart.WIDTH, chart.HEIGHT))

    def test_all_zero_renders(self):
        self.assertEqual(_open(chart.render_chart(_zero_points(14))).size, (chart.WIDTH, chart.HEIGHT))

    def test_single_point_renders(self):
        points = _zero_points(1)
        points[0].answers["answer"] = 3
        points[0].tickets_opened = 2
        self.assertEqual(_open(chart.render_chart(points)).size, (chart.WIDTH, chart.HEIGHT))

    def test_31_points_render(self):
        points = _zero_points(31)
        for index, point in enumerate(points):
            point.answers["clarify"] = index
            point.cost["staff"] = index / 100
            point.tickets_closed = index % 7
        self.assertEqual(_open(chart.render_chart(points)).size, (chart.WIDTH, chart.HEIGHT))

    def test_only_last_day_has_a_column(self):
        points = _zero_points(14)
        points[-1].answers["answer"] = 10
        image = _open(chart.render_chart(points)).convert("RGB")
        y = chart.ANSWERS_TOP + chart.ANSWERS_HEIGHT - 5
        last = image.getpixel((round(chart.slot_center(13, 14)), y))
        earlier = image.getpixel((round(chart.slot_center(5, 14)), y))
        self.assertTrue(_close_to(last, chart.SERIES_1), last)
        self.assertTrue(_close_to(earlier, chart.SURFACE, tolerance=2), earlier)


class NiceAxisTests(unittest.TestCase):
    def _tick_count(self, top, step):
        return round(top / step) + 1

    def test_zero_uses_defaults(self):
        self.assertEqual(chart.nice_axis(0)[0], chart.COUNT_DEFAULT_MAX)
        self.assertEqual(chart.nice_axis(0, cost=True)[0], chart.COST_DEFAULT_MAX)

    def test_small_count(self):
        top, step = chart.nice_axis(7)
        self.assertGreaterEqual(top, 7)
        self.assertIn(self._tick_count(top, step), (3, 4))
        self.assertIsInstance(step, int)

    def test_cost_below_one(self):
        top, step = chart.nice_axis(0.37, cost=True)
        self.assertLess(step, 1)
        self.assertGreaterEqual(top, 0.37)
        self.assertIn(self._tick_count(top, step), (3, 4))

    def test_large_count(self):
        top, step = chart.nice_axis(1234)
        self.assertGreaterEqual(top, 1234)
        self.assertLess(top, 2500)
        self.assertIn(self._tick_count(top, step), (3, 4))

    def test_counts_never_get_fractional_steps(self):
        for maximum in (1, 2, 3):
            top, step = chart.nice_axis(maximum)
            self.assertGreaterEqual(step, 1)
            self.assertGreaterEqual(top, maximum)
            self.assertIn(self._tick_count(top, step), (3, 4))


if __name__ == "__main__":
    unittest.main()
