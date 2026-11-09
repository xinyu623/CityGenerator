# 鍓嶇鐭冲ⅷ鐏版敼鐗?路 瀹炴柦璁″垝

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 鎶婄綉椤靛澹充粠鏆栫背 + 閾侀攬绾㈡崲鎴愮焊搴曠煶澧ㄧ伆锛屽彧鐣欎富鎸夐挳涓庤繘搴︽潯涓€澶勬殩鑹诧紱3D 棰勮鏀规垚娴锋姤鐨?绾稿簳 + 楂樺害娓愬彉"銆?

**Architecture:** 涓夊鏀瑰姩锛屼簰涓嶈€﹀悎锛歚app.css` 鐨?token 涓庣粍浠惰壊锛堜竴涓枃浠讹級锛沗map.js` 鐨勯€夋涓夎壊锛沗viewer.js` 鐨勬潗璐ㄣ€佽壊闃朵笌鐏厜銆傞鑹插叏閮ㄦ敹鏁涙垚 `:root` token锛屾棫瀛楅潰閲忎竴涓笉鐣欍€傛渶鍚庣敤 CDP 鎴浘鍋氳瑙夐獙鏀躲€?

**Tech Stack:** 鍘熺敓 CSS 鑷畾涔夊睘鎬э紱Three.js r160锛圕DN importmap锛夛紱pytest + Node 瀛愯繘绋嬫祴璇曪紙椤圭洰宸叉湁濂楄矾锛夛紱headless Chrome + CDP 鎴浘銆?

**Spec:** `docs/superpowers/specs/2026-11-09-graphite-restyle-design.md`

## Global Constraints

- 棰滆壊瀛楅潰閲忓彧鑳芥潵鑷?`:root`锛沗app.css` 鍐呬笉寰楁畫鐣欐棫濂舵补/榧犲熬鑽夎壊锛堟祴璇曢噷鏈夊畬鏁存竻鍗曪級銆?
- 瀵规瘮搴︼細`--ink` 瀵?`--panel` 鈮?10:1锛沗--muted` 瀵?`--panel` 鈮?4.5:1锛?1鈥?2px 灏忓瓧鐢ㄥ畠锛夈€?
- 鏆栬壊 `#A7472C` 鍙厑璁稿嚭鐜板湪 `:root`銆乣.button.primary`銆乣.progress-bar` 涓夊銆?
- 3D 鑹插€间笌娴锋姤鍚屾簮锛氱焊搴?`#F2EFE9`锛屾笎鍙?`#DCD6CC 鈫?#241F1B`锛?
  鍋滅偣 0 / 0.10 / 0.25 / 0.45 / 0.70 / 1.00銆?
- 涓嶆柊澧炰緷璧栥€佷笉寮曞叆 web font銆佷笉鏀?DOM 缁撴瀯銆佷笉鏀瑰湴鍥?tile 婧愩€?
- 3D 椤剁偣鑹插啓鍏ョ嚎鎬х┖闂达紙sRGB鈫抣inear锛夛紝鍚﹀垯浼氳娓叉煋鍣ㄤ簩娆＄紪鐮佹礂鐧姐€?
- 闈㈡澘鍦嗚 10px銆佹帶浠?8px锛涚粏绾夸竴寰?1px锛涢槾褰遍檷鍒板嚑涔庝笉鍙銆?

## Review Focus

- **绾钩鍦?鏋佸皬楂樺樊閫夊尯**锛歚maxY - minY < 1 m` 鏃惰壊闃堕€€鍖栵紝蹇呴』钀藉洖鏈€娴呬竴妗ｏ紝涓嶈兘鍏ㄩ粦鎴?NaN銆傦紙Task 3 娴嬭瘯锛?
- **娌℃湁 `patches.json` 鐨勬棫浠诲姟**锛氬洖閫€鎴愬崟鍑犱綍浣擄紝浠嶈璧板悓涓€濂楄壊闃讹紝涓嶈兘娈嬬暀绫崇櫧銆傦紙Task 3 娴嬭瘯锛?
- **鐡︾墖鍔犺浇澶辫触**锛氬湴鍥惧彧鍓?`--bg` 搴曡壊锛岄€夋銆佹彁绀恒€佺粺璁″崱浠嶈鐪嬪緱娓呫€傦紙Task 1 瀵规瘮搴︽祴璇?+ Task 2 閫夋瀵规瘮搴︽祴璇曪級
- **绾挎妯″紡**锛氶《鐐硅壊 + wireframe 鍙犲姞涓嶈兘绯婃垚涓€鐗囥€傦紙Task 4 鎴浘锛?
- **disabled 涓绘寜閽?*锛歚opacity .48` 鐨勬殩鑹插湪绾稿簳涓婁笉鑳借鎴愬彟涓€绉嶇矇銆傦紙Task 4 鎴浘锛?

