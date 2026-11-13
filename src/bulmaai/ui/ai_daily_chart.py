import io
import math
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1280
HEIGHT = 1040
SCALE = 2

SURFACE = "#1a1a19"
GRIDLINE = "#2c2c2a"
BASELINE = "#383835"
TEXT_PRIMARY = "#f0efec"
TEXT_SECONDARY = "#c3c2b7"
TEXT_MUTED = "#898781"
SERIES_1 = "#3987e5"
SERIES_2 = "#d95926"
SERIES_3 = "#199e70"
SERIES_OTHER = "#6b6a65"

PLOT_LEFT = 88
PLOT_RIGHT = 1164
CAPTION_POS = (28, 20)
TEXT_SIZE = 22
TITLE_SIZE = 26
VALUE_SIZE = 24
TITLE_X = 28
TITLE_OFFSET = 56
LEGEND_X = 420
LEGEND_SWATCH = 18
LEGEND_SWATCH_RADIUS = 4
LEGEND_SWATCH_GAP = 8
LEGEND_ITEM_GAP = 28
TICK_LABEL_RIGHT = 76
DAY_LABEL_OFFSET = 12
COLUMN_WIDTH = 36
COLUMN_MIN_SLOT = 60
COLUMN_SLOT_SHARE = 0.6
SEGMENT_GAP = 4
SEGMENT_MIN_HEIGHT = 2
COLUMN_RADIUS = 6
VALUE_GAP = 12
LINE_WIDTH = 4
DOT_RADIUS = 10
DOT_RING = 4
END_LABEL_SPACING = 28

ANSWERS_TITLE = "AI answers per day"
ANSWERS_TOP = 120
ANSWERS_HEIGHT = 200
ANSWERS_SERIES = (
    ("answer", "answer", SERIES_1),
    ("clarify", "clarify", SERIES_2),
    ("handoff", "handoff", SERIES_3),
    ("other", "other", SERIES_OTHER),
)

COST_TITLE = "Cost per day"
COST_TOP = 456
COST_HEIGHT = 200
COST_SERIES = (
    ("support", "support", SERIES_1),
    ("tickets", "ticket tools", SERIES_2),
    ("staff", "Staff Bulma", SERIES_3),
    ("other", "other", SERIES_OTHER),
)

TICKETS_TITLE = "Tickets opened and closed"
TICKETS_TOP = 792
TICKETS_HEIGHT = 180
TICKETS_SERIES = (
    ("tickets_opened", "opened", SERIES_1),
    ("tickets_closed", "closed", SERIES_2),
)

COUNT_DEFAULT_MAX = 4
COST_DEFAULT_MAX = 1.0

