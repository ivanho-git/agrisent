"""Generate PWA icons for AGRI-SENTINEL"""
from PIL import Image, ImageDraw, ImageFont
import os

SIZES = [192, 512]
OUT_DIR = os.path.join(os.path.dirname(__file__), "static", "icons")
os.makedirs(OUT_DIR, exist_ok=True)

for size in SIZES:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background circle
    pad = int(size * 0.05)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=int(size * 0.22),
        fill=(6, 78, 59),  # --e9 green
    )

    # Inner accent circle
    inner_pad = int(size * 0.15)
    draw.rounded_rectangle(
        [inner_pad, inner_pad, size - inner_pad, size - inner_pad],
        radius=int(size * 0.16),
        fill=(5, 150, 105),  # --e6 green
    )

    # Leaf emoji / text
    try:
        font_size = int(size * 0.45)
        font = ImageFont.truetype("arial.ttf", font_size)
    except:
        font = ImageFont.load_default()

    text = "🌱"
    # Use "AS" as fallback if emoji doesn't render well
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2
    y = (size - th) // 2 - int(size * 0.03)
    draw.text((x, y), text, fill="white", font=font)

    # If emoji didn't render, draw "AS" text
    # Overlay "AS" as a reliable fallback
    try:
        small_font_size = int(size * 0.28)
        small_font = ImageFont.truetype("arial.ttf", small_font_size)
        text2 = "AS"
        bbox2 = draw.textbbox((0, 0), text2, font=small_font)
        tw2, th2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
        x2 = (size - tw2) // 2
        y2 = (size - th2) // 2
        draw.text((x2, y2), text2, fill="white", font=small_font)
    except:
        pass

    path = os.path.join(OUT_DIR, f"icon-{size}.png")
    img.save(path, "PNG")
    print(f"Created {path} ({size}x{size})")

print("Done!")