---

### Task 1: 澶栧３ CSS 鐭冲ⅷ鐏板寲

**Files:**
- Modify: `app/static/css/app.css`锛堝叏鏂囷級
- Create: `tests/test_static_theme.py`

**Interfaces:**
- Consumes: 鏃?
- Produces: `:root` token 鍚嶏紙`--bg/--panel/--panel-2/--paper/--header/--ink/--ink-soft/--muted/--line/--line-strong/--accent/--danger/--warn/--map-filter/--shadow/--radius/--radius-sm`锛夛紝Task 4 鐨勬埅鍥句緷璧栧畠浠€?

- [ ] **Step 1: 鍐欏け璐ユ祴璇?*

`tests/test_static_theme.py`锛?

```python
from __future__ import annotations

import re
from pathlib import Path

import pytest

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
        assert re.search(rf"{name}:\s*{value}", CSS), f"{name} 鏈涓?{value}"


def test_no_legacy_cream_or_sage_literals_survive():
    found = sorted({h for h in LEGACY if h in CSS.lower()})
    assert found == [], f"浠嶆畫鐣欐棫鑹? {found}"


def _blocks(selector: str) -> str:
    return "".join(m.group(0) for m in re.finditer(rf"{re.escape(selector)}[^{{}}]*\{{[^{{}}]*\}}", CSS))


def test_warm_accent_is_confined_to_the_primary_action():
    allowed = (_blocks(":root") + _blocks(".button.primary") + _blocks(".progress-bar")).lower()
    for hex_value in ("#a7472c", "#8f3b24"):
        for match in re.finditer(hex_value, CSS.lower()):
            assert css_block_at(match.start()).lower() in allowed, f"{hex_value} 鍑虹幇鍦ㄤ笉鍏佽鐨勮鍒欓噷"
    assert "accent-color: var(--accent)" not in CSS


def css_block_at(index: int) -> str:
    start = CSS.rfind("}", 0, index) + 1
    end = CSS.find("}", index) + 1
    return CSS[start:end]


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
    assert "--map-filter: saturate(0.55)" in CSS
    assert re.search(r"\.leaflet-container\s*\{[^}]*background:\s*var\(--bg\)", CSS)
```

- [ ] **Step 2: 璺戝畠锛岀‘璁ゅけ璐?*

Run: `.venv\Scripts\python.exe -m pytest tests/test_static_theme.py -q`
Expected: FAIL锛坄:root` 閲岃繕鏄?`--bg: #e9eee5`锛?

- [ ] **Step 3: 鏀?`app.css`**

鍏堟妸 `:root` 鎹㈡垚锛堥€愬瓧锛屽惈绌鸿锛夛細

```css
:root {
  --bg: #ece9e2;
  --panel: #f6f4ef;
  --panel-2: #fbfaf6;
  --paper: #f2efe9;
  --header: #f3f0e9;
  --ink: #211d19;
  --ink-soft: #3b3631;
  --muted: #6e675e;
  --line: #dbd6cb;
  --line-strong: #c2bcaf;
  --accent: #a7472c;
  --accent-ink: #fff6f0;
  --danger: #8a3a2a;
  --warn: #6e5424;
  --map-filter: saturate(0.55) contrast(0.96) brightness(1.03);
  --shadow: 0 12px 30px rgba(30, 27, 22, 0.1);
  --radius: 10px;
  --radius-sm: 8px;
}
```

