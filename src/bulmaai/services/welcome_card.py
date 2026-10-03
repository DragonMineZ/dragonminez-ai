import io
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

CANVAS_WIDTH = 800
CANVAS_HEIGHT = 510
AVATAR_DIAMETER = 280
RING_WIDTH = 8
AVATAR_TOP = 14
TITLE_TEXT = "WELCOME"
TITLE_SIZE = 120
NAME_MAX_SIZE = 64
NAME_MIN_SIZE = 24
TEXT_MARGIN = 40
TEXT_COLOR = (62, 184, 138, 255)
GLOW_COLOR = (0, 0, 0, 200)
RING_COLOR = (255, 255, 255, 255)
GLOW_BLUR_RADIUS = 6
GLOW_OFFSET = 3

FONT_CANDIDATES = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "Arial Bold.ttf",
    "arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)


@lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _circular_avatar(avatar_bytes: bytes) -> Image.Image:
    inner = AVATAR_DIAMETER - 2 * RING_WIDTH
    with Image.open(io.BytesIO(avatar_bytes)) as source:
        avatar = source.convert("RGBA").resize((inner, inner), Image.Resampling.LANCZOS)

    scale = 4
    mask = Image.new("L", (inner * scale, inner * scale), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, inner * scale - 1, inner * scale - 1), fill=255)
    mask = mask.resize((inner, inner), Image.Resampling.LANCZOS)

    badge = Image.new("RGBA", (AVATAR_DIAMETER, AVATAR_DIAMETER), (0, 0, 0, 0))
    ring_mask = Image.new("L", (AVATAR_DIAMETER * scale, AVATAR_DIAMETER * scale), 0)
    ImageDraw.Draw(ring_mask).ellipse(
        (0, 0, AVATAR_DIAMETER * scale - 1, AVATAR_DIAMETER * scale - 1), fill=255
    )
    ring_mask = ring_mask.resize((AVATAR_DIAMETER, AVATAR_DIAMETER), Image.Resampling.LANCZOS)
    badge.paste(Image.new("RGBA", badge.size, RING_COLOR), (0, 0), ring_mask)
    badge.paste(avatar, (RING_WIDTH, RING_WIDTH), mask)
    return badge


def _fit_name_font(draw: ImageDraw.ImageDraw, text: str) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    limit = CANVAS_WIDTH - 2 * TEXT_MARGIN
    for size in range(NAME_MAX_SIZE, NAME_MIN_SIZE - 1, -2):
        font = _font(size)
        if draw.textlength(text, font=font) <= limit:
            return font
    return _font(NAME_MIN_SIZE)


def _draw_centered(
    canvas: Image.Image,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    top: int,
) -> int:
    probe = ImageDraw.Draw(canvas)
    left, text_top, right, bottom = probe.textbbox((0, 0), text, font=font)
    x = (CANVAS_WIDTH - (right - left)) // 2 - left
    y = top - text_top

    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).text((x, y + GLOW_OFFSET), text, font=font, fill=GLOW_COLOR)
    glow = glow.filter(ImageFilter.GaussianBlur(GLOW_BLUR_RADIUS))
    canvas.alpha_composite(glow)

    ImageDraw.Draw(canvas).text((x, y), text, font=font, fill=TEXT_COLOR)
    return top + (bottom - text_top)


def render_welcome_card(avatar_bytes: bytes, username: str) -> bytes:
    canvas = Image.new("RGBA", (CANVAS_WIDTH, CANVAS_HEIGHT), (0, 0, 0, 0))
    badge = _circular_avatar(avatar_bytes)
    canvas.alpha_composite(badge, ((CANVAS_WIDTH - AVATAR_DIAMETER) // 2, AVATAR_TOP))

    title_bottom = _draw_centered(canvas, TITLE_TEXT, _font(TITLE_SIZE), AVATAR_TOP + AVATAR_DIAMETER + 18)
    name = username.upper()
    name_font = _fit_name_font(ImageDraw.Draw(canvas), name)
    _draw_centered(canvas, name, name_font, title_bottom + 14)

    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()
