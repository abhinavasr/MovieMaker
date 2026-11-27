"""Animated data graphics for productions -- the numbers a podcast's hosts cite, on screen.

2026-11-27, the person: "the debates should also bring in the stats and numbers on graphs, not
just 2 people talking" and "the graphs should be animated wherever possible". Drawn by code
(Pillow + ffmpeg), never by an image model: a picture model garbles digits, and these numbers
are checked against their sources. Rendered on the CPU in seconds; no GPU, no review needed.

A chart is a dict:
  {"kind": "compare" | "single" | "share" | "trend",
   "title": "What the survey found",
   "items": [{"label": "Think AI skills matter", "value": 54, "display": "54%"}, ...],
   "unit": "%", "source": "edX workforce survey, 2025", "speaker": "Mahira Singh"}
compare = 2-6 labelled bars; single = one big number; share = one percentage as a ring;
trend = values over time (labels are the years/dates, in order).
"""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

KINDS = ("compare", "single", "share", "trend")
FPS = 25
ANIM_S = 1.6                 # the build-up; then the chart holds with a slow push-in
SS = 2                       # supersampling for clean edges

# The studio's own palette (app.css :root): dark, gold the one accent.
BG = (12, 13, 15)
PANEL = (27, 29, 34)
LINE = (52, 56, 66)
INK = (236, 231, 223)
MUTED = (155, 150, 141)
GOLD = (227, 165, 74)
STEEL = (122, 167, 224)

_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_fonts: dict = {}


def _f(size: int, bold: bool = False):
    key = (size, bold)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(_BOLD if bold else _FONT, size)
    return _fonts[key]


def _ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _parts(display: str, value: float) -> tuple[str, str, int]:
    """'$16,000' -> ('$', '', 0) prefix/suffix/decimals, so a count-up looks like the figure."""
    m = re.match(r"^\s*([^\d\-]*)(-?[\d,]*\.?\d*)(.*)$", str(display or ""))
    if not m or not m.group(2):
        return "", "", 0 if float(value).is_integer() else 1
    num = m.group(2)
    dec = len(num.split(".")[1]) if "." in num else 0
    return m.group(1), m.group(3), dec


def _fmt(value: float, display: str, frac: float) -> str:
    pre, suf, dec = _parts(display, value)
    if frac >= 1:
        return display or f"{value:g}"
    v = value * frac
    s = f"{v:,.{dec}f}" if "," in (display or "") else f"{v:.{dec}f}"
    return f"{pre}{s}{suf}"


def valid(chart: dict) -> bool:
    if not isinstance(chart, dict) or chart.get("kind") not in KINDS:
        return False
    items = [i for i in chart.get("items") or [] if isinstance(i, dict) and
             isinstance(i.get("value"), (int, float)) and math.isfinite(i["value"])]
    need = {"compare": 2, "trend": 2, "single": 1, "share": 1}[chart["kind"]]
    if chart["kind"] == "share" and items and not 0 <= items[0]["value"] <= 100:
        return False
    return len(items) >= need