鍐嶆寜杩欏紶琛ㄦ浛鎹㈠瓧闈㈤噺锛堟病鏈夊垪鍑虹殑鍗婇€忔槑鐧?榛戯紝涓€寰嬫崲鎴愬悓鏄庡害鐨勪腑鎬х伆锛?
`rgba(255,255,255,x)` 鈫?`rgba(246,244,239,x)`锛宍rgba(65,58,45,x)` 鈫?`rgba(30,27,22,x)`锛夛細

| 鏃у瓧闈㈤噺 | 鏂板幓鍚?|
|---|---|
| `#e3e8de`锛坢ap/leaflet 搴曪級 | `var(--bg)` |
| `#fdfbf5` `#fffdf7` `#faf8f0` `#fbf9f1` `#f8f7ef` `#f7f3e8` `#eef0e6` `#f3f0e5` | `var(--panel)`锛堝崱鐗囷級/ `var(--panel-2)`锛堣緭鍏ユ銆佸唴宓屼簳锛?|
| `#e8e6da` `#e8e5d8` `#e8e3d5` `#e3cbbb` `#e2ddcf` `#ded8c8` `#d8d1bf` | `var(--line)` / `var(--line-strong)` |
| `#f1c7c1` `#fff5f4` | 杈规 `#e4cdc4`锛屽簳 `#f6edea`锛屽瓧 `var(--danger)` |
| `#f0d9b3` `#75430a` | 杈规 `#e4d8be`锛屽簳 `#f7f0e0`锛屽瓧 `var(--warn)` |
| `#b9a488` `#a08c72`锛堝簳闈㈠浘渚嬶級 | `#6e675f` / `#4a443c` |
| `#f7f3e8` `#d8d1bf`锛堝缓绛戝浘渚嬶級 | `#dcd6cc` / `#c2bcaf` |
| `#5d6256` `#5e6256` `#6e7266` `#4f554a` `#8b8e80` `#7b3522` `#7d3a27` | `var(--ink-soft)` / `var(--muted)` |
| `#b9c0b0` `#bcc4b5` `#a9bbc9` `#c8b9a6` `#b9c2b3` | `var(--line-strong)` |
| `#fff8ee` `#fff9f0`锛堜富鎸夐挳瀛楋級 | `var(--accent-ink)` |
| `#bc5738` `#c96443` `#b14e31` `#8e3822` `#85331f` `#d58a68` `#c56a4d` `#7d2f1d` | 鍒犳帀锛氫富鎸夐挳涓庤繘搴︽潯鏀圭函鑹?`var(--accent)`锛宧over `#8f3b24` |

缁撴瀯鎬ф敼鍔紙鐓у仛锛屼笉瑕佽嚜鐢卞彂鎸ワ級锛?

