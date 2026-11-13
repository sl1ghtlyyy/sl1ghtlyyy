#!/usr/bin/env python3
"""Render the profile README cards as self-contained animated SVGs.

    python scripts/render_cards.py            # animated cards -> cards/
    STATIC=1 python scripts/render_cards.py   # final frame only -> preview/ (+ preview/index.html)

Every card is a standalone SVG shown through an <img> tag, so it may only use what
works there: one inline <style> (CSS keyframes), SMIL <animate>, and fonts embedded
as base64 WOFF data URIs, subset with pyftsubset to exactly the glyphs the card draws.

Each card carries both site palettes and picks one with @media (prefers-color-scheme: dark),
so it follows the viewer's OS/browser theme (see adaptive_svg).

Motion rules:
  * Every element's resting attributes are its final frame. CSS animations only
    describe how it arrives (`animation-fill-mode: both`), so removing them (STATIC=1,
    prefers-reduced-motion) leaves the finished card.
  * All CSS animations live inside @media (prefers-reduced-motion: no-preference).
  * SMIL-driven pieces (status pulse, typewriter, busiest-day ping) only take effect
    through the .motion / .typed classes, which are enabled by that same media query.
  * Delays share one page timeline (T_* below), so the README plays top to bottom.

Data comes from data/contributions.json (refreshed daily by .github/workflows/profile-cards.yml).
"""
from __future__ import annotations

import base64
import datetime as dt
import functools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
STATIC = os.environ.get("STATIC") == "1"
OUT = Path(os.environ.get("OUT") or ROOT / ("preview" if STATIC else "cards"))
DATA = ROOT / "data" / "contributions.json"

# --------------------------------------------------------------------------- content

PROFILE = {
    "logo": ("sl1ghtly", " />"),
    "badge": "available for work",            # None to hide the status pill
    "greeting": "Hey there!",
    "name": "I'm Abhishek Dhall.",
    "line": "I build iOS apps, break things ethically, and write about it.",
    "terminal": [("> ", "brand"), ("alias: ", "muted"), ("slightly", "ink"),
                 ("   status: ", "muted"), ("building • learning • shipping", "ink")],
    "portrait": ROOT / "assets" / "portrait.webp",  # None (or a missing file) to drop it
    "portrait_note": "that's me, hi",
}

TILES = [  # README links each tile to https://<url>
    {"slug": "work", "label": "01 · work", "title": "Shipped apps.",
     "text": "Melp.us and myValU / ISIC, iOS apps live on the App Store.", "url": "sl1ghtly.com/work"},
    {"slug": "writing", "label": "02 · writing", "title": "The writing.",
     "text": "Notes on what I'm building and where I get stuck.", "url": "sl1ghtly.com/writing"},
    {"slug": "roadmap", "label": "03 · roadmap", "title": "Security roadmap.",
     "text": "Learning mobile security in public, one step at a time.", "url": "sl1ghtly.com/roadmap"},
]

FEATURED = ["SysChats", "ShakesToTheMoon", "AnnoMap"]  # must match the repo-*.svg lines in README.md

# Page timeline (seconds). Each card starts where the one above it is mostly done.
T_HERO, T_TILES, T_CHART, T_REPOS, T_REPO_STEP = 0.0, 0.7, 1.15, 1.75, 0.22
T_GRAPH = T_REPOS + T_REPO_STEP * len(FEATURED) + 0.15

# --------------------------------------------------------------------------- brand (sl1ghtly.com tokens)

THEMES = {
    "light": {  # :root
        "card": "#ffffff", "paper": "#f3f4f6", "ink": "#171717", "muted": "#5f5f5f",
        "border": "#d4d6db", "subtle": "#e4e4e7", "empty": "#ebedf0",
        "brand": "#9a4e1c", "amber": "#ec9630", "strong": "#c46b36", "mint": "#23bd86",
        "glow": 0.10, "halo": 0.16,
    },
    "dark": {  # .dark
        "card": "#131b27", "paper": "#0d1219", "ink": "#f0ebe0", "muted": "#8d9ab0",
        "border": "#1e2c3f", "subtle": "#162030", "empty": "#1c2a3c",
        "brand": "#ec9630", "amber": "#ec9630", "strong": "#ec9630", "mint": "#45d6a0",
        "glow": 0.07, "halo": 0.13,
    },
}

LANG_COLORS = {"Swift": "#F05138", "HTML": "#e34c26", "Python": "#3572A5", "Java": "#b07219",
               "Jupyter Notebook": "#DA5B0B", "JavaScript": "#f1e05a", "TypeScript": "#3178c6"}


