# 前端石墨灰改版 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把网页外壳从暖米 + 铁锈红换成纸底石墨灰，只留主按钮与进度条一处暖色；3D 预览改成海报的"纸底 + 高度渐变"。

**Architecture:** 三处改动，互不耦合：`app.css` 的 token 与组件色（一个文件）；`map.js` 的选框三色；`viewer.js` 的材质、色阶与灯光。颜色全部收敛成 `:root` token，旧字面量一个不留。最后用 CDP 截图做视觉验收。

**Tech Stack:** 原生 CSS 自定义属性；Three.js r160（CDN importmap）；pytest + Node 子进程测试（项目已有套路）；headless Chrome + CDP 截图。

**Spec:** `docs/superpowers/specs/2026-11-09-graphite-restyle-design.md`

## Global Constraints

- 颜色字面量只能来自 `:root`；`app.css` 内不得残留旧奶油/鼠尾草色（测试里有完整清单）。
- 对比度：`--ink` 对 `--panel` ≥ 10:1；`--muted` 对 `--panel` ≥ 4.5:1（11–12px 小字用它）。
- 暖色 `#A7472C` 只允许出现在 `:root`、`.button.primary`、`.progress-bar` 三处。
- 3D 色值与海报同源：纸底 `#F2EFE9`，渐变 `#DCD6CC → #241F1B`，
  停点 0 / 0.10 / 0.25 / 0.45 / 0.70 / 1.00。
- 不新增依赖、不引入 web font、不改 DOM 结构、不改地图 tile 源。
- 3D 顶点色写入线性空间（sRGB→linear），否则会被渲染器二次编码洗白。
- 面板圆角 10px、控件 8px；细线一律 1px；阴影降到几乎不可见。

## Review Focus

- **纯平地/极小高差选区**：`maxY - minY < 1 m` 时色阶退化，必须落回最浅一档，不能全黑或 NaN。（Task 3 测试）
- **没有 `patches.json` 的旧任务**：回退成单几何体，仍要走同一套色阶，不能残留米白。（Task 3 测试）
- **瓦片加载失败**：地图只剩 `--bg` 底色，选框、提示、统计卡仍要看得清。（Task 1 对比度测试 + Task 2 选框对比度测试）
- **线框模式**：顶点色 + wireframe 叠加不能糊成一片。（Task 4 截图）
- **disabled 主按钮**：`opacity .48` 的暖色在纸底上不能读成另一种粉。（Task 4 截图）

---

### Task 1: 外壳 CSS 石墨灰化

**Files:**
- Modify: `app/static/css/app.css`（全文）
- Create: `tests/test_static_theme.py`

**Interfaces:**
- Consumes: 无
- Produces: `:root` token 名（`--bg/--panel/--panel-2/--paper/--header/--ink/--ink-soft/--muted/--line/--line-strong/--accent/--danger/--warn/--map-filter/--shadow/--radius/--radius-sm`），Task 4 的截图依赖它们。

- [ ] **Step 1: 写失败测试**

`tests/test_static_theme.py`：

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
        assert re.search(rf"{name}:\s*{value}", CSS), f"{name} 未设为 {value}"


def test_no_legacy_cream_or_sage_literals_survive():
    found = sorted({h for h in LEGACY if h in CSS.lower()})
    assert found == [], f"仍残留旧色: {found}"


def _blocks(selector: str) -> str:
    return "".join(m.group(0) for m in re.finditer(rf"{re.escape(selector)}[^{{}}]*\{{[^{{}}]*\}}", CSS))


def test_warm_accent_is_confined_to_the_primary_action():
    allowed = (_blocks(":root") + _blocks(".button.primary") + _blocks(".progress-bar")).lower()
    for hex_value in ("#a7472c", "#8f3b24"):
        for match in re.finditer(hex_value, CSS.lower()):
            assert css_block_at(match.start()).lower() in allowed, f"{hex_value} 出现在不允许的规则里"
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

- [ ] **Step 2: 跑它，确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_static_theme.py -q`
Expected: FAIL（`:root` 里还是 `--bg: #e9eee5`）

- [ ] **Step 3: 改 `app.css`**