- `body` 鑳屾櫙鏀规垚绾?`var(--bg)`锛屽垹鎺変袱涓?`radial-gradient`銆?
- `.brand-mark` 鏀圭煶澧ㄦ笎鍙?`linear-gradient(145deg,#3a342e,#211d19 60%,#15120f)`锛屽瓧鑹?`#f6f4ef`锛屽渾瑙?`var(--radius-sm)`銆?
- `.pill` / `.pill.subtle`锛氶€忔槑搴曘€乣var(--line)` 杈规銆乣var(--muted)` 瀛椼€?
- `.panel` / `.topbar` / `.footer` / `.sidebar` 鐢?`--panel`銆乣--header`銆乣--bg`锛岃竟妗嗕竴寰?1px `var(--line)`锛岄槾褰卞垹鍒板彧鍓?`--shadow` 鎴栨病鏈夈€?
- `.step-label`銆乣.badge`銆乣.badge.ok`銆乣.dot`銆乣.map-note`銆乣.footer` 鍏ㄩ儴杞伆锛坄--muted` / `--line`锛夛紝涓嶅啀鐢?accent銆?
- `input:focus`锛氳竟妗?`var(--ink-soft)`锛岀幆 `rgba(59,54,49,0.1)`锛沗accent-color: var(--ink-soft)`銆?
- `.progress`锛歚background: var(--line)`锛沗.progress-bar`锛歚background: var(--accent)`锛沗.progress-bar` 鐨勬笎鍙樺垹鎺夈€?
- `.result-panel` 鍒犳帀 `inset 0 3px 0 rgba(...)` 鏆栬壊鏉★紝鍙暀 1px `var(--line-strong)`銆?
- `.map-readout` 鏀规垚娣辫壊閿氱偣锛歚rgba(33,29,25,0.92)` 搴曘€乣#f6f4ef` 瀛椼€佹爣绛?`#a9a299`銆佸垎闅旂嚎 `rgba(246,244,239,0.14)`銆?
- `.viewer-wrap` 鑳屾櫙鏀?`var(--paper)` 绾壊锛沗::after` 鏆楄闄嶅埌 `rgba(71,64,55,0.06)`銆?
- `.viewer-tag` / `.viewer-hint` / `.viewer-status`锛氱焊搴曞崐閫忔槑 + `var(--ink-soft)` 瀛楋紝鐐圭姸鏍囪鏀圭伆銆?
- `.viewer-action.is-active`锛氭繁鑹插～鍏?`#211d19` + 绾歌壊瀛楋紙鍘熸潵鏄殩鑹诧級銆?
- `.leaflet-tile-pane { filter: var(--map-filter); }`銆?
- `.leaflet-control-attribution a` 涓嶅啀鐢?accent銆?
- 鍦嗚锛歚16px鈫抳ar(--radius)`銆乣10px/12px鈫抳ar(--radius-sm)`銆乣22px鈫?2px`锛沗999px` 淇濈暀銆?
- 瀛楅噸锛歚h1/h2/h3` 涓?`.brand-text h1` 鐢?700 闄嶅埌 600銆?

- [ ] **Step 4: 璺戞祴璇曞埌缁?*

Run: `.venv\Scripts\python.exe -m pytest tests/test_static_theme.py -q`
Expected: PASS锛? passed锛?

- [ ] **Step 5: 鎻愪氦**

```bash
git add app/static/css/app.css tests/test_static_theme.py
git commit -m "style: graphite shell for the web UI"
```

---

### Task 2: 鍦板浘閫夋鏀圭煶澧ㄧ伆

**Files:**
- Modify: `app/static/js/map.js:139-199`
- Modify: `tests/test_static_theme.py`锛堣拷鍔犱袱鏉★級

**Interfaces:**
- Consumes: Task 1 鐨?`--bg` / `--ink` 鍊硷紙娴嬭瘯閲岀洿鎺ュ啓瀛楅潰鑹诧級
- Produces: 鏃?

- [ ] **Step 1: 杩藉姞澶辫触娴嬭瘯**

```python
MAP = (ROOT / "app" / "static" / "js" / "map.js").read_text(encoding="utf-8")


def test_selection_outline_is_graphite_not_slate():
    for old in ("#547883", "#7fa1aa"):
        assert old not in MAP.lower()
    assert MAP.count('"#544d45"') == 3


def test_selection_outline_contrasts_with_the_paper_backdrop():
    assert _ratio("#544d45", TOKENS["--bg"]) >= 3.0
```

- [ ] **Step 2: 璺戝畠锛岀‘璁ゅけ璐?*

Run: `.venv\Scripts\python.exe -m pytest tests/test_static_theme.py -q`
Expected: FAIL锛坄#547883` 浠嶅湪锛?

- [ ] **Step 3: 鏀?`map.js`**