def _wrap(draw, text: str, font, width: int) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def frame(chart: dict, w: int, h: int, t: float) -> Image.Image:
    """The chart at time t (seconds since it appeared), drawn at SS x size."""
    W, H = w * SS, h * SS
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    u = min(W, H) / 100                            # layout unit
    pad = 7 * u
    # title, appearing first
    title_a = _ease(t / 0.5)
    tf = _f(int(6.2 * u), True)
    y = pad
    for line in _wrap(d, chart.get("title") or "", tf, W - 2 * pad)[:2]:
        d.text((pad, y), line, font=tf, fill=_mix(BG, INK, title_a))
        y += 7.6 * u
    # a gold rule under the title, drawing itself
    d.rectangle([pad, y + 1.2 * u, pad + (W - 2 * pad) * _ease(t / 0.8) * 0.18, y + 1.8 * u], fill=GOLD)
    top = y + 6 * u
    foot = H - pad - 4 * u
    items = [i for i in chart.get("items") or [] if isinstance(i, dict)]
    kind = chart.get("kind")
    grow = lambda k=0: _ease((t - 0.35 - 0.12 * k) / 1.1)       # staggered
    if kind == "compare":
        _bars(d, items[:6], pad, top, W - pad, foot - 4 * u, u, grow)
    elif kind == "trend":
        _trend(d, items[:12], pad, top, W - pad, foot - 4 * u, u, t)
    elif kind == "share":
        _ring(d, items[0], W / 2, (top + foot) / 2, min(W, foot - top) * 0.34, u, grow(0))
    else:
        _single(d, items[0], W / 2, (top + foot) / 2, u, grow(0), W - 2 * pad)
    # source and speaker, at the foot
    sf = _f(int(3.0 * u))
    name_w = 0
    if chart.get("speaker"):
        name = chart["speaker"]
        nf = _f(int(3.2 * u), True)
        name_w = d.textlength(name, font=nf)
        d.text((W - pad - name_w, foot + 1.4 * u), name, font=nf, fill=_mix(BG, GOLD, title_a))
    src = chart.get("source") or ""
    if src:
        room = W - 2 * pad - name_w - (6 * u if name_w else 0)
        txt = f"Source: {src}"
        if d.textlength(txt, font=sf) > room:             # cut to fit beside the speaker's name
            while txt and d.textlength(txt + "…", font=sf) > room:
                txt = txt[:-1]
            txt = txt.rstrip(" ,(") + "…"
        d.text((pad, foot + 1.5 * u), txt, font=sf, fill=_mix(BG, MUTED, title_a))
    return im


def _mix(a, b, k):
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def _bars(d, items, x0, y0, x1, y1, u, grow):
    n = len(items)
    vmax = max(abs(i["value"]) for i in items) or 1
    row = (y1 - y0) / n
    bh = min(row * 0.52, 11 * u)
    lf, vf = _f(int(3.6 * u)), _f(int(4.6 * u), True)
    label_w = min((x1 - x0) * 0.34, max(d.textlength(str(i.get("label", "")), font=lf) for i in items) + 2 * u)
    for k, it in enumerate(items):
        cy = y0 + row * (k + 0.5)
        g = grow(k)
        lines = _wrap(d, str(it.get("label", "")), lf, label_w - 2 * u)[:2]
        ly = cy - len(lines) * 2.4 * u
        for ln in lines:
            d.text((x0, ly), ln, font=lf, fill=INK)
            ly += 4.8 * u
        bx0 = x0 + label_w
        full = (x1 - bx0 - 16 * u) * abs(it["value"]) / vmax
        d.rounded_rectangle([bx0, cy - bh / 2, bx0 + full, cy + bh / 2], radius=bh * 0.18, fill=PANEL)
        if g > 0.001:
            d.rounded_rectangle([bx0, cy - bh / 2, bx0 + max(bh * 0.4, full * g), cy + bh / 2],
                                radius=bh * 0.18, fill=GOLD if k == 0 else STEEL)
        txt = _fmt(it["value"], str(it.get("display") or ""), g)
        d.text((bx0 + full * g + 2 * u, cy - 2.9 * u), txt, font=vf, fill=_mix(BG, INK, min(1, g * 2)))


def _single(d, it, cx, cy, u, g, width):
    big = _f(int(22 * u), True)
    txt = _fmt(it["value"], str(it.get("display") or ""), g)
    final = str(it.get("display") or f"{it['value']:g}")
    tw = d.textlength(final, font=big)
    d.text((cx - tw / 2, cy - 16 * u), txt, font=big, fill=GOLD)
    lf = _f(int(4.4 * u))
    y = cy + 10 * u
    for ln in _wrap(d, str(it.get("label", "")), lf, width * 0.8)[:3]:
        lw = d.textlength(ln, font=lf)
        d.text((cx - lw / 2, y), ln, font=lf, fill=_mix((12, 13, 15), INK, min(1, g * 1.5)))
        y += 5.6 * u