先把 `:root` 换成（逐字，含空行）：

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

再按这张表替换字面量（没有列出的半透明白/黑，一律换成同明度的中性灰：
`rgba(255,255,255,x)` → `rgba(246,244,239,x)`，`rgba(65,58,45,x)` → `rgba(30,27,22,x)`）：

| 旧字面量 | 新去向 |
|---|---|
| `#e3e8de`（map/leaflet 底） | `var(--bg)` |
| `#fdfbf5` `#fffdf7` `#faf8f0` `#fbf9f1` `#f8f7ef` `#f7f3e8` `#eef0e6` `#f3f0e5` | `var(--panel)`（卡片）/ `var(--panel-2)`（输入框、内嵌井） |
| `#e8e6da` `#e8e5d8` `#e8e3d5` `#e3cbbb` `#e2ddcf` `#ded8c8` `#d8d1bf` | `var(--line)` / `var(--line-strong)` |
| `#f1c7c1` `#fff5f4` | 边框 `#e4cdc4`，底 `#f6edea`，字 `var(--danger)` |
| `#f0d9b3` `#75430a` | 边框 `#e4d8be`，底 `#f7f0e0`，字 `var(--warn)` |
| `#b9a488` `#a08c72`（底面图例） | `#6e675f` / `#4a443c` |
| `#f7f3e8` `#d8d1bf`（建筑图例） | `#dcd6cc` / `#c2bcaf` |
| `#5d6256` `#5e6256` `#6e7266` `#4f554a` `#8b8e80` `#7b3522` `#7d3a27` | `var(--ink-soft)` / `var(--muted)` |
| `#b9c0b0` `#bcc4b5` `#a9bbc9` `#c8b9a6` `#b9c2b3` | `var(--line-strong)` |
| `#fff8ee` `#fff9f0`（主按钮字） | `var(--accent-ink)` |
| `#bc5738` `#c96443` `#b14e31` `#8e3822` `#85331f` `#d58a68` `#c56a4d` `#7d2f1d` | 删掉：主按钮与进度条改纯色 `var(--accent)`，hover `#8f3b24` |

结构性改动（照做，不要自由发挥）：

- `body` 背景改成纯 `var(--bg)`，删掉两个 `radial-gradient`。
- `.brand-mark` 改石墨渐变 `linear-gradient(145deg,#3a342e,#211d19 60%,#15120f)`，字色 `#f6f4ef`，圆角 `var(--radius-sm)`。
- `.pill` / `.pill.subtle`：透明底、`var(--line)` 边框、`var(--muted)` 字。
- `.panel` / `.topbar` / `.footer` / `.sidebar` 用 `--panel`、`--header`、`--bg`，边框一律 1px `var(--line)`，阴影删到只剩 `--shadow` 或没有。
- `.step-label`、`.badge`、`.badge.ok`、`.dot`、`.map-note`、`.footer` 全部转灰（`--muted` / `--line`），不再用 accent。
- `input:focus`：边框 `var(--ink-soft)`，环 `rgba(59,54,49,0.1)`；`accent-color: var(--ink-soft)`。
- `.progress`：`background: var(--line)`；`.progress-bar`：`background: var(--accent)`；`.progress-bar` 的渐变删掉。
- `.result-panel` 删掉 `inset 0 3px 0 rgba(...)` 暖色条，只留 1px `var(--line-strong)`。
- `.map-readout` 改成深色锚点：`rgba(33,29,25,0.92)` 底、`#f6f4ef` 字、标签 `#a9a299`、分隔线 `rgba(246,244,239,0.14)`。
- `.viewer-wrap` 背景改 `var(--paper)` 纯色；`::after` 暗角降到 `rgba(71,64,55,0.06)`。
- `.viewer-tag` / `.viewer-hint` / `.viewer-status`：纸底半透明 + `var(--ink-soft)` 字，点状标记改灰。
- `.viewer-action.is-active`：深色填充 `#211d19` + 纸色字（原来是暖色）。
- `.leaflet-tile-pane { filter: var(--map-filter); }`。
- `.leaflet-control-attribution a` 不再用 accent。
- 圆角：`16px→var(--radius)`、`10px/12px→var(--radius-sm)`、`22px→12px`；`999px` 保留。
- 字重：`h1/h2/h3` 与 `.brand-text h1` 由 700 降到 600。