def mix(a: str, b: str, t: float) -> str:
    pa, pb = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b))
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def ramp(th: dict) -> list[str]:
    """Contribution levels 0-4 in the brand amber."""
    e, a = th["empty"], th["amber"]
    return [e, mix(e, a, .35), mix(e, a, .62), a, th["strong"] if th["strong"] != a else mix(a, "#ffffff", .18)]

# --------------------------------------------------------------------------- fonts

FONT_DIR = ROOT / "fonts"
FONTS = {  # key: (variable source, wght instance, css class, fallback stack)
    "sans": ("Geist[wght].ttf", 400, "fs", "Geist,system-ui,sans-serif"),
    "bold": ("Geist[wght].ttf", 600, "fb", "Geist,system-ui,sans-serif"),
    "mono": ("GeistMono[wght].ttf", 400, "fm", "'Geist Mono',ui-monospace,monospace"),
    "hand": ("Caveat[wght].ttf", 600, "fh", "Caveat,cursive"),
}
_TMP = Path(tempfile.mkdtemp(prefix="cards-"))


@functools.cache
def font_instance(key: str) -> tuple[Path, dict, dict, int]:
    """Static instance of a variable font at the weight we use + its metrics."""
    src, wght, *_ = FONTS[key]
    path = _TMP / f"{key}.ttf"
    font = instancer.instantiateVariableFont(TTFont(FONT_DIR / src), {"wght": wght})
    font.save(path)
    return path, font.getBestCmap(), {g: m[0] for g, m in font["hmtx"].metrics.items()}, font["head"].unitsPerEm


