"""App icon rendering. Deliberately PIL-only so packaging scripts can
render the icon at build time without pulling in the GUI stack."""

from PIL import Image, ImageDraw, ImageFilter


def create_app_icon(size: int = 256) -> Image.Image:
    """Generate RGB Venn diagram icon with transparent background."""
    r_ch = Image.new("L", (size, size), 0)
    g_ch = Image.new("L", (size, size), 0)
    b_ch = Image.new("L", (size, size), 0)

    cx, cy = size // 2, size // 2
    radius = int(size * 0.32)
    spread = int(size * 0.16)

    # Red = top, Green = bottom-left, Blue = bottom-right
    circles = [
        (r_ch, cx, cy - spread),
        (g_ch, cx - int(spread * 0.87), cy + int(spread * 0.5)),
        (b_ch, cx + int(spread * 0.87), cy + int(spread * 0.5)),
    ]
    for ch, ox, oy in circles:
        ImageDraw.Draw(ch).ellipse(
            [ox - radius, oy - radius, ox + radius, oy + radius], fill=255,
        )

    # Additive merge: overlaps naturally produce C/M/Y/W
    rgb = Image.merge("RGB", (r_ch, g_ch, b_ch))

    # Soft glow: blend with a blurred version
    glow = rgb.filter(ImageFilter.GaussianBlur(radius=size // 16))
    rgb = Image.blend(rgb, glow, 0.4)

    # Alpha from brightness — black becomes transparent
    alpha = rgb.convert("L")
    img = rgb.convert("RGBA")
    img.putalpha(alpha)
    return img
