"""Generate AMVelo favicon assets: green bike icon for tabs, complete logo for bookmarks."""
import os
from PIL import Image, ImageDraw, ImageFont

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
GREEN_BIKE = "#238b52"       # light-theme --greenway
GREEN_TEXT = "#335d4a"       # light-theme --green-dark
GREEN_ACCENT = "#7caa8f"     # brand-period color
BACKGROUND = "#fbfcfa"       # light-theme --paper


def draw_bike_icon(size, background=None, padding=0.1):
    """Draw the AMVelo bike icon at the given size."""
    canvas = Image.new("RGB", (size, size), background if background else (252, 252, 250))
    draw = ImageDraw.Draw(canvas)

    scale = size / 32.0
    pad = size * padding
    inner = size - 2 * pad
    s = inner / 32.0

    def x(val):
        return pad + val * s

    def y(val):
        return pad + val * s

    r = 5.5 * s
    green = tuple(int(GREEN_BIKE[i:i+2], 16) for i in (1, 3, 5))

    cx1, cy1 = x(8), y(22)
    cx2, cy2 = x(24), y(22)

    draw.ellipse([cx1 - r, cy1 - r, cx1 + r, cy1 + r], fill=green)
    draw.ellipse([cx2 - r, cy2 - r, cx2 + r, cy2 + r], fill=green)

    points = [
        (8, 22), (14, 12), (20, 22), (8, 22),
        (14, 12), (19, 12), (16, 12), (24, 22),
    ]
    scaled_points = [(x(px), y(py)) for px, py in points]
    line_width = max(2, round(1.8 * s * scale))

    draw.line([scaled_points[0], scaled_points[1]], fill=green, width=line_width, joint="curve")
    draw.line([scaled_points[1], scaled_points[2]], fill=green, width=line_width, joint="curve")
    draw.line([scaled_points[2], scaled_points[3]], fill=green, width=line_width, joint="curve")
    draw.line([scaled_points[4], scaled_points[5]], fill=green, width=line_width, joint="curve")
    draw.line([scaled_points[6], scaled_points[7]], fill=green, width=line_width, joint="curve")

    hub_r = r * 0.25
    hub_color = tuple(max(0, c - 30) for c in green)
    draw.ellipse([cx1 - hub_r, cy1 - hub_r, cx1 + hub_r, cy1 + hub_r], fill=hub_color)
    draw.ellipse([cx2 - hub_r, cy2 - hub_r, cx2 + hub_r, cy2 + hub_r], fill=hub_color)

    return canvas


def draw_complete_logo(size):
    """Draw the complete AMVelo logo: bike icon + 'AMVelo' text."""
    canvas = Image.new("RGB", (size, size), (252, 252, 250))
    draw = ImageDraw.Draw(canvas)

    icon_size = int(size * 0.45)
    icon = draw_bike_icon(icon_size, background=(252, 252, 250), padding=0.15)
    canvas.paste(icon, (int(size * 0.08), int(size * 0.3)))

    text_x = size // 2 + 5
    text_y = size // 2 - int(size * 0.12)

    try:
        font = ImageFont.truetype("arialbd.ttf", int(size * 0.25))
    except (OSError, IOError):
        font = ImageFont.load_default()

    green_rgb = tuple(int(GREEN_TEXT[i:i+2], 16) for i in (1, 3, 5))

    am_width = draw.textlength("AM", font=font)
    draw.text((text_x, text_y), "AM", fill=green_rgb, font=font)
    v_width = draw.textlength("V", font=font)
    draw.text((text_x + am_width, text_y), "V", fill=green_rgb, font=font)
    draw.text((text_x + am_width + v_width, text_y), "elo", fill=green_rgb, font=font)

    return canvas


def generate_svg_favicon():
    """Generate the SVG favicon with a green bike icon on a light background."""
    svg_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">
  <rect width="32" height="32" fill="{BACKGROUND}"/>
  <g fill="none" stroke="{GREEN_BIKE}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="8" cy="22" r="5.5" fill="none"/>
    <circle cx="24" cy="22" r="5.5" fill="none"/>
    <path d="m8 22 6-10 6 10H8Zm6-10h5m-3 0 8 10"/>
  </g>
</svg>'''
    svg_path = os.path.join(STATIC_DIR, "favicon.svg")
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(svg_content)
    print(f"Created {svg_path}")


def generate_all():
    os.makedirs(STATIC_DIR, exist_ok=True)

    # 1. SVG favicon — green bike icon for browser tabs
    generate_svg_favicon()

    # 2. PNG favicons — green bike icon (for browsers without SVG support)
    for size in [16, 32, 48]:
        icon = draw_bike_icon(size)
        png_path = os.path.join(STATIC_DIR, f"favicon-{size}x{size}.png")
        icon.save(png_path, "PNG")
        print(f"Created {png_path}")

    # 3. ICO file — combines 16, 32, 48 for maximum browser compatibility
    ico_path = os.path.join(STATIC_DIR, "favicon.ico")
    draw_bike_icon(32).save(
        ico_path, "ICO", sizes=[(16, 16), (32, 32), (48, 48)]
    )
    print(f"Created {ico_path}")

    # 4. Complete logo — bike + "AMVelo" text for Opera/bookmark favorites
    for size in [180, 192, 512]:
        logo = draw_complete_logo(size)
        if size == 180:
            png_path = os.path.join(STATIC_DIR, "apple-touch-icon.png")
        else:
            png_path = os.path.join(STATIC_DIR, f"android-chrome-{size}x{size}.png")
        logo.save(png_path, "PNG")
        print(f"Created {png_path}")

    print("\nAll favicon assets generated successfully!")


if __name__ == "__main__":
    generate_all()