def measure(text: str, key: str, size: float, tracking: float = 0) -> float:
    _, cmap, adv, upm = font_instance(key)
    w = sum(adv.get(cmap.get(ord(c), ".notdef"), upm // 2) for c in text)
    return w * size / upm + tracking * len(text)


@functools.cache
def woff_data_uri(key: str, chars: str) -> str:
    """Subset with pyftsubset (fontTools.subset) to `chars`, as a base64 WOFF data URI."""
    path, cmap, *_ = font_instance(key)
    if missing := sorted({c for c in chars if ord(c) not in cmap and not c.isspace()}):
        print(f"  ! {key} has no glyph for {''.join(missing)!r}; it will fall back", file=sys.stderr)
    txt, out = _TMP / f"{key}.txt", _TMP / f"{key}.woff"
    txt.write_text(chars, encoding="utf-8")
    subprocess.run([sys.executable, "-m", "fontTools.subset", str(path), f"--text-file={txt}",
                    "--flavor=woff", "--no-hinting", "--desubroutinize", "--drop-tables+=meta,DSIG", f"--output-file={out}"], check=True)
    return "data:font/woff;base64," + base64.b64encode(out.read_bytes()).decode()

# --------------------------------------------------------------------------- svg helpers

MOTION_CSS = """
.motion{display:none}
@media (prefers-reduced-motion:no-preference){
.rise{animation:rise .75s cubic-bezier(.2,.7,.2,1) both}
.fade{animation:fade .8s ease both}
.slide{animation:slide .8s cubic-bezier(.2,.7,.2,1) both}
.grow{animation:grow .9s cubic-bezier(.2,.8,.2,1) both;transform-box:fill-box;transform-origin:50% 100%}
.widen{animation:widen 1.1s cubic-bezier(.3,.7,.2,1) both;transform-box:fill-box;transform-origin:0 50%}
.pop{animation:pop .45s cubic-bezier(.3,1.5,.5,1) both;transform-box:fill-box;transform-origin:50% 50%}
.zoom{animation:zoom 1s cubic-bezier(.2,.8,.2,1) both;transform-box:fill-box;transform-origin:50% 50%}
.draw{stroke-dasharray:1;animation:draw .9s cubic-bezier(.6,0,.3,1) both}
.motion{display:inline}
.typed{clip-path:url(#typed)}
@keyframes rise{from{opacity:0;transform:translateY(12px)}}
@keyframes fade{from{opacity:0}}
@keyframes slide{from{opacity:0;transform:translateX(-18px)}}
@keyframes grow{from{transform:scaleY(0)}}
@keyframes widen{from{transform:scaleX(0)}}
@keyframes pop{from{opacity:0;transform:scale(.3)}}
@keyframes zoom{from{opacity:0;transform:scale(.6)}}
@keyframes draw{from{stroke-dashoffset:1}}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important}.motion,.typed{display:none!important}}
"""


def anim(kind: str | None, delay: float = 0, cls: str = "") -> str:
    """class/style attributes for an animated element; plain final frame in STATIC mode."""
    classes = " ".join(c for c in (cls, None if STATIC else kind) if c)
    out = f' class="{classes}"' if classes else ""
    if kind and not STATIC:
        out += f' style="animation-delay:{delay:.2f}s"'
    return out


def fmt_date(d: dt.date, year: bool = True) -> str:
    return f"{d.day} {d:%b}" + (f" {d:%Y}" if year else "")


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


class Card:
    def __init__(self, name: str, w: int, h: int, th: dict, alt: str):
        self.name, self.w, self.h, self.th, self.alt = name, w, h, th, alt
        self.body: list[str] = []
        self.defs: list[str] = []
        self.glyphs: dict[str, set[str]] = defaultdict(set)

    # -- text
    def span(self, s: str, key: str, fill: str | None = None, size: float | None = None, extra: str = "") -> str:
        self.glyphs[key].update(s)
        attrs = f' class="{FONTS[key][2]}"' + (f' fill="{self.th.get(fill, fill)}"' if fill else "")
        attrs += f' font-size="{size}"' if size else ""
        return f"<tspan{attrs}{extra}>{escape(s)}</tspan>"

    def text(self, x: float, y: float, s: str | list, key: str = "sans", size: float = 14, fill: str = "ink",
             anchor: str | None = None, a: str = "", tracking: float | None = None) -> str:
        """`s` is a string or a list of (text, font-key, fill[, size]) runs."""
        runs = [(s, key, fill)] if isinstance(s, str) else s
        inner = "".join(self.span(*run) for run in runs)
        attrs = f'x="{x:g}" y="{y:g}" font-size="{size}" fill="{self.th[fill]}" xml:space="preserve"'
        attrs += f' text-anchor="{anchor}"' if anchor else ""
        attrs += f' letter-spacing="{tracking:g}"' if tracking else ""
        return f"<text {attrs}{a}>{inner}</text>"

    def wrap(self, s: str, key: str, size: float, width: float, max_lines: int) -> list[str]:
        lines, cur = [], ""
        for word in s.split():
            trial = f"{cur} {word}".strip()
            if measure(trial, key, size) <= width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            while measure(lines[-1] + "…", key, size) > width and " " in lines[-1]:
                lines[-1] = lines[-1].rsplit(" ", 1)[0]
            lines[-1] = lines[-1].rstrip(",.;:") + "…"
        return lines

    # -- pieces
    def frame(self, r: int = 16) -> None:
        self.defs.append(f'<clipPath id="frame"><rect width="{self.w}" height="{self.h}" rx="{r}"/></clipPath>')
        self.body.append(f'<rect x=".5" y=".5" width="{self.w - 1}" height="{self.h - 1}" rx="{r}" '
                         f'fill="{self.th["card"]}" stroke="{self.th["border"]}"/>')

    def arrow(self, x: float, y: float, delay: float, size: float = 9) -> str:
        """↗ drawn as a path (no glyph dependency)."""
        s = size
        return (f'<path d="M{x} {y + s}L{x + s} {y}M{x + 2} {y}H{x + s}V{y + s - 2}" pathLength="1" fill="none" '
                f'stroke="{self.th["brand"]}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"'
                f'{anim("draw", delay)}/>')

    def typewriter(self, x: float, y: float, runs: list, key: str, size: float, start: float,
                   cps: float = 38) -> str:
        """SMIL typewriter: a clip rect steps through each character's measured width.
        The text is drawn twice: plain (shown with reduced motion / STATIC) and clipped
        (shown only when motion is allowed). Width 0 until typing starts, then frozen open."""
        full = "".join(t for t, *_ in runs)
        plain = self.text(x, y, [(t, key, f) for t, f in runs], key, size)
        if STATIC:
            return plain
        widths = [measure(full[:i], key, size) for i in range(len(full) + 1)]
        dur = len(full) / cps
        vals = ";".join(f"{w:.1f}" for w in widths)
        self.defs.append(
            f'<clipPath id="typed"><rect x="{x - 2}" y="{y - size - 2}" width="0" height="{size + 8}">'
            f'<animate attributeName="width" values="{vals}" calcMode="discrete" begin="{start:.2f}s" '
            f'dur="{dur:.2f}s" fill="freeze"/></rect></clipPath>')
        cursor = (f'<rect class="motion" x="{x}" y="{y - size + 1}" width="{size * .55:.1f}" height="{size + 2}" '
                  f'fill="{self.th["amber"]}" opacity="0">'
                  f'<animate attributeName="x" values="{";".join(f"{x + w + 1:.1f}" for w in widths)}" '
                  f'calcMode="discrete" begin="{start:.2f}s" dur="{dur:.2f}s" fill="freeze"/>'
                  f'<animate attributeName="opacity" values="1;0" calcMode="discrete" dur="1s" '
                  f'begin="{start:.2f}s" repeatCount="indefinite"/></rect>')
        # Without motion the plain copy shows; with motion the clipped copy replaces it.
        return (f'<g class="still">{plain}</g><g class="typed motion">{plain}</g>{cursor}')

    # -- output
    def markup(self) -> str:
        return f'<defs>{"".join(self.defs)}</defs><g clip-path="url(#frame)">{"".join(self.body)}</g>'

    def svg(self, markup: str | None = None, theme_css: str = "") -> str:
        faces, classes = [], [theme_css] if theme_css else []
        for key in sorted(self.glyphs):
            chars = "".join(sorted(self.glyphs[key]))
            fam = f"pc-{key}"
            faces.append(f"@font-face{{font-family:{fam};src:url({woff_data_uri(key, chars)}) format('woff')}}")
            classes.append(f".{FONTS[key][2]}{{font-family:{fam},{FONTS[key][3]}}}")
        css = "\n".join(faces + classes)
        if not STATIC:
            # The plain typewriter copy hides only when motion is on (the clipped copy takes over).
            css += MOTION_CSS + "@media (prefers-reduced-motion:no-preference){.still{display:none}}"
        title = escape(self.alt)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-labelledby="t" fill="none">'
                f'<title id="t">{title}</title><style>{css}</style>{markup or self.markup()}</svg>\n')


THEMED_PROPS = {"fill", "stroke", "stop-color", "fill-opacity", "stop-opacity"}
_TAG = re.compile(r"(<[^>]+>)")
_ATTR = re.compile(r'\s([a-z:-]+)="([^"]*)"')


def adaptive_svg(light: Card, dark: Card) -> str:
    """One SVG that follows the viewer's OS/browser theme.

    Both renders are structurally identical; only colours differ. Every themed attribute
    that differs is replaced by a class whose value flips under prefers-color-scheme: dark.
    """
    lt, dk = _TAG.split(light.markup()), _TAG.split(dark.markup())
    if len(lt) != len(dk):
        raise ValueError(f"{light.name}: light and dark renders differ in structure")
    rules: dict[tuple[str, str, str], str] = {}
    out = []
    for a, b in zip(lt, dk):
        if a == b:
            out.append(a)
            continue
        if not a.startswith("<"):
            raise ValueError(f"{light.name}: text differs between themes: {a!r} / {b!r}")
        la, da = dict(_ATTR.findall(a)), dict(_ATTR.findall(b))
        names = []
        for prop, lv in la.items():
            if da.get(prop) == lv:
                continue
            if prop not in THEMED_PROPS:
                raise ValueError(f"{light.name}: non-colour attribute {prop!r} differs between themes")
            names.append(rules.setdefault((prop, lv, da[prop]), f"k{len(rules)}"))
            a = a.replace(f' {prop}="{lv}"', "", 1)
        cls = " ".join(names)
        a = (a.replace(' class="', f' class="{cls} ', 1) if ' class="' in a
             else re.sub(r"^<([\w:-]+)", lambda m: f'<{m.group(1)} class="{cls}"', a))
        out.append(a)
    css = "".join(f".{n}{{{p}:{lv}}}" for (p, lv, _), n in rules.items())
    css += "@media (prefers-color-scheme:dark){" + "".join(f".{n}{{{p}:{dv}}}" for (p, _, dv), n in rules.items()) + "}"
    light.glyphs.update({k: light.glyphs[k] | dark.glyphs[k] for k in dark.glyphs})
    return light.svg("".join(out), css)

# --------------------------------------------------------------------------- data


def load_data() -> dict:
    raw = json.loads(DATA.read_text())
    days = [(dt.date.fromisoformat(d["date"]), d["count"], d["level"]) for d in raw["days"]]
    days.sort()
    counts = [c for _, c, _ in days]

    # current streak: today may still be empty when the Action runs, so it doesn't break the streak
    i = len(days) - 1
    if i >= 0 and counts[i] == 0:
        i -= 1
    current = 0
    while i >= 0 and counts[i] > 0:
        current, i = current + 1, i - 1
    longest = run = 0
    for c in counts:
        run = run + 1 if c else 0
        longest = max(longest, run)

    busiest = max(days, key=lambda d: (d[1], d[0])) if days and max(counts) > 0 else None

    last = days[-1][0]
    months = []
    y, m = last.year, last.month
    for _ in range(12):
        months.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()
    per_month = defaultdict(int)
    for d, c, _ in days:
        per_month[(d.year, d.month)] += c

    return {
        "raw": raw, "days": days, "total": raw.get("total", sum(counts)),
        "current": current, "longest": longest, "active": sum(1 for c in counts if c), "busiest": busiest,
        "months": [(dt.date(y, m, 1), per_month[(y, m)]) for y, m in months],
        "repos": {r["name"]: r for r in raw.get("repos", [])},
        "updated": dt.datetime.fromisoformat(raw["generated_at"]).date() if raw.get("generated_at") else last,
    }

# --------------------------------------------------------------------------- cards


def hero(th: dict) -> Card:
    p = PROFILE
    portrait = p["portrait"] if p["portrait"] and Path(p["portrait"]).exists() else None
    c = Card("hero", 840, 290, th, f"{p['name']} {p['line']}")
    c.frame()
    c.defs.append(f'<radialGradient id="glow" cx="50%" cy="0" r="75%" gradientTransform="scale(1 .9)">'
                  f'<stop offset="0" stop-color="{th["amber"]}" stop-opacity="{th["glow"]}"/>'
                  f'<stop offset="1" stop-color="{th["amber"]}" stop-opacity="0"/></radialGradient>')
    b, t = c.body, T_HERO
    b.append(f'<rect width="840" height="290" fill="url(#glow)"{anim("fade", t)}/>')

    # logo + status pill
    logo_w = measure("".join(p["logo"]), "mono", 14)
    b.append(c.text(40, 52, [(p["logo"][0], "mono", "ink"), (p["logo"][1], "mono", "brand")], "mono", 14,
                    a=anim("rise", t + .05)))
    if p["badge"]:
        px, pw = 40 + logo_w + 18, measure(p["badge"], "mono", 11) + 34
        dot = (px + 13, 47)
        b.append(f'<g{anim("fade", t + .35)}>'
                 f'<rect x="{px}" y="35" width="{pw:.1f}" height="24" rx="12" fill="{th["paper"]}" stroke="{th["subtle"]}"/>'
                 f'<circle cx="{dot[0]}" cy="{dot[1]}" r="3.5" fill="{th["mint"]}"/>'
                 f'<circle class="motion" cx="{dot[0]}" cy="{dot[1]}" r="3.5" fill="{th["mint"]}">'
                 f'<animate attributeName="r" values="3.5;9" dur="2.2s" begin="{t + 1.2:.2f}s" repeatCount="indefinite"/>'
                 f'<animate attributeName="opacity" values=".55;0" dur="2.2s" begin="{t + 1.2:.2f}s" repeatCount="indefinite"/>'
                 f'</circle>{c.text(px + 24, 51, p["badge"], "mono", 11, "muted")}</g>')

    # portrait (behind the text column's right edge)
    col_w = 560 if portrait else 760
    if portrait:
        mime = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}[portrait.suffix.lower()]
        data = base64.b64encode(portrait.read_bytes()).decode()
        b.append(f'<circle cx="730" cy="150" r="108" fill="{th["amber"]}" fill-opacity="{th["halo"]}"{anim("zoom", t + .2)}/>')
        b.append(f'<g{anim("rise", t + .3)}><image href="data:{mime};base64,{data}" x="640" y="22" width="180" '
                 f'height="320" preserveAspectRatio="xMidYMin meet"/></g>')
        if p["portrait_note"]:
            b.append(f'<g transform="rotate(-4 632 262)">{c.text(632, 262, p["portrait_note"], "hand", 21, "brand", "end", anim("fade", t + 1.5))}</g>')
            b.append(f'<path d="M638 254C652 250 661 240 664 224M657 230L664 222L669 231" pathLength="1" '
                     f'stroke="{th["brand"]}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"'
                     f'{anim("draw", t + 1.7)}/>')

    # greeting, name, line, terminal
    b.append(f'<g transform="rotate(-3 40 108)">{c.text(40, 108, p["greeting"], "hand", 31, "brand", a=anim("rise", t + .15))}</g>')
    b.append(c.text(40, 160, p["name"], "bold", 44, "ink", tracking=-1.1, a=anim("rise", t + .25)))
    for i, line in enumerate(c.wrap(p["line"], "sans", 17, col_w, 2)):
        b.append(c.text(40, 198 + i * 24, line, "sans", 17, "muted", a=anim("rise", t + .38 + i * .06)))
    b.append(c.typewriter(40, 252, p["terminal"], "mono", 12.5, start=t + .9))
    return c


