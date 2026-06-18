"""Generate PWA icons using the real IXL white logo.
Design spec (from IXL Studio.dc.html review):
  - Dark background #1A1A1A
  - IXL white logo (with red diamond) centred
  - 'Studio' text below — NO pipe, larger size
  - No red stripe at bottom
"""
from PIL import Image, ImageDraw, ImageFont
import os

HERE    = os.path.dirname(os.path.abspath(__file__))
ROOT    = os.path.dirname(HERE)
LOGO    = os.path.join(ROOT, "static", "IXL-white.png")
OUT_DIR = os.path.join(ROOT, "static")

BG     = (26, 26, 26, 255)    # #1A1A1A
WHITE  = (255, 255, 255, 255)
FONT_BOLD = r"C:\Windows\Fonts\arialbd.ttf"


def create_icon(size: int) -> Image.Image:
    img  = Image.new("RGBA", (size, size), BG)
    draw = ImageDraw.Draw(img)

    # --- load & scale IXL logo ---
    logo = Image.open(LOGO).convert("RGBA")
    lw, lh = logo.size                        # 232 × 104
    target_w = int(size * 0.55)               # 55% of icon width
    scale    = target_w / lw
    new_lw   = target_w
    new_lh   = int(lh * scale)
    logo     = logo.resize((new_lw, new_lh), Image.LANCZOS)

    # --- fonts ---
    sz_studio = int(size * 0.20)
    try:
        f_studio = ImageFont.truetype(FONT_BOLD, sz_studio)
    except OSError:
        f_studio = ImageFont.load_default()

    # --- measure Studio text ---
    stu_bb = draw.textbbox((0, 0), "Studio", font=f_studio)
    stu_w  = stu_bb[2] - stu_bb[0]
    stu_h  = stu_bb[3] - stu_bb[1]

    gap_logo_stu = int(size * 0.06)

    # total content block height (logo already includes "since 1858" baked in)
    block_h = new_lh + gap_logo_stu + stu_h
    top_y   = (size - block_h) // 2

    # --- paste logo (alpha composite) ---
    logo_x = (size - new_lw) // 2
    logo_y = top_y
    img.paste(logo, (logo_x, logo_y), logo)

    # --- Studio text (white, bold, no pipe) ---
    stu_x = (size - stu_w) // 2
    stu_y = logo_y + new_lh + gap_logo_stu
    draw.text((stu_x, stu_y), "Studio", fill=WHITE, font=f_studio)

    return img.convert("RGB")


for size, name in [(512, "icon-512.png"), (192, "icon-192.png"), (180, "apple-touch-icon.png")]:
    img  = create_icon(size)
    path = os.path.join(OUT_DIR, name)
    img.save(path)
    print(f"  wrote {path}")

print("Done.")