- [ ] **Step 4: 跑测试到绿**

Run: `.venv\Scripts\python.exe -m pytest tests/test_static_theme.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
git add app/static/css/app.css tests/test_static_theme.py
git commit -m "style: graphite shell for the web UI"
```

---

### Task 2: 地图选框改石墨灰

**Files:**
- Modify: `app/static/js/map.js:139-199`
- Modify: `tests/test_static_theme.py`（追加两条）

**Interfaces:**
- Consumes: Task 1 的 `--bg` / `--ink` 值（测试里直接写字面色）
- Produces: 无

- [ ] **Step 1: 追加失败测试**

```python
MAP = (ROOT / "app" / "static" / "js" / "map.js").read_text(encoding="utf-8")


def test_selection_outline_is_graphite_not_slate():
    for old in ("#547883", "#7fa1aa"):
        assert old not in MAP.lower()
    assert MAP.count('"#544d45"') == 3


def test_selection_outline_contrasts_with_the_paper_backdrop():
    assert _ratio("#544d45", TOKENS["--bg"]) >= 3.0
```

- [ ] **Step 2: 跑它，确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_static_theme.py -q`
Expected: FAIL（`#547883` 仍在）

- [ ] **Step 3: 改 `map.js`**

三处 `color: "#547883"` → `"#544d45"`；两处 `fillColor: "#7fa1aa"` → `"#9a9286"`
（`fillOpacity` 保留 0.13 / 0.08）；`firstMarker` 的 `fillColor: "#ffffff"` → `"#fbfaf6"`。
另把完成选区 `weight: 2` 降到 `1.5`。

- [ ] **Step 4: 跑测试到绿**

Run: `.venv\Scripts\python.exe -m pytest tests/test_static_theme.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: 提交**

```bash
git add app/static/js/map.js tests/test_static_theme.py
git commit -m "style: graphite selection rectangle on the map"
```

---

### Task 3: 3D 预览换石墨材质与高度色阶

**Files:**
- Modify: `app/static/js/viewer.js:1-18, 127-145, 222-277, 555-573`
- Modify: `tests/test_frontend_viewer.py`

**Interfaces:**
- Consumes: 无
- Produces: `COLORS`、`HEIGHT_RAMP`（`[t, "#rrggbb"]` 数组）、`rampColor(t: number) -> [r,g,b]`（线性空间 0–1）、`rampColors(positions: Float32Array, minY: number, maxY: number) -> Float32Array`，Task 4 的截图依赖。

- [ ] **Step 1: 改测试到新预期，并追加色阶测试**

`tests/test_frontend_viewer.py` 的 `HARNESS` 增加导出：

```js
const module = await import(pathToFileURL(viewerPath).href);
console.log(JSON.stringify({
  materials: module.MATERIALS,
  colors: module.COLORS,
  ramp: module.HEIGHT_RAMP,
  at0: module.rampColor(0),
  at1: module.rampColor(1),
  flat: module.rampColors(new Float32Array([0, 0, 0, 0, 0, 0]), 0, 0),
  ramp3: module.rampColors(new Float32Array([0, 0, 0, 0, 5, 0, 0, 10, 0]), 0, 10),
  mixed: module.legendGroups({ ground: 19718, wall: 12552, bottom: 322 }),
  ...
}));
```

断言改成：

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

- [ ] **Step 2: 跑它，确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_frontend_viewer.py -q`
Expected: FAIL（`rampColor` 未导出 / 颜色仍是 `0xF2EBDC`）

- [ ] **Step 3: 改 `viewer.js`**

顶部换成：

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

加纯函数（Node 可测，不依赖 three）：

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

再加 `applyRamp(geometry, minY, maxY)`：给 geometry 设置 `color` 属性
（`rampColors(geometry.attributes.position.array, minY, maxY)`），并返回 geometry。