def tile(th: dict, i: int, spec: dict) -> Card:
    c = Card(f"tile-{spec['slug']}", 272, 168, th, f"{spec['title']} {spec['text']} ({spec['url']})")
    c.frame(14)
    t = T_TILES + i * .12
    title_w = measure(spec["title"], "hand", 30)
    lines = c.wrap(spec["text"], "sans", 13, 232, 2)
    c.body += [
        f'<g{anim("rise", t)}>'
        + c.text(20, 32, spec["label"], "mono", 11, "muted")
        + c.text(20, 76, spec["title"], "hand", 30, "ink")
        + "".join(c.text(20, 108 + j * 19, ln, "sans", 13, "muted") for j, ln in enumerate(lines))
        + c.text(20, 150, spec["url"], "mono", 11, "brand")
        + "</g>",
        c.arrow(243, 23, t + .35),
        f'<path d="M20 85q{title_w / 4:.1f} -4 {title_w / 2:.1f} -1t{title_w / 2:.1f} 0" pathLength="1" '
        f'stroke="{th["amber"]}" stroke-width="2.4" stroke-linecap="round"{anim("draw", t + .4)}/>',
    ]
    return c


def nice_scale(v: int) -> tuple[int, int]:
    if v <= 4:
        return 4, 1
    raw = v / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 5, 10) if s * mag >= raw)
    step = max(1, int(step))
    return step * math.ceil(v / step), step