涓夊 `color: "#547883"` 鈫?`"#544d45"`锛涗袱澶?`fillColor: "#7fa1aa"` 鈫?`"#9a9286"`
锛坄fillOpacity` 淇濈暀 0.13 / 0.08锛夛紱`firstMarker` 鐨?`fillColor: "#ffffff"` 鈫?`"#fbfaf6"`銆?
鍙︽妸瀹屾垚閫夊尯 `weight: 2` 闄嶅埌 `1.5`銆?

- [ ] **Step 4: 璺戞祴璇曞埌缁?*

Run: `.venv\Scripts\python.exe -m pytest tests/test_static_theme.py -q`
Expected: PASS锛? passed锛?

- [ ] **Step 5: 鎻愪氦**

```bash
git add app/static/js/map.js tests/test_static_theme.py
git commit -m "style: graphite selection rectangle on the map"
```

---

### Task 3: 3D 棰勮鎹㈢煶澧ㄦ潗璐ㄤ笌楂樺害鑹查樁

**Files:**
- Modify: `app/static/js/viewer.js:1-18, 127-145, 222-277, 555-573`
- Modify: `tests/test_frontend_viewer.py`

**Interfaces:**
- Consumes: 鏃?
- Produces: `COLORS`銆乣HEIGHT_RAMP`锛坄[t, "#rrggbb"]` 鏁扮粍锛夈€乣rampColor(t: number) -> [r,g,b]`锛堢嚎鎬х┖闂?0鈥?锛夈€乣rampColors(positions: Float32Array, minY: number, maxY: number) -> Float32Array`锛孴ask 4 鐨勬埅鍥句緷璧栥€?

- [ ] **Step 1: 鏀规祴璇曞埌鏂伴鏈燂紝骞惰拷鍔犺壊闃舵祴璇?*

`tests/test_frontend_viewer.py` 鐨?`HARNESS` 澧炲姞瀵煎嚭锛?

```js
const module = await import(pathToFileURL(viewerPath).href);
console.log(JSON.stringify({
  materials: module.MATERIALS,
  colors: module.COLORS,
  ramp: module.HEIGHT_RAMP,
  at0: module.rampColor(0),
  at1: module.rampColor(1),
  flat: Array.from(module.rampColors(new Float32Array([0, 0, 0, 0, 0, 0]), 0, 0)),
  ramp3: Array.from(module.rampColors(new Float32Array([0, 0, 0, 0, 5, 0, 0, 10, 0]), 0, 10)),
  mixed: module.legendGroups({ ground: 19718, wall: 12552, bottom: 322 }),
  ...
}));
```

鏂█鏀规垚锛?

```python
def test_preview_materials_are_graphite_with_vertex_colours(tmp_path):
    payload = _viewer_payload(tmp_path)
    materials = payload["materials"]
    assert materials["ground"] == materials["wall"] == materials["buildings"]
    assert materials["ground"]["color"] == 0xFFFFFF
    assert materials["ground"]["vertexColors"] is True
    assert materials["bottom"]["vertexColors"] is False
    assert materials["bottom"]["color"] == 0x6E675F
    assert payload["colors"]["plate"] == 0xF2EFE9


def test_height_ramp_runs_from_light_paper_grey_to_near_black(tmp_path):
    payload = _viewer_payload(tmp_path)
    assert [stop[1] for stop in payload["ramp"]] == [
        "#DCD6CC", "#CFC6B9", "#ACA196", "#7F756C", "#4F4842", "#241F1B"
    ]

    def lum(rgb):
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

    assert lum(payload["at0"]) > lum(payload["at1"]) * 20


def test_ramp_degrades_to_the_light_stop_on_a_flat_selection(tmp_path):
    payload = _viewer_payload(tmp_path)
    assert payload["flat"] == pytest.approx(list(payload["at0"]) * 2)
    assert payload["ramp3"] == pytest.approx(
        [*payload["at0"], *payload["at0"], *payload["at1"]], rel=1e-6
    )
    assert len(payload["ramp3"]) == 9
```

- [ ] **Step 2: 璺戝畠锛岀‘璁ゅけ璐?*

Run: `.venv\Scripts\python.exe -m pytest tests/test_frontend_viewer.py -q`
Expected: FAIL锛坄rampColor` 鏈鍑?/ 棰滆壊浠嶆槸 `0xF2EBDC`锛?

- [ ] **Step 3: 鏀?`viewer.js`**

椤堕儴鎹㈡垚锛?

```js
export const COLORS = {
  city: 0xffffff,
  buildings: 0xffffff,
  ground: 0xffffff,
  wall: 0xffffff,
  // The underside is the section cut: it stays flat graphite instead of taking
  // the height ramp, which would paint the whole slab the lightest stop.
  bottom: 0x6e675f,
  plate: 0xf2efe9,
};

export const MATERIALS = {
  city: { color: COLORS.city, vertexColors: true, roughness: 0.9, metalness: 0.0 },
  buildings: { color: COLORS.buildings, vertexColors: true, roughness: 0.88, metalness: 0.0 },
  ground: { color: COLORS.ground, vertexColors: true, roughness: 0.88, metalness: 0.0 },
  wall: { color: COLORS.wall, vertexColors: true, roughness: 0.88, metalness: 0.0 },
  bottom: { color: COLORS.bottom, vertexColors: false, roughness: 0.95, metalness: 0.0 },
};

export const HEIGHT_RAMP = [
  [0.0, "#DCD6CC"],
  [0.1, "#CFC6B9"],
  [0.25, "#ACA196"],
  [0.45, "#7F756C"],
  [0.7, "#4F4842"],
  [1.0, "#241F1B"],
];
```

