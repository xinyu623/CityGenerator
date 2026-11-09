from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app" / "static" / "css" / "app.css").read_text(encoding="utf-8")

TOKENS = {
    "--bg": "#ece9e2",
    "--panel": "#f6f4ef",
    "--panel-2": "#fbfaf6",
    "--paper": "#f2efe9",
    "--ink": "#211d19",
    "--ink-soft": "#3b3631",
    "--muted": "#6e675e",
    "--line": "#dbd6cb",
    "--line-strong": "#c2bcaf",
    "--accent": "#a7472c",
}

LEGACY = ["#e9eee5", "#fcfaf3", "#f8f6ed", "#f7f2e4", "#e2e9df", "#fdfbf5", "#fffdf7",
          "#faf8f0", "#f8f7ef", "#fbf9f1", "#fbf7ed", "#fdf0dc", "#eef0e6", "#f3e1d6",
          "#e8e6da", "#e8e5d8", "#e8e3d5", "#e3cbbb", "#e2ddcf", "#ded8c8", "#d8d1bf",
          "#f1c7c1", "#fff5f4", "#fff9ef", "#f0d9b3", "#f7f0e4", "#f3f0e5", "#f5e3d8",
          "#dfc1b1", "#f7f3e8", "#b9a488", "#a08c72", "#f3e4d5", "#fff8ee", "#fff9f0",
          "#b9c0b0", "#bcc4b5", "#a9bbc9", "#668790", "#1f261f", "#394136", "#71786d",
          "#dfe2d6", "#cbd1c3", "#7d3a27", "#7b3522", "#8a5a14", "#a02c1f", "#75430a",
          "#5d6256", "#5e6256", "#6e7266", "#4f554a", "#8b8e80", "#7d2f1d", "#c56a4d",
          "#bc5738", "#c96443", "#b14e31", "#8e3822", "#85331f", "#d58a68", "#e3e8de"]


def test_root_declares_the_graphite_tokens():
    for name, value in TOKENS.items():
        assert re.search(rf"{name}:\s*{value}", CSS), f"{name} is not set to {value}"


def test_no_legacy_cream_or_sage_literals_survive():
    found = sorted({h for h in LEGACY if h in CSS.lower()})
    assert found == [], f"legacy colours survive: {found}"


def _blocks(selector: str) -> list[str]:
    return [m.group(0) for m in re.finditer(rf"{re.escape(selector)}[^{{}}]*\{{[^{{}}]*\}}", CSS)]


def test_warm_accent_is_confined_to_the_primary_action():
    allowed = {
        block.strip().lower()
        for selector in (":root", ".button.primary", ".progress-bar")
        for block in _blocks(selector)
    }
    for hex_value in ("#a7472c", "#8f3b24"):
        for match in re.finditer(hex_value, CSS.lower()):
            assert css_block_at(match.start()).lower() in allowed, f"{hex_value} appears in a disallowed rule"
    assert "accent-color: var(--accent)" not in CSS


def css_block_at(index: int) -> str:
    start = CSS.rfind("}", 0, index) + 1
    end = CSS.find("}", index) + 1
    return CSS[start:end].strip()


def _lum(hex_value: str) -> float:
    r, g, b = (int(hex_value[i:i + 2], 16) / 255 for i in (1, 3, 5))
    channel = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_text_contrast_on_paper_panels():
    assert _ratio(TOKENS["--ink"], TOKENS["--panel"]) >= 10.0
    assert _ratio(TOKENS["--muted"], TOKENS["--panel"]) >= 4.5


def test_map_tiles_are_desaturated_and_the_backdrop_is_paper():
    assert "--map-filter: saturate(0.35)" in CSS
    assert re.search(r"\.leaflet-container\s*\{[^}]*background:\s*var\(--bg\)", CSS)


def test_disabled_primary_button_drops_the_warm_fill():
    block = re.search(r"\.button\.primary:disabled\s*\{[^}]*\}", CSS)
    assert block is not None, "缺少禁用态主按钮规则"
    rule = block.group(0)
    assert "var(--panel-2)" in rule
    assert "var(--accent)" not in rule


MAP = (ROOT / "app" / "static" / "js" / "map.js").read_text(encoding="utf-8")


def test_selection_outline_is_graphite_not_slate():
    for old in ("#547883", "#7fa1aa"):
        assert old not in MAP.lower()
    assert MAP.count('"#544d45"') == 3


def test_selection_outline_contrasts_with_the_paper_backdrop():
    assert _ratio("#544d45", TOKENS["--bg"]) >= 3.0