def chart(th: dict, d: dict) -> Card:
    c = Card("activity", 840, 350, th,
             f"Contributions per month for the last 12 months: {plural(d['total'], 'contribution')}, "
             f"current streak {plural(d['current'], 'day')}, {d['active']} active days.")
    c.frame()
    b, t = c.body, T_CHART
    first, last = d["months"][0][0], d["months"][-1][0]
    in_range = sum(v for _, v in d["months"])
    b.append(c.text(32, 54, "A year of commits.", "hand", 31, "ink", a=anim("rise", t)))
    b.append(c.text(808, 50, f"{first:%b %Y} – {last:%b %Y}  ·  {plural(in_range, 'contribution')}",
                    "mono", 11, "muted", "end", anim("fade", t + .1)))

    # stats
    bz = d["busiest"]
    stats = [
        ("current streak", str(d["current"]), " day" if d["current"] == 1 else " days",
         f"longest: {plural(d['longest'], 'day')}"),
        ("active days", str(d["active"]), f" / {len(d['days'])}",
         f"{d['active'] / max(1, len(d['days'])):.0%} of the year"),
        ("busiest day", fmt_date(bz[0]) if bz else "—", "",
         f"{plural(bz[1], 'contribution')} · {bz[0]:%A}" if bz else "no contributions yet"),
    ]
    for i, (label, value, unit, sub) in enumerate(stats):
        x = 32 + i * 264
        if i:
            b.append(f'<line x1="{x - 22}" y1="80" x2="{x - 22}" y2="146" stroke="{th["subtle"]}"{anim("fade", t + .2)}/>')
        b.append(f'<g{anim("rise", t + .15 + i * .08)}>'
                 + c.text(x, 92, label, "mono", 11, "muted")
                 + c.text(x, 126, [(value, "bold", "ink"), (unit, "sans", "muted", 16)], "bold", 30, tracking=-.6)
                 + c.text(x, 146, sub, "mono", 11, "brand")
                 + "</g>")

    # bars
    left, right, top, base = 64, 808, 182, 306
    vmax = max(v for _, v in d["months"])
    scale_max, step = nice_scale(vmax)
    for v in range(step, scale_max + 1, step):
        y = base - (base - top) * v / scale_max
        b.append(f'<g{anim("fade", t + .25)}><line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" '
                 f'stroke="{th["subtle"]}" stroke-dasharray="3 4"/>'
                 + c.text(left - 12, y + 4, str(v), "mono", 10, "muted", "end") + "</g>")
    b.append(f'<line x1="{left}" y1="{base}" x2="{right}" y2="{base}" stroke="{th["border"]}"{anim("fade", t + .25)}/>')

    slot = (right - left) / 12
    bw = 30
    peak = max(range(12), key=lambda k: (d["months"][k][1], k))
    for k, (month, v) in enumerate(d["months"]):
        cx = left + slot * (k + .5)
        h = max(3.0, (base - top) * v / scale_max)
        fill = th["amber"] if (k == peak and v) else (mix(th["card"], th["amber"], .45) if v else th["empty"])
        delay = t + .35 + k * .06
        b.append(f'<rect x="{cx - bw / 2:.1f}" y="{base - h:.1f}" width="{bw}" height="{h:.1f}" rx="5" '
                 f'fill="{fill}"{anim("grow", delay)}/>')
        if v:
            b.append(c.text(cx, base - h - 8, str(v), "mono", 11, "brand" if k == peak else "muted", "middle",
                            anim("fade", delay + .5)))
        b.append(c.text(cx, base + 20, f"{month:%b}", "mono", 11, "ink" if k == 11 else "muted", "middle",
                        anim("fade", t + .3)))
        if k == 0 or month.month == 1:
            b.append(c.text(cx, base + 35, f"{month:%Y}", "mono", 9, "muted", "middle", anim("fade", t + .3)))
    b.append(c.text(808, base + 35, "this month, so far", "hand", 15, "brand", "end", anim("fade", t + 1.3)))
    return c