鍔犵函鍑芥暟锛圢ode 鍙祴锛屼笉渚濊禆 three锛夛細

```js
function srgbToLinear(c) {
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

const RAMP_LINEAR = HEIGHT_RAMP.map(([t, hex]) => [
  t,
  [1, 3, 5].map((i) => srgbToLinear(parseInt(hex.slice(i, i + 2), 16) / 255)),
]);

export function rampColor(t) {
  const clamped = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0));
  for (let i = 1; i < RAMP_LINEAR.length; i += 1) {
    const [t1, c1] = RAMP_LINEAR[i];
    const [t0, c0] = RAMP_LINEAR[i - 1];
    if (clamped <= t1) {
      const k = t1 === t0 ? 0 : (clamped - t0) / (t1 - t0);
      return [0, 1, 2].map((j) => c0[j] + (c1[j] - c0[j]) * k);
    }
  }
  return RAMP_LINEAR[RAMP_LINEAR.length - 1][1];
}

export function rampColors(positions, minY, maxY) {
  const span = maxY - minY;
  const out = new Float32Array(positions.length);
  for (let i = 0; i < positions.length; i += 3) {
    const t = span >= 1 ? (positions[i + 1] - minY) / span : 0;
    const [r, g, b] = rampColor(t);
    out[i] = r;
    out[i + 1] = g;
    out[i + 2] = b;
  }
  return out;
}
```

