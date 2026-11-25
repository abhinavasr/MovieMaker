"""Character sheets: one picture with several views of a person (front, 3/4, side, back, face)
side by side on a plain background, usually with labels.

2026-11-25, the person gave Abhi's and Khaing's five-view turnaround sheets as their character
pictures ("use this for Abhi"); the assistant first lost them, then used the WHOLE sheet as the
main picture, so every shot picture was drawn from five copies of the person. "He should have
cropped." Now a sheet given to a character is cut into its views: the front view becomes the
main picture and the others the character's own views (never redrawn).

Pure Pillow (the studio image has no numpy): the layout is found on a ~600 px copy.
"""
from __future__ import annotations

from PIL import Image, ImageOps

KEYS = ("front", "three_quarter", "side", "back")


def _bg(img: Image.Image) -> tuple[int, int, int]:
    """The background colour: the median of the border pixels."""
    w, h = img.size
    px = img.load()
    border = [px[x, y] for x in range(0, w, 3) for y in (1, h - 2)] + \
             [px[x, y] for y in range(0, h, 3) for x in (1, w - 2)]
    return tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))


def _runs(flags: list[bool], min_len: int, max_gap: int) -> list[tuple[int, int]]:
    """Contiguous True runs (gaps up to max_gap bridged), at least min_len long."""
    runs, start, gap = [], None, 0
    for i, f in enumerate(flags + [False] * (max_gap + 1)):
        if f:
            if start is None:
                start = i
            gap, end = 0, i
        elif start is not None:
            gap += 1
            if gap > max_gap:
                if end - start + 1 >= min_len:
                    runs.append((start, end))
                start, gap = None, 0
    return runs


def find_panels(img: Image.Image) -> list[dict]:
    """The figures/blocks of a sheet, left to right: [{box, fill}] in this image's pixels."""
    w, h = img.size
    px = img.load()
    bg = _bg(img)
    fg = [[sum(abs(a - b) for a, b in zip(px[x, y], bg)) > 36 for x in range(w)] for y in range(h)]
    band = range(int(h * 0.12), int(h * 0.88))          # leave out the title and the labels
    cols = [sum(fg[y][x] for y in band) / len(band) > 0.03 for x in range(w)]
    panels = []
    for x0, x1 in _runs(cols, max(4, w // 40), max(2, w // 120)):
        rows = [sum(fg[y][x0:x1 + 1]) / (x1 - x0 + 1) > 0.03 for y in range(h)]
        segs = _runs(rows, max(4, h // 20), max(2, h // 80))
        if not segs:
            continue
        y0, y1 = max(segs, key=lambda s: s[1] - s[0])     # the figure, not its label or title
        area = (x1 - x0 + 1) * (y1 - y0 + 1)
        fill = sum(fg[y][x] for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)) / area
        panels.append({"box": (x0, y0, x1 + 1, y1 + 1), "fill": fill})
    return panels


def is_sheet(path) -> bool:
    try:
        with Image.open(path) as im:
            w, h = im.size
            if w < h * 1.35:                              # sheets are wide
                return False
            small = ImageOps.exif_transpose(im).convert("RGB")
            small.thumbnail((600, 600))
            return len(find_panels(small)) >= 3
    except Exception:                                      # noqa: BLE001
        return False


def split(path) -> dict[str, Image.Image] | None:
    """{front, three_quarter, side, back, face, extra_N}: crops of a sheet, or None if the
    picture is not one. The panel much wider than the rest is the face close-up; the standing
    figures are named left to right in the usual turnaround order."""
    with Image.open(path) as im:
        full = ImageOps.exif_transpose(im).convert("RGB")
    W, H = full.size
    if W < H * 1.35:
        return None
    small = full.copy()
    small.thumbnail((600, 600))
    s = W / small.size[0]
    panels = find_panels(small)
    if len(panels) < 3:
        return None
    bg = _bg(small)
    out: dict[str, Image.Image] = {}
    figures = []
    # The close-up is the panel much wider than the standing figures (a photo block, often
    # on the same grey as the sheet, so "how full it is" does not tell it apart).
    widths = sorted(p["box"][2] - p["box"][0] for p in panels)
    median = widths[len(widths) // 2] if len(widths) % 2 else widths[len(widths) // 2 - 1]
    widest = max(panels, key=lambda p: p["box"][2] - p["box"][0])
    face = widest if (widest["box"][2] - widest["box"][0]) > 1.35 * median else None
    for p in panels:
        x0, y0, x1, y1 = p["box"]
        pad = int(6 * s)
        box = (max(0, int(x0 * s) - pad), max(0, int(y0 * s) - pad),     # little below: the
               min(W, int(x1 * s) + pad), min(H, int(y1 * s) + int(s)))   # label sits there
        crop = full.crop(box)
        if p is face:
            out["face"] = crop
        else:
            figures.append(crop)
    for i, crop in enumerate(figures):
        # a tall narrow figure is padded with the background to a 9:16-ish frame
        cw, ch = crop.size
        tw = max(cw, int(ch * 0.5))
        if tw > cw:
            framed = Image.new("RGB", (tw, ch), bg)
            framed.paste(crop, ((tw - cw) // 2, 0))
            crop = framed
        out[KEYS[i] if i < len(KEYS) else f"extra_{i - len(KEYS) + 1}"] = crop
    return out if "front" in out else None