def repo_card(th: dict, i: int, r: dict, login: str) -> Card:
    c = Card(f"repo-{r['name']}", 840, 150, th, f"{r['name']}: {r['description'] or 'repository'}")
    c.frame()
    b, t = c.body, T_REPOS + i * T_REPO_STEP
    lang = r.get("language")
    lcolor = r.get("language_color") or LANG_COLORS.get(lang or "", th["muted"])
    lines = c.wrap(r["description"] or "No description yet.", "sans", 14, 540, 2)
    b.append(f'<g{anim("slide", t)}>'
             + c.text(32, 44, [(f"{i + 1:02d}", "mono", "brand"), (" · featured repo", "mono", "muted")], "mono", 11)
             + c.text(32, 78, [(f"{login} / ", "sans", "muted"), (r["name"], "bold", "ink")], "bold", 23, tracking=-.4)
             + "".join(c.text(32, 106 + j * 21, ln, "sans", 14, "muted") for j, ln in enumerate(lines))
             + "</g>")
    b.append(c.arrow(799, 32, t + .3))

    # meta column
    mx = 640
    pushed = dt.datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00")).date()
    star = " ".join(f"{mx + 6 + (6 if k % 2 == 0 else 2.6) * math.sin(k * math.pi / 5):.1f},"
                    f"{97 - (6 if k % 2 == 0 else 2.6) * math.cos(k * math.pi / 5):.1f}" for k in range(10))
    ink = th["muted"]
    b.append(f'<g{anim("fade", t + .25)}>'
             + f'<circle cx="{mx + 6}" cy="68" r="5" fill="{lcolor}"/>'
             + c.text(mx + 20, 72, lang or "—", "mono", 12, "ink")
             + f'<polygon points="{star}" stroke="{ink}" stroke-width="1.3" stroke-linejoin="round"/>'
             + c.text(mx + 20, 101, plural(r["stars"], "star"), "mono", 12, "muted")
             + f'<g stroke="{ink}" stroke-width="1.3" stroke-linecap="round" transform="translate({mx + 89} 90)">'
               f'<circle cx="2" cy="2" r="1.8"/><circle cx="10" cy="2" r="1.8"/><circle cx="6" cy="12" r="1.8"/>'
               f'<path d="M2 4v1.5q0 2 2 2h4q2 0 2-2V4M6 7.5v2.7"/></g>'
             + c.text(mx + 106, 101, plural(r["forks"], "fork"), "mono", 12, "muted")
             + c.text(mx, 128, f"updated {pushed:%b %Y}", "mono", 12, "muted")
             + "</g>")
    b.append(f'<rect x="0" y="146" width="840" height="4" fill="{lcolor}"{anim("widen", t + .15)}/>')
    return c