`loadModels` 里：每个 mesh 构建完成后不立刻上色；等 `combined` 的包围盒
`box` 算出来、所有几何体已平移 `lift` 之后，遍历 `state.modelGroup.children`，
只对 `material.vertexColors === true` 的 mesh 调 `applyRamp(child.geometry, box.min.y, box.max.y)`。

灯光与曝光：

- `renderer.toneMappingExposure = 0.95`
- `HemisphereLight(0xfffcf6, 0x6e675f, 0.5)`
- `keyLight`：`DirectionalLight(0xfff6e8, 1.9)`
- `fillLight`：`(0xb9c4cc, 0.35)`；`rimLight`：`(0xffffff, 0.25)`
- `buildPedestal` 的材质色 `COLORS.plate`、`roughness: 0.85`、`envMapIntensity: 0.25`
- 接地影径向渐变改成 `rgba(48, 43, 37, 0.26)` / `0.10` / `0`

- [ ] **Step 4: 跑测试到绿**

Run: `.venv\Scripts\python.exe -m pytest tests/test_frontend_viewer.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add app/static/js/viewer.js tests/test_frontend_viewer.py
git commit -m "style: graphite 3D preview with a height ramp"
```

---

### Task 4: 截图验收（无源码改动）

**Files:**
- Create: `scratch/restyle/capture.py`（scratch/ 已 gitignore）
- Create: `scratch/restyle/*.png`（验收产物）

**Interfaces:**
- Consumes: 前三个任务的产物；`data/jobs/<id>/output/` 里已有的 `out/six-city/changsha/output/` 副本
- Produces: `shell.png`、`result.png`、`viewer.png`

- [ ] **Step 1: 造一个"已完成"的假任务**

把 `out/six-city/changsha/output/` 复制到 `data/jobs/restyle-demo/output/`，写
`data/jobs/restyle-demo/job.json`：`status: "completed"`、`progress: 100`、
`stats` 从 `metadata.json` 取（`buildings/triangles/area_km2/...`），
`files` 列出 `city.stl / patches.json / metadata.json` 及它们的 `/api/jobs/restyle-demo/files/<name>` URL。

- [ ] **Step 2: 起服务**

Run（后台）: `.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
成功标志：`GET http://127.0.0.1:8000/api/config` 返回 200。

- [ ] **Step 3: 写 `capture.py` 并拍三张**

沿用 `scratch/capture_readme_preview.py` 的 CDP 会话类；不同点是：

1. `POST /api/jobs` 与 `GET /api/jobs/*` 用页面内 `fetch` 钩子返回假任务的 JSON，
   让 `app.js` 真实的 `renderResult` 跑起来（不碰 Overpass）。
2. 1440×1000，`deviceScaleFactor: 1`。
3. 三张：放大地图后的空壳（`shell.png`）；点完"生成"直到结果面板出现（`result.png`）；
   点"3D 预览"、停自动旋转后（`viewer.png`）。

- [ ] **Step 4: 看图和自检**

用 read_image 逐张看，并回答这五条（Review Focus）：纯平地/极小高差下模型不至全黑；
无 patches 回退仍是石墨；线框可读；瓦片失败时选框可见；disabled 主按钮不显粉。
任何一条不过 → 回到对应任务改，重拍。

- [ ] **Step 5: 报告**

把 `shell.png` / `result.png` / `viewer.png` 三张路径交给用户，
并列出与 `docs/images/frontend.png`、`docs/images/preview.png` 的差异；
是否替换 docs 里的图，等用户决定（不在本计划范围内）。

---

## 自检记录

- **Spec 覆盖**：外壳灰阶 → Task 1；单一暖色 → Task 1（测试锁死）；地图保持彩色但降饱和 → Task 1（`--map-filter`）；
  选框 → Task 2；3D 纸底/台座/高度渐变/灯光 → Task 3；截图验证 → Task 4。
- **类型一致**：`rampColor` / `rampColors` / `HEIGHT_RAMP` / `COLORS` 名字在 Task 3 测试与实现里一致。
- **比例**：计划 ≈3 倍于 spec，主体是 Task 1 的替换表与 Task 3 的纯函数，无转录性代码。