def _ring(d, it, cx, cy, r, u, g):
    wd = r * 0.22
    box = [cx - r, cy - r, cx + r, cy + r]
    d.ellipse(box, outline=PANEL, width=int(wd))
    ang = 360 * it["value"] / 100 * g
    if ang > 0.5:
        d.arc(box, -90, -90 + ang, fill=GOLD, width=int(wd))
    big = _f(int(r * 0.42), True)
    txt = _fmt(it["value"], str(it.get("display") or f"{it['value']:g}%"), g)
    tw = d.textlength(txt, font=big)
    d.text((cx - tw / 2, cy - r * 0.3), txt, font=big, fill=INK)
    lf = _f(int(3.8 * u))
    y = cy + r + 4 * u
    for ln in _wrap(d, str(it.get("label", "")), lf, r * 3.2)[:2]:
        lw = d.textlength(ln, font=lf)
        d.text((cx - lw / 2, y), ln, font=lf, fill=INK)
        y += 4.8 * u


def _trend(d, items, x0, y0, x1, y1, u, t):
    vals = [i["value"] for i in items]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    lo = min(lo, 0) if lo >= 0 and lo < hi * 0.5 else lo - (hi - lo) * 0.1
    lf, vf = _f(int(3.2 * u)), _f(int(3.8 * u), True)
    ax0, ax1, ay0, ay1 = x0 + 4 * u, x1 - 6 * u, y0 + 4 * u, y1 - 6 * u
    for k in range(4):                                       # light grid
        gy = ay1 - (ay1 - ay0) * k / 3
        d.line([ax0, gy, ax1, gy], fill=PANEL, width=max(1, int(0.25 * u)))
    pts = [(ax0 + (ax1 - ax0) * k / max(1, len(items) - 1),
            ay1 - (ay1 - ay0) * (v - lo) / (hi - lo)) for k, v in enumerate(vals)]
    prog = _ease((t - 0.3) / 1.4) * (len(pts) - 1)         # the line draws itself
    shown = [pts[0]]
    for k in range(1, len(pts)):
        if prog >= k:
            shown.append(pts[k])
        elif prog > k - 1:
            f = prog - (k - 1)
            shown.append((pts[k - 1][0] + (pts[k][0] - pts[k - 1][0]) * f,
                          pts[k - 1][1] + (pts[k][1] - pts[k - 1][1]) * f))
            break
    if len(shown) > 1:
        d.line(shown, fill=GOLD, width=int(1.1 * u), joint="curve")
    for k, (px, py) in enumerate(pts):
        if prog >= k - 0.001:
            rr = 1.6 * u
            d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=GOLD)
            txt = str(items[k].get("display") or f"{vals[k]:g}")
            tw = d.textlength(txt, font=vf)
            d.text((px - tw / 2, py - 7 * u), txt, font=vf, fill=INK)
        lab = str(items[k].get("label", ""))
        lw = d.textlength(lab, font=lf)
        d.text((px - lw / 2, ay1 + 2.2 * u), lab, font=lf, fill=MUTED)


def still(chart: dict, w: int, h: int) -> bytes:
    """The finished chart as a PNG (the shot's picture in the editor)."""
    import io
    im = frame(chart, w, h, ANIM_S + 1).resize((w, h), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def render(chart: dict, w: int, h: int, seconds: float, out: Path) -> Path:
    """The animated chart as a silent MP4 of `seconds` (the host's voice plays over it from
    the timeline). Build-up, then a slow push-in while it holds."""
    n = max(1, int(round(seconds * FPS)))
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                             "-s", f"{w}x{h}", "-r", str(FPS), "-i", "pipe:0", "-c:v", "libx264",
                             "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "18", str(out)],
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    held = None
    try:
        for k in range(n):
            t = k / FPS
            if t <= ANIM_S + 0.2:
                img = frame(chart, w, h, t).resize((w, h), Image.LANCZOS)
            else:
                if held is None:
                    held = frame(chart, w, h, ANIM_S + 1)
                z = 1 + 0.035 * _ease((t - ANIM_S) / max(1.0, seconds - ANIM_S))
                W, H = held.size
                cw, ch = W / z, H / z
                img = held.crop((int((W - cw) / 2), int((H - ch) / 2),
                                 int((W + cw) / 2), int((H + ch) / 2))).resize((w, h), Image.BILINEAR)
            proc.stdin.write(img.tobytes())
        proc.stdin.close()
        if proc.wait(timeout=300) != 0:
            raise RuntimeError("could not encode the chart: " + proc.stderr.read().decode()[-300:])
    except Exception:
        proc.kill()
        raise
    return out