def graph(th: dict, d: dict) -> Card:
    days = d["days"]
    c = Card("contributions", 840, 236, th,
             f"Contribution graph: {plural(d['total'], 'contribution')} in the last year.")
    c.frame()
    b, t = c.body, T_GRAPH
    colors = ramp(th)
    b.append(c.text(32, 52, "Every square, a day.", "hand", 31, "ink", a=anim("rise", t)))
    b.append(c.text(808, 48, f"{plural(d['total'], 'contribution')} in the last year", "mono", 11, "muted", "end",
                    anim("fade", t + .1)))

    x0, y0, cell, pitch = 64, 90, 11, 14
    start = days[0][0] - dt.timedelta(days=(days[0][0].weekday() + 1) % 7)  # Sunday on/before first day
    last_month, last_col = None, -9
    cells = []
    bz = d["busiest"]
    for date, count, level in days:
        col, row = (date - start).days // 7, (date.weekday() + 1) % 7
        x, y = x0 + col * pitch, y0 + row * pitch
        cells.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2.5" fill="{colors[level]}"'
                     f'{anim("pop", t + .2 + col * .02 + row * .025)}/>')
        if date.day <= 7 and row == 0 or date == days[0][0]:
            if date.month != last_month and col - last_col >= 3:
                b.append(c.text(x, y0 - 10, f"{date:%b}", "mono", 10, "muted", a=anim("fade", t + .15 + col * .02)))
                last_month, last_col = date.month, col
        if bz and date == bz[0] and not STATIC:
            cx, cy = x + cell / 2, y + cell / 2
            cells.append(f'<circle class="motion" cx="{cx}" cy="{cy}" r="6" stroke="{th["amber"]}" stroke-width="1.5" opacity="0">'
                         f'<animate attributeName="r" values="6;16" dur="1.4s" begin="{t + 2.0:.2f}s" repeatCount="3"/>'
                         f'<animate attributeName="opacity" values=".9;0" dur="1.4s" begin="{t + 2.0:.2f}s" repeatCount="3"/>'
                         f'</circle>')
    b += cells
    for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        b.append(c.text(32, y0 + row * pitch + 9, name, "mono", 10, "muted", a=anim("fade", t + .15)))

    # footer: updated date + legend
    fy = 214
    b.append(c.text(32, fy, f"updated {fmt_date(d['updated'])}", "mono", 10, "muted", a=anim("fade", t + .6)))
    lx = 808 - 30 - 5 * pitch
    b.append(f'<g{anim("fade", t + .6)}>' + c.text(lx - 8, fy, "less", "mono", 10, "muted", "end")
             + "".join(f'<rect x="{lx + k * pitch}" y="{fy - 9}" width="{cell}" height="{cell}" rx="2.5" fill="{col}"/>'
                       for k, col in enumerate(colors))
             + c.text(808, fy, "more", "mono", 10, "muted", "end") + "</g>")
    return c