鍐嶅姞 `applyRamp(geometry, minY, maxY)`锛氱粰 geometry 璁剧疆 `color` 灞炴€?
锛坄rampColors(geometry.attributes.position.array, minY, maxY)`锛夛紝骞惰繑鍥?geometry銆?

`loadModels` 閲岋細姣忎釜 mesh 鏋勫缓瀹屾垚鍚庝笉绔嬪埢涓婅壊锛涚瓑 `combined` 鐨勫寘鍥寸洅
`box` 绠楀嚭鏉ャ€佹墍鏈夊嚑浣曚綋宸插钩绉?`lift` 涔嬪悗锛岄亶鍘?`state.modelGroup.children`锛?
鍙 `material.vertexColors === true` 鐨?mesh 璋?`applyRamp(child.geometry, box.min.y, box.max.y)`銆?

鐏厜涓庢洕鍏夛細

- `renderer.toneMappingExposure = 0.95`
- `HemisphereLight(0xfffcf6, 0x6e675f, 0.5)`
- `keyLight`锛歚DirectionalLight(0xfff6e8, 1.9)`
- `fillLight`锛歚(0xb9c4cc, 0.35)`锛沗rimLight`锛歚(0xffffff, 0.25)`
- `buildPedestal` 鐨勬潗璐ㄨ壊 `COLORS.plate`銆乣roughness: 0.85`銆乣envMapIntensity: 0.25`
- 鎺ュ湴褰卞緞鍚戞笎鍙樻敼鎴?`rgba(48, 43, 37, 0.26)` / `0.10` / `0`

- [ ] **Step 4: 璺戞祴璇曞埌缁?*

Run: `.venv\Scripts\python.exe -m pytest tests/test_frontend_viewer.py -q`
Expected: PASS锛? passed锛?

- [ ] **Step 5: 鎻愪氦**

```bash
git add app/static/js/viewer.js tests/test_frontend_viewer.py
git commit -m "style: graphite 3D preview with a height ramp"
```

---

### Task 4: 鎴浘楠屾敹锛堟棤婧愮爜鏀瑰姩锛?

**Files:**
- Create: `scratch/restyle/capture.py`锛坰cratch/ 宸?gitignore锛?
- Create: `scratch/restyle/*.png`锛堥獙鏀朵骇鐗╋級

**Interfaces:**
- Consumes: 鍓嶄笁涓换鍔＄殑浜х墿锛沗data/jobs/<id>/output/` 閲屽凡鏈夌殑 `out/six-city/changsha/output/` 鍓湰
- Produces: `shell.png`銆乣result.png`銆乣viewer.png`

- [ ] **Step 1: 閫犱竴涓?宸插畬鎴?鐨勫亣浠诲姟**

鎶?`out/six-city/changsha/output/` 澶嶅埗鍒?`data/jobs/restyle-demo/output/`锛屽啓
`data/jobs/restyle-demo/job.json`锛歚status: "completed"`銆乣progress: 100`銆?
`stats` 浠?`metadata.json` 鍙栵紙`buildings/triangles/area_km2/...`锛夛紝
`files` 鍒楀嚭 `city.stl / patches.json / metadata.json` 鍙婂畠浠殑 `/api/jobs/restyle-demo/files/<name>` URL銆?

- [ ] **Step 2: 璧锋湇鍔?*

Run锛堝悗鍙帮級: `.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
鎴愬姛鏍囧織锛歚GET http://127.0.0.1:8000/api/config` 杩斿洖 200銆?

- [ ] **Step 3: 鍐?`capture.py` 骞舵媿涓夊紶**

娌跨敤 `scratch/capture_readme_preview.py` 鐨?CDP 浼氳瘽绫伙紱涓嶅悓鐐规槸锛?

1. `POST /api/jobs` 涓?`GET /api/jobs/*` 鐢ㄩ〉闈㈠唴 `fetch` 閽╁瓙杩斿洖鍋囦换鍔＄殑 JSON锛?
   璁?`app.js` 鐪熷疄鐨?`renderResult` 璺戣捣鏉ワ紙涓嶇 Overpass锛夈€?
2. 1440脳1000锛宍deviceScaleFactor: 1`銆?
3. 涓夊紶锛氭斁澶у湴鍥惧悗鐨勭┖澹筹紙`shell.png`锛夛紱鐐瑰畬"鐢熸垚"鐩村埌缁撴灉闈㈡澘鍑虹幇锛坄result.png`锛夛紱
   鐐?3D 棰勮"銆佸仠鑷姩鏃嬭浆鍚庯紙`viewer.png`锛夈€?

- [ ] **Step 4: 鐪嬪浘鍜岃嚜妫€**

鐢?read_image 閫愬紶鐪嬶紝骞跺洖绛旇繖浜旀潯锛圧eview Focus锛夛細绾钩鍦?鏋佸皬楂樺樊涓嬫ā鍨嬩笉鑷冲叏榛戯紱
鏃?patches 鍥為€€浠嶆槸鐭冲ⅷ锛涚嚎妗嗗彲璇伙紱鐡︾墖澶辫触鏃堕€夋鍙锛沝isabled 涓绘寜閽笉鏄剧矇銆?
浠讳綍涓€鏉′笉杩?鈫?鍥炲埌瀵瑰簲浠诲姟鏀癸紝閲嶆媿銆?

- [ ] **Step 5: 鎶ュ憡**

鎶?`shell.png` / `result.png` / `viewer.png` 涓夊紶璺緞浜ょ粰鐢ㄦ埛锛?
骞跺垪鍑轰笌 `docs/images/frontend.png`銆乣docs/images/preview.png` 鐨勫樊寮傦紱
鏄惁鏇挎崲 docs 閲岀殑鍥撅紝绛夌敤鎴峰喅瀹氾紙涓嶅湪鏈鍒掕寖鍥村唴锛夈€?

---

## 鑷璁板綍

- **Spec 瑕嗙洊**锛氬澹崇伆闃?鈫?Task 1锛涘崟涓€鏆栬壊 鈫?Task 1锛堟祴璇曢攣姝伙級锛涘湴鍥句繚鎸佸僵鑹蹭絾闄嶉ケ鍜?鈫?Task 1锛坄--map-filter`锛夛紱
  閫夋 鈫?Task 2锛?D 绾稿簳/鍙板骇/楂樺害娓愬彉/鐏厜 鈫?Task 3锛涙埅鍥鹃獙璇?鈫?Task 4銆?
- **绫诲瀷涓€鑷?*锛歚rampColor` / `rampColors` / `HEIGHT_RAMP` / `COLORS` 鍚嶅瓧鍦?Task 3 娴嬭瘯涓庡疄鐜伴噷涓€鑷淬€?
- **姣斾緥**锛氳鍒?鈮? 鍊嶄簬 spec锛屼富浣撴槸 Task 1 鐨勬浛鎹㈣〃涓?Task 3 鐨勭函鍑芥暟锛屾棤杞綍鎬т唬鐮併€?
