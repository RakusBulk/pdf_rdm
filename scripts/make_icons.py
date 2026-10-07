"""Regenerate the viewer icons (viewer/assets/icon.png, icon.ico, icon.icns).

Needs Pillow (not a runtime dependency):  pip install pillow
The .icns step uses macOS' `iconutil`, so it is skipped on other systems.

    python scripts/make_icons.py
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

OUT = Path(__file__).resolve().parent.parent / "viewer" / "assets"
SIZE = 1024
SS = 2  # supersampling for smooth edges


def _gradient(size: int, top: str, bottom: str) -> Image.Image:
    grad = Image.linear_gradient("L").resize((size, size))  # black (top) -> white (bottom)
    return ImageOps.colorize(grad, black=top, white=bottom).convert("RGBA")


def draw_master() -> Image.Image:
    s = SIZE * SS
    k = s / 1024  # design is laid out on a 1024 grid

    def p(*v):  # scale grid units to pixels
        return [round(x * k) for x in v]

    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # Rounded-square plate with a deep-red gradient (matches the dashboard accent).
    plate = _gradient(s, "#e0393e", "#7a1013")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(p(100, 100, 924, 924), radius=round(190 * k), fill=255)
    canvas.paste(plate, (0, 0), mask)
    d = ImageDraw.Draw(canvas)

    # Soft highlight: white fading out from the top of the plate.
    fade = Image.linear_gradient("L").transpose(Image.FLIP_TOP_BOTTOM).resize((s, s))  # white at top
    fade = fade.point(lambda v: int(max(0, v - 128) * 0.55))  # visible only in the top half
    hi = Image.new("RGBA", (s, s), (255, 255, 255, 0))
    hi.putalpha(fade)
    hi_masked = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    hi_masked.paste(hi, (0, 0), mask)
    canvas.alpha_composite(hi_masked)
    d = ImageDraw.Draw(canvas)

    # Drop shadow under the page.
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon(
        [tuple(p(x, y)) for x, y in [(310, 262), (580, 262), (710, 392), (710, 762), (310, 762)]],
        fill=(0, 0, 0, 70),
    )
    from PIL import ImageFilter
    shadow = shadow.filter(ImageFilter.GaussianBlur(round(18 * k)))
    canvas.alpha_composite(shadow, (0, round(14 * k)))
    d = ImageDraw.Draw(canvas)

    # The page: white sheet with a folded top-right corner.
    page = [(290, 240), (560, 240), (690, 370), (690, 740), (290, 740)]
    d.polygon([tuple(p(x, y)) for x, y in page], fill="#ffffff")
    d.polygon([tuple(p(x, y)) for x, y in [(560, 240), (690, 370), (560, 370)]], fill="#f1c9c9")

    # Text lines on the page.
    for y, x1 in [(450, 620), (510, 620), (570, 620), (630, 500)]:
        d.rounded_rectangle(p(340, y, x1, y + 28), radius=round(14 * k), fill="#d9b0b0")

    # Lock badge (dark disc with a white ring, white padlock).
    cx, cy, r = 700, 700, 170
    d.ellipse(p(cx - r - 22, cy - r - 22, cx + r + 22, cy + r + 22), fill="#ffffff")
    d.ellipse(p(cx - r, cy - r, cx + r, cy + r), fill="#2a2d33")
    # shackle
    d.arc(p(cx - 62, cy - 120, cx + 62, cy + 4), start=180, end=360, fill="#ffffff", width=round(26 * k))
    d.rectangle(p(cx - 62, cy - 58, cx - 36, cy - 20), fill="#ffffff")
    d.rectangle(p(cx + 36, cy - 58, cx + 62, cy - 20), fill="#ffffff")
    # body
    d.rounded_rectangle(p(cx - 92, cy - 28, cx + 92, cy + 96), radius=round(26 * k), fill="#ffffff")
    # keyhole
    d.ellipse(p(cx - 17, cy + 6, cx + 17, cy + 40), fill="#2a2d33")
    d.polygon([tuple(p(x, y)) for x, y in [(cx - 11, cy + 30), (cx + 11, cy + 30), (cx + 16, cy + 72), (cx - 16, cy + 72)]],
              fill="#2a2d33")

    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    master = draw_master()
    master.save(OUT / "icon.png")

    master.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

    if platform.system() == "Darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "icon.iconset"
            iconset.mkdir()
            for base in (16, 32, 128, 256, 512):
                master.resize((base, base), Image.LANCZOS).save(iconset / f"icon_{base}x{base}.png")
                master.resize((base * 2, base * 2), Image.LANCZOS).save(iconset / f"icon_{base}x{base}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT / "icon.icns")], check=True)
    else:
        print("skipped icon.icns (needs macOS iconutil)")
    print("wrote", ", ".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