# --------------------------------------------------------------------------- main


PREVIEW_HTML = """<!doctype html><meta charset="utf-8"><title>profile cards preview</title>
<!-- Cards follow your OS/browser theme: switch it to screenshot the other one. -->
<style>body{{margin:0;padding:32px;background:#fff}} @media (prefers-color-scheme:dark){{body{{background:#0d1117}}}}
main{{width:840px}} img{{display:block;width:100%;margin:0 0 16px}}
.row{{display:flex;justify-content:space-between}} .row img{{width:32.5%}}</style>
<main>{stack}</main>"""


def main() -> None:
    d = load_data()
    repos = []
    for name in FEATURED:
        if name in d["repos"]:
            repos.append(d["repos"][name])
        else:
            print(f"  ! featured repo {name!r} not found in {DATA.name}; skipped", file=sys.stderr)
    login = d["raw"].get("login", "")

    def build(th: dict) -> list[Card]:
        return [hero(th), *(tile(th, i, s) for i, s in enumerate(TILES)), chart(th, d),
                *(repo_card(th, i, r, login) for i, r in enumerate(repos)), graph(th, d)]

    if OUT.exists():  # generated output only; cleared so dropped repos don't linger
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    names = []
    for light, dark in zip(build(THEMES["light"]), build(THEMES["dark"])):
        (OUT / f"{light.name}.svg").write_text(adaptive_svg(light, dark), encoding="utf-8")
        names.append(light.name)
    size = sum((OUT / f"{n}.svg").stat().st_size for n in names)
    print(f"{len(names)} cards -> {OUT.relative_to(ROOT) if OUT.is_relative_to(ROOT) else OUT} ({size / 1024:.0f} KB)")
    if STATIC:
        img = lambda n: f'<img src="{n}.svg">'  # noqa: E731
        stack = (img("hero") + '<div class="row">' + "".join(img(f"tile-{s['slug']}") for s in TILES) + "</div>"
                 + "".join(img(n) for n in names if not n.startswith(("hero", "tile"))))
        (OUT / "index.html").write_text(PREVIEW_HTML.format(stack=stack), encoding="utf-8")
        print(f"final-frame preview: {OUT / 'index.html'}")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