FONT_FAMILIES = (
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ("arial.ttf", "arialbd.ttf"),
    ("Arial.ttf", "Arial Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
)


@lru_cache(maxsize=32)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for regular, heavy in FONT_FAMILIES:
        try:
            pair = (ImageFont.truetype(regular, size), ImageFont.truetype(heavy, size))
            return pair[bold]
        except OSError:
            continue
    return ImageFont.load_default(size)


def _px(value: float) -> int:
    return round(value * SCALE)


def slot_width(count: int) -> float:
    return (PLOT_RIGHT - PLOT_LEFT) / count


def slot_center(index: int, count: int) -> float:
    return PLOT_LEFT + slot_width(count) * (index + 0.5)


def column_width(count: int) -> float:
    slot = slot_width(count)
    return COLUMN_WIDTH if slot >= COLUMN_MIN_SLOT else slot * COLUMN_SLOT_SHARE


def nice_axis(maximum: float, cost: bool = False) -> tuple[float, float]:
    """The axis maximum and tick step: 3 or 4 ticks counting 0, top >= maximum."""
    if maximum <= 0:
        maximum = COST_DEFAULT_MAX if cost else COUNT_DEFAULT_MAX
    exponent = -4 if cost else 0
    while True:
        for mantissa in (1, 2, 5):
            step = mantissa * 10**exponent
            intervals = math.ceil(maximum / step - 1e-9)
            if intervals <= 3:
                top = step * max(intervals, 2)
                if exponent < 0:
                    return round(top, 10), round(step, 10)
                return top, step
        exponent += 1


def _ticks(top: float, step: float) -> list[float]:
    return [round(step * index, 10) for index in range(round(top / step) + 1)]


def _cost_decimals(step: float) -> int:
    if step >= 1:
        return 0
    return max(2, -math.floor(math.log10(step) + 1e-9))


def _text(draw, xy, text, *, size=TEXT_SIZE, bold=False, fill=TEXT_MUTED, anchor="la") -> None:
    draw.text((_px(xy[0]), _px(xy[1])), text, font=_font(size * SCALE, bold), fill=fill, anchor=anchor)


def _text_width(draw, text, *, size=TEXT_SIZE, bold=False) -> float:
    return draw.textlength(text, font=_font(size * SCALE, bold)) / SCALE


def _header(draw, title: str, top: int, series) -> None:
    center = top - TITLE_OFFSET + TITLE_SIZE / 2 + 2
    _text(draw, (TITLE_X, center), title, size=TITLE_SIZE, bold=True, fill=TEXT_PRIMARY, anchor="lm")
    title_right = TITLE_X + _text_width(draw, title, size=TITLE_SIZE, bold=True)
    x = max(LEGEND_X, title_right + LEGEND_ITEM_GAP)
    for _key, label, color in series:
        draw.rounded_rectangle(
            (
                _px(x),
                _px(center - LEGEND_SWATCH / 2),
                _px(x + LEGEND_SWATCH) - 1,
                _px(center + LEGEND_SWATCH / 2) - 1,
            ),
            radius=_px(LEGEND_SWATCH_RADIUS),
            fill=color,
        )
        x += LEGEND_SWATCH + LEGEND_SWATCH_GAP
        _text(draw, (x, center), label, fill=TEXT_SECONDARY, anchor="lm")
        x += _text_width(draw, label) + LEGEND_ITEM_GAP


def _axes(draw, days, top: int, height: int, axis_top: float, step: float, label) -> None:
    bottom = top + height
    for tick in _ticks(axis_top, step):
        y = bottom - height * tick / axis_top
        draw.rectangle(
            (_px(PLOT_LEFT), _px(y), _px(PLOT_RIGHT) - 1, _px(y) + SCALE - 1),
            fill=BASELINE if tick == 0 else GRIDLINE,
        )
        _text(draw, (TICK_LABEL_RIGHT, y), label(tick), anchor="rm")
    count = len(days)
    for index, day in enumerate(days):
        if (count - 1 - index) % 2 == 0:
            _text(draw, (slot_center(index, count), bottom + DAY_LABEL_OFFSET), str(day.day), anchor="ma")


def _column(draw, center: float, width: float, bottom: float, heights, colors) -> None:
    filled = [index for index, value in enumerate(heights) if value > 0]
    base = bottom
    for index, value in enumerate(heights):
        if value <= 0:
            continue
        low, high = base, base - value
        base = high
        topmost = index == filled[-1]
        if not topmost and value - SEGMENT_GAP >= SEGMENT_MIN_HEIGHT:
            high += SEGMENT_GAP
        box = (_px(center - width / 2), _px(high), _px(center + width / 2) - 1, _px(low) - 1)
        if box[3] < box[1]:
            continue
        if topmost:
            radius = min(_px(COLUMN_RADIUS), box[3] - box[1] + 1, (box[2] - box[0] + 1) // 2)
            draw.rounded_rectangle(box, radius=radius, fill=colors[index], corners=(True, True, False, False))
        else:
            draw.rectangle(box, fill=colors[index])


def _stacked_panel(draw, points, title, top, height, series, values_of, *, cost: bool) -> None:
    count = len(points)
    bottom = top + height
    rows = [[max(0, values_of(point).get(key, 0) or 0) for key, _label, _color in series] for point in points]
    axis_top, step = nice_axis(max(sum(row) for row in rows), cost=cost)
    decimals = _cost_decimals(step) if cost else 0
    label = (lambda tick: f"${tick:.{decimals}f}") if cost else (lambda tick: str(int(tick)))
    _header(draw, title, top, series)
    _axes(draw, [point.day for point in points], top, height, axis_top, step, label)
    width = column_width(count)
    colors = [color for _key, _label, color in series]
    for index, row in enumerate(rows):
        _column(draw, slot_center(index, count), width, bottom, [height * value / axis_top for value in row], colors)
    total = sum(rows[-1])
    if not total:
        return  # no column, nothing to label
    column_top = bottom - height * total / axis_top
    y = min(max(column_top, top), bottom - VALUE_SIZE)
    text = f"${total:.2f}" if cost else str(int(total))
    x = slot_center(count - 1, count) + width / 2 + VALUE_GAP
    _text(draw, (x, y), text, size=VALUE_SIZE, bold=True, fill=TEXT_PRIMARY, anchor="la")


def _spread(first: float, second: float, top: float, bottom: float) -> tuple[float, float]:
    """Move two label centers apart to END_LABEL_SPACING, keeping their order, inside the panel."""
    if abs(first - second) < END_LABEL_SPACING:
        middle = (first + second) / 2
        half = END_LABEL_SPACING / 2
        first, second = (middle - half, middle + half) if first <= second else (middle + half, middle - half)
    shift = max(0, top - min(first, second)) - max(0, max(first, second) - bottom)
    return first + shift, second + shift


def _lines_panel(draw, points, title, top, height, series) -> None:
    count = len(points)
    bottom = top + height
    rows = {key: [max(0, getattr(point, key, 0) or 0) for point in points] for key, _label, _color in series}
    axis_top, step = nice_axis(max(max(values) for values in rows.values()))
    _header(draw, title, top, series)
    _axes(draw, [point.day for point in points], top, height, axis_top, step, lambda tick: str(int(tick)))
    ends = {}
    for key, _label, color in reversed(series):
        path = [
            (_px(slot_center(index, count)), _px(bottom - height * value / axis_top))
            for index, value in enumerate(rows[key])
        ]
        if len(path) > 1:
            draw.line(path, fill=color, width=_px(LINE_WIDTH), joint="curve")
            cap = _px(LINE_WIDTH) // 2
            x, y = path[0]
            draw.ellipse((x - cap, y - cap, x + cap, y + cap), fill=color)
        x, y = path[-1]
        ring = _px(DOT_RADIUS + DOT_RING)
        dot = _px(DOT_RADIUS)
        draw.ellipse((x - ring, y - ring, x + ring, y + ring), fill=SURFACE)
        draw.ellipse((x - dot, y - dot, x + dot, y + dot), fill=color)
        ends[key] = bottom - height * rows[key][-1] / axis_top
    (first_key, _l1, _c1), (second_key, _l2, _c2) = series
    first_y, second_y = _spread(ends[first_key], ends[second_key], top, bottom)
    labels = ((first_key, first_y), (second_key, second_y))
    if rows[first_key][-1] == rows[second_key][-1]:
        labels = ((first_key, ends[first_key]),)  # the same number twice says nothing: one label on the shared dot
    for key, y in labels:
        _text(
            draw,
            (PLOT_RIGHT + VALUE_GAP, y),
            str(int(rows[key][-1])),
            size=VALUE_SIZE,
            bold=True,
            fill=TEXT_PRIMARY,
            anchor="lm",
        )


def render_chart(points) -> bytes:
    points = list(points)
    if not points:
        raise ValueError("render_chart needs at least one point")
    canvas = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), SURFACE)
    draw = ImageDraw.Draw(canvas)
    days = "1 day" if len(points) == 1 else f"{len(points)} days"
    _text(draw, CAPTION_POS, f"Last {days} · UTC")
    _stacked_panel(
        draw, points, ANSWERS_TITLE, ANSWERS_TOP, ANSWERS_HEIGHT, ANSWERS_SERIES, lambda p: p.answers, cost=False
    )
    _stacked_panel(draw, points, COST_TITLE, COST_TOP, COST_HEIGHT, COST_SERIES, lambda p: p.cost, cost=True)
    _lines_panel(draw, points, TICKETS_TITLE, TICKETS_TOP, TICKETS_HEIGHT, TICKETS_SERIES)
    image = canvas.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
