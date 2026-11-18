"""The dashboard's colours, measured the way WCAG 2.2 measures them (success
criterion 1.4.3, contrast minimum: 4.5:1 for text under 18 pt, with the
relative luminance and ratio of its definitions).

A measure of 15 Nov 2026 took every screen at 1280 and 390 px
in both themes: in the dark theme the white text of the blue buttons read at
3.6:1 and the red of every error note at 3.3-3.7:1, and the Usage page's
warning, amber-500 on white, at 2.1:1. The pairs below are the ones every
page draws from the theme's tokens (dashboard/src/index.css), measured on
the tokens themselves, so a token changed later is measured again.
"""
import colorsys
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dashboard" / "src"
CSS = SRC / "index.css"
AA = 4.5


def _tokens(block: str) -> dict[str, tuple[float, float, float]]:
    """`--name: H S% L%;` of one block of index.css, as sRGB in 0..1."""
    out = {}
    for name, h, s, l in re.findall(r"--([\w-]+):\s*([\d.]+)\s+([\d.]+)%\s+([\d.]+)%;", block):
        out[name] = colorsys.hls_to_rgb(float(h) / 360, float(l) / 100, float(s) / 100)
    return out


def _themes() -> dict[str, dict]:
    css = CSS.read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    light = re.search(r"^:root\s*\{(.*?)^\}", css, flags=re.S | re.M).group(1)
    dark = re.search(r"^\.dark\s*\{(.*?)^\}", css, flags=re.S | re.M).group(1)
    return {"light": _tokens(light), "dark": _tokens(dark)}


def _luminance(c) -> float:
    def lin(v):
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a, b) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _over(fg, alpha: float, bg):
    """`bg-x/10`: the colour at that opacity over what is under it."""
    return tuple(alpha * f + (1 - alpha) * b for f, b in zip(fg, bg))


def test_the_contrast_is_measured_as_wcag_defines_it():
    assert round(_ratio((1, 1, 1), (0, 0, 0)), 2) == 21.0
    # The dark theme's blue under white, as the check measured it.
    blue = colorsys.hls_to_rgb(217 / 360, 0.60, 0.91)
    assert round(_ratio((1, 1, 1), blue), 1) == 3.6


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_filled_button_reads(theme):
    """The filled buttons of every page (Create key, New project, Search)
    and the red of a confirmed deletion."""
    t = _themes()[theme]
    assert _ratio(t["primary-foreground"], t["primary"]) >= AA, theme
    assert _ratio(t["destructive-foreground"], t["destructive"]) >= AA, theme


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("under", ["background", "card"])
def test_an_error_reads_on_its_note_and_on_the_page(theme, under):
    """Every error note is `text-destructive` on `bg-destructive/10`
    (components/LoadState.tsx, ErrorNote), on the page or in a card."""
    t = _themes()[theme]
    red, page = t["destructive"], t[under]
    assert _ratio(red, page) >= AA, (theme, under)
    assert _ratio(red, _over(red, 0.10, page)) >= AA, (theme, under)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_link_in_the_blue_reads_on_the_page(theme):
    t = _themes()[theme]
    assert _ratio(t["primary"], t["background"]) >= AA, theme
    assert _ratio(t["primary"], t["card"]) >= AA, theme


def _code(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def test_a_text_in_a_palette_colour_reads_in_both_themes():
    """A status written in a colour of Tailwind's palette, not in a token:
    a 700 shade in the light theme and a 400 shade beside it, `dark:`, in
    the dark one. Measured on Tailwind 4.3's palette (oklch, converted to
    sRGB) with the formula above, on white, on the dark card and on the
    colour's own /10 tint: every hue at 700 reads 4.5:1 or more on white
    and on its tint (green-700 on its tint the lowest, 4.52:1), every hue at
    400 5.0:1 or more on the dark page, the card and the tint (indigo-400,
    5.09:1). The check of 15 Nov 2026 found what the shades under them read:
    amber-500 on white 2.1:1 (Usage's "No rate for ..."), emerald-500 2.5:1
    (the confirmations of Webhooks, Settings, Facts and the graph), red-500
    3.8:1 (Home), red-600 on its tint 4.1:1 (the Audit log's error badge);
    and a light shade alone is a dark text on the dark page."""
    offenders = []
    for p in sorted(SRC.rglob("*.tsx")):
        for n, line in enumerate(_code(p).splitlines(), 1):
            where = f"{p.relative_to(SRC)}:{n}"
            light = re.findall(r"(?<![\w:-])text-([a-z]+)-(\d{2,3})\b", line)
            dark = dict(re.findall(r"(?<![\w-])dark:text-([a-z]+)-(\d{2,3})\b", line))
            for hue, shade in light:
                if int(shade) < 700:
                    offenders.append(f"{where}: text-{hue}-{shade} is lighter than 700")
                if hue not in dark:
                    offenders.append(f"{where}: text-{hue}-{shade} has no dark:text-{hue}- beside it")
            for hue, shade in dark.items():
                if int(shade) > 400:
                    offenders.append(f"{where}: dark:text-{hue}-{shade} is darker than 400")
    assert offenders == [], "\n".join(offenders)


# ── the pages before the dashboard: sign-in, setup, an app's consent ────────

# They draw on one dark page of their own, in colours written by value, not
# in the theme's tokens: what the check of 15 Nov 2026 measured there was
# white on their blue button at 3.6:1 and their grey (#6B7280) at 3.7-4.0:1.
_BEFORE = ("pages/LoginPage.tsx", "pages/SetupPage.tsx", "pages/OAuthConsentPage.tsx")
_HEX = r"#([0-9A-Fa-f]{6})"


def _rgb(h: str):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


@pytest.mark.parametrize("page", _BEFORE)
def test_the_pages_before_the_dashboard_read(page):
    """Every text colour of the page on every surface it draws: a text on a
    line with a filled background (a button, in each of its states) on that
    background; any other text on the page and on each tint the page lays
    over it (`bg-white/4`, `bg-[#3B82F6]/6`)."""
    code = _code(SRC / page)
    # The page's own ground: the background of its full-height element.
    grounds = set(re.findall(rf"min-h-screen[^\"]*?bg-\[{_HEX}\]", code))
    assert len(grounds) == 1, (page, grounds)
    ground = _rgb(grounds.pop())
    tints = [ground]
    for tint, alpha in re.findall(rf"bg-\[{_HEX}\]/(\d+)", code):
        tints.append(_over(_rgb(tint), int(alpha) / 100, ground))
    for alpha in re.findall(r"bg-white/(\d+)", code):
        tints.append(_over((1, 1, 1), int(alpha) / 100, ground))
    offenders, measured = [], 0
    for n, line in enumerate(code.splitlines(), 1):
        texts = re.findall(rf"text-\[{_HEX}\]", line) + (["FFFFFF"] if re.search(r"(?<![\w-])text-white\b", line) else [])
        fills = [f for f in re.findall(rf"(?<![\w/-])(?:[\w-]+:)*bg-\[{_HEX}\](?!/)", line) if _rgb(f) != ground]
        for t in texts:
            for under in ([_rgb(f) for f in fills] or tints):
                measured += 1
                r = _ratio(_rgb(t), under)
                if r < AA:
                    offenders.append(f"{page}:{n}: #{t} at {r:.2f}:1")
    assert measured > 0, page
    assert offenders == [], "\n".join(offenders)
