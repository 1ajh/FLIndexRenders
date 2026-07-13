"""Generate the FLIndexRenders app icon: an audio-render waveform tucked inside
a project folder — "your renders, indexed by project." Drawn from scratch with
Pillow so it shares the dark-tile look of the sister tool FLSearchBySample
without reusing its magnifier mark.

Produces icon.png (1024 + 256 for the web), icon.ico (Windows) and
icon.icns (macOS). Pure Pillow drawing — no external assets. Run:  python make_icon.py
"""

import os

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 4                      # supersample factor for smooth edges
BASE = 1024
N = BASE * SS

# -- palette ---------------------------------------------------------------
TILE_TOP = (47, 51, 59)
TILE_BOT = (28, 30, 36)
FOLDER_BACK = (58, 74, 99)      # the raised back tab
FOLDER_FRONT_TOP = (74, 94, 124)
FOLDER_FRONT_BOT = (52, 66, 88)
FOLDER_LIP = (120, 146, 184)
WAVE = (255, 159, 67)           # FL amber — the render
WAVE_HI = (255, 194, 128)
WAVE_DIM = (214, 126, 44)
SHADOW = (0, 0, 0, 70)


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def vertical_gradient(size, top, bottom):
    grad = Image.new('RGB', (1, size), 0)
    for y in range(size):
        grad.putpixel((0, y), lerp(top, bottom, y / max(size - 1, 1)))
    return grad.resize((size, size))


def _round_rect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def draw_icon():
    img = Image.new('RGBA', (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded tile with a soft top-down gradient.
    radius = int(N * 0.22)
    mask = Image.new('L', (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, N - 1, N - 1], radius, fill=255)
    img.paste(vertical_gradient(N, TILE_TOP, TILE_BOT), (0, 0), mask)

    # --- folder ---------------------------------------------------------
    # Folder body geometry (a manila-style folder, centred).
    fx0, fx1 = int(N * 0.17), int(N * 0.83)
    fy1 = int(N * 0.79)
    body_top = int(N * 0.36)
    tab_top = int(N * 0.28)
    r = int(N * 0.045)

    # Soft drop shadow under the folder.
    shadow = Image.new('RGBA', (N, N), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle([fx0 + int(N * 0.012), body_top + int(N * 0.03),
                          fx1 + int(N * 0.012), fy1 + int(N * 0.03)],
                         radius=r, fill=SHADOW)
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(N * 0.02)))
    img.alpha_composite(shadow)

    # Back tab (a trapezoid-ish raised corner at the top-left of the folder).
    tab_w = int((fx1 - fx0) * 0.42)
    _round_rect(d, [fx0, tab_top, fx0 + tab_w, body_top + int(N * 0.05)],
                int(N * 0.03), FOLDER_BACK)

    # Front panel with its own gentle gradient, clipped to a rounded rect.
    panel = vertical_gradient(N, FOLDER_FRONT_TOP, FOLDER_FRONT_BOT)
    pmask = Image.new('L', (N, N), 0)
    ImageDraw.Draw(pmask).rounded_rectangle([fx0, body_top, fx1, fy1],
                                            radius=r, fill=255)
    img.paste(panel, (0, 0), pmask)

    # A bright lip along the folder's top edge.
    d.rounded_rectangle([fx0, body_top, fx1, body_top + int(N * 0.055)],
                        radius=r, fill=FOLDER_LIP)
    d.rectangle([fx0, body_top + int(N * 0.03), fx1, body_top + int(N * 0.055)],
                fill=FOLDER_FRONT_TOP)

    # --- render waveform inside the folder ------------------------------
    # Symmetric bars around a midline: the classic "rendered audio" look.
    heights = [0.28, 0.55, 0.40, 0.82, 0.62, 1.0, 0.48, 0.72, 0.34, 0.58, 0.24]
    n = len(heights)
    pad = int(N * 0.075)
    wx0, wx1 = fx0 + pad, fx1 - pad
    mid = int((body_top + fy1) / 2) + int(N * 0.02)
    span = (wx1 - wx0)
    bar_w = int(span / (n * 1.7))
    gap = (span - bar_w * n) / (n - 1)
    max_h = int((fy1 - body_top) * 0.34)

    for i, h in enumerate(heights):
        x = int(wx0 + i * (bar_w + gap))
        bh = max(int(max_h * h), int(N * 0.012))
        # Alternate a touch of tone so the bars have life.
        fill = WAVE if i % 2 == 0 else WAVE_DIM
        d.rounded_rectangle([x, mid - bh, x + bar_w, mid + bh],
                            radius=bar_w // 2, fill=fill)

    # Bright centre highlight dots on the tallest bars for a little sparkle.
    for i, h in enumerate(heights):
        if h >= 0.8:
            x = int(wx0 + i * (bar_w + gap))
            d.rounded_rectangle([x, mid - int(N * 0.008), x + bar_w,
                                 mid + int(N * 0.008)],
                                radius=bar_w // 2, fill=WAVE_HI)

    return img.resize((BASE, BASE), Image.LANCZOS)


def main():
    icon = draw_icon()
    icon.save(os.path.join(HERE, 'icon.png'))
    icon.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, 'icon-256.png'))
    icon.save(os.path.join(HERE, 'icon.ico'),
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                     (128, 128), (256, 256)])
    try:
        icon.save(os.path.join(HERE, 'icon.icns'))
    except Exception as exc:  # ICNS support varies by Pillow build
        print(f'icns skipped: {exc!r}')
    print('icon assets written to', HERE)


if __name__ == '__main__':
    main()
