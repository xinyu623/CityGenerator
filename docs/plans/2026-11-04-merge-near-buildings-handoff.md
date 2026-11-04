# 交接：近邻楼体合并 + 四周 50 m 缓冲（9 城）

**状态**：待实现。这份文档给新窗口的 agent 用，不需要读之前的对话。

## 任务

两条改动，一起生效，覆盖 `models/city_cores` 里的**全部 9 份**（中国 6 城 + 国外 3 城）：

1. **近邻合并**：间距 <= 2 m 的楼体在并集时融成一整块（不删楼，各自屋顶高度保留，
   所以是"带台阶的整块"）。需要改代码，新增 `merge_gap_m`。
2. **四周 50 m 缓冲**：每份模型底面外扩 50 m 空地，建筑不动。**只用现有参数**
   `--ground-margin 50`，不需要改代码。目的是模型最外一圈永远是平地，不会是楼体墙面。

## 数据集现状

- 清单：`models/city_cores/layout_parameters.json`，9 条 `layouts`。
- 产物来源：`out\six-city\<city>`（中国 6 城）、`out\global-four\<city>`（纽约、洛杉矶、伦敦）。
- 组装脚本：`scratch/build_city_cores.py`（已经被扩成 9 城，内部有 SIX / FOUR 两个根目录常量）。
- 预览：`models/city_cores/render_previews.py`（Warm Graphite 配色；含 `save_png()` 重试）。
- 目录里另有 `overview_skyline.png`（6° 仰角的天际线视图），不是本次改动范围。

## 当前口径（9 份实际在用）

| 项 | 中国 6 城 | 国外 3 城 |
| --- | --- | --- |
| 轮廓简化 `--simplify` | 0.05 | 0.2 |
| 高度上限 `--max-height` | 700（上海中心 632） | 600（中央公园塔 472） |
| 高度下限 `--min-height` | 10 | 10 |
| 地形 | 仅重庆开（30 m + 250 m 缓冲坡道） | 全关 |
| 其他 | 上海/广州用 `--height-overrides` | `--origin-zero --no-proxy`；建筑数最大的是伦敦 15 607，默认 20 000 上限够用 |

本次两条改动对 9 份一律生效：`--merge-gap 2`、`--ground-margin 50`。

## 加 `merge_gap_m`：改哪里

合并机制已经存在，只是只对"相切"生效。入口在 `app/services/buildings.py` 的 `extrude_prepared`：

```python
footprints = [item.polygon_local for item in prepared.items]      # 约第 933 行
grow = max(0.0, float(contact_overlap_m)) / 2.0
expanded: dict[int, Polygon] = {}
if grow > 0.0:
    for index in _contact_indices(footprints, max(0.0, float(contact_overlap_m))):
        buffered = footprints[index].buffer(grow, join_style=2, mitre_limit=2.0)
        if buffered.is_valid and not buffered.is_empty:
            expanded[index] = buffered
```

- `_contact_indices(polygons, tolerance)`（约第 887 行）用
  `STRtree.query(polygons, predicate="dwithin", distance=tolerance)` 找"接触或近乎接触"的下标。
- 被标记的轮廓各外扩 `tolerance / 2`，挤出后两栋有了公共体积，Manifold 并集才会真正融合。
- `contact_overlap_m` 默认 `0.1`（`app/config.py`），由 `app/services/pipeline.py` 传入
  （搜 `contact_overlap_m=`）。

改法：

- 请求选项：`app/models/schemas.py` 的 `GenerateOptions` 加
  `merge_gap_m: float = Field(default=0.0, ge=0.0, le=20.0)`。
- CLI：`app/cli.py` 加 `--merge-gap`（照抄 `--min-height` 那一段的写法），并写进 `options` 字典。
- `extrude_prepared` 加 `merge_gap_m: float = 0.0`，`run_pipeline` 传 `request.options.merge_gap_m`。
- 计算：`tolerance = max(contact_overlap_m, merge_gap_m)`；
  `grow = tolerance / 2.0 + 0.05`（+0.05 保证是体积重叠，相切并集会留下非流形边）。
- `merge_gap_m = 0` 时必须与现在**完全一致**（tolerance 退回 `contact_overlap_m`）。

**副作用必须写进文档**：外扩是四边等距的，2 m 间距会让街道各窄约 1 m，小院子（5 m 见方）会被填平。
若用户不接受，备选是"在近邻对之间加桥接柱"，只补缝不长胖，工作量大一些。

## 测试

`tests/test_geometry.py` 有夹具 `_feature(osm_id, tags, offset, size)`：

1. 两栋间距 1 m，`merge_gap_m=2` 时并集是**一个连通实体**；
2. 同样两栋，`merge_gap_m=0` 时仍是两个壳（保持现状）；
3. 间距 5 m 的两栋在 `merge_gap_m=2` 下不合并；
4. `merge_gap_m=0` 的三角形数/体积与改动前一致。

再加一条 CLI 传参测试，照抄 `tests/test_cli.py` 里
`test_cli_parser_accepts_a_raised_height_ceiling` / `test_cli_passes_the_edge_taper_to_the_request`。

## 重跑（9 份）

六份中国城市的参数（共用）：
`--height 25 --level-height 3 --min-height 10 --embed 0.5 --simplify 0.05 --min-area 4 --max-height 700 --merge-gap 2 --ground-margin 50 --quiet`

三份国外城市（共用）：
`--height 25 --level-height 3 --min-height 10 --embed 0.5 --simplify 0.2 --min-area 4 --max-height 600 --merge-gap 2 --ground-margin 50 --origin-zero --no-proxy --quiet`。
伦敦 15 607 栋，默认 `CITYGEN_MAX_BUILDINGS=20000` 够用，不必再调。

| 城市 | bbox（south,west,north,east） | 输出目录 | simplify | 最高楼 (m) | 额外参数 |
| --- | --- | --- | ---: | ---: | --- |
| beijing | 39.893966,116.431577,39.930034,116.478423 | out\six-city\beijing | 0.05 | 528 | `--no-terrain` |
| shanghai | 31.218966,121.478988,31.255034,121.521012 | out\six-city\shanghai | 0.05 | 632 | `--no-terrain --height-overrides docs\plans\2026-11-02-height-overrides-shanghai.json` |
| guangzhou | 23.095966,113.305465,23.132034,113.344535 | out\six-city\guangzhou | 0.05 | 600 | `--no-terrain --height-overrides docs\plans\2026-11-02-height-overrides-guangzhou.json` |
| shenzhen | 22.521966,114.035548,22.558034,114.074452 | out\six-city\shenzhen | 0.05 | 599 | `--no-terrain` |
| changsha | 28.177966,112.956615,28.214034,112.997385 | out\six-city\changsha | 0.05 | 452 | `--no-terrain` |
| chongqing | 29.541966,106.549345,29.578034,106.590655 | out\six-city\chongqing | 0.05 | 363 | `--terrain-resolution 30 --terrain-edge-taper 250` |
| new_york | 40.734966,-74.013717,40.771034,-73.966283 | out\global-four\new_york | 0.2 | 472 | `--no-terrain --origin-zero --no-proxy` |
| los_angeles | 34.029966,-118.274683,34.066034,-118.231317 | out\global-four\los_angeles | 0.2 | 335 | `--no-terrain --origin-zero --no-proxy` |
| london | 51.494966,-0.117869,51.531034,-0.060131 | out\global-four\london | 0.2 | 310 | `--no-terrain --origin-zero --no-proxy` |

跑完重建数据集并重绘：

```powershell
.\.venv\Scripts\python.exe scratch\build_city_cores.py
$env:CITY_CORES_DIR = 'models/city_cores'
.\.venv\Scripts\python.exe models\city_cores\render_previews.py
```

OSM / DEM 已缓存，9 份全跑大致 2-3 分钟（伦敦的网格较重建模稍慢）。

## 基准数字（改动前，v2.3.3 + 国外三城）

| 城市 | 建筑 | 三角形 | 最高 | 底面 |
| --- | ---: | ---: | ---: | --- |
| beijing | 2,949 | 67,168 | 528.0 | 4.22 x 4.37 km |
| shanghai | 3,958 | 113,052 | 632.0 | 4.23 x 4.17 km |
| guangzhou | 1,653 | 47,604 | 600.0 | 4.15 x 4.15 km |
| shenzhen | 2,562 | 115,166 | 599.1 | 4.11 x 4.22 km |
| changsha | 5,338 | 150,330 | 452.0 | 4.23 x 4.13 km |
| chongqing | 1,539 | 116,668 | 363.0 | 4.63 x 4.56 km |
| new_york | 11,245 | 352,872 | 472.0 | 4.14 x 4.20 km |
| los_angeles | 6,570 | 317,948 | 335.3 | 4.30 x 4.44 km |
| london | 15,607 | 512,198 | 310.0 | 4.26 x 4.30 km |

合并会让三角形数下降（相邻墙面被并集消掉），这是预期；但水密、单连通实体、非流形为 0 不能变。

## 验收

`scratch/audit_six.py` 目前只读 `out/six-city` 的六份。把它改成遍历 `out/six-city` 与
`out/global-four`，或复制一份改路径；检查项不变：水密、绕向、非流形边、**连通分量 1**、
退化面 0、重复面 0、`patches.json` 区间恰好覆盖每个三角形一次、三角形数与 metadata 一致。

## 要同步的文档

1. `docs/plans/2026-11-02-six-city-core-stl.md`：参数表加 `merge_gap_m`、`ground_margin_m` 两行，
   结果表更新底面尺寸与三角形数。
2. `docs/plans/2026-11-04-three-global-city-core-stl.md`：同上（国外三城的取舍与参数）。
3. `models/city_cores/README.md`：表格里的三角形数、底面，以及"合并/缓冲"约定。
4. `CHANGELOG.md` + `app/config.py` 的 `version`（建议 `2.4.0`，因为新增公开参数）。

## 环境坑（这个工作区踩过的）

- **文件沙箱只认工作区相对路径**：绝对路径、仓库外路径会被静默拒绝或返回空。
- **写文件工具偶发"报成功但没落盘"**：`write` 之后用 `Test-Path` 确认；不可靠时改用
  `[System.IO.File]::WriteAllText($path, $text, [System.Text.UTF8Encoding]::new($false))`。
- **别用 PowerShell `Set-Content` 改源码**：会把 UTF-8 写成 ANSI，中文注释变乱码。
- **控制台是 GBK**：CLI 摘要里不能出现 `²` 之类，否则成功的任务会以退出码 1 结束
  （`tests/test_cli.py::test_summary_prints_on_a_gbk_console`）。
- **PIL 存大 PNG 偶发 `OSError: EINVAL`**：`render_previews.py` 的 `save_png()` 已做临时名 + 重试，别删。
- `models/`、`out/`、`data/`、`scratch/` 都在 `.gitignore`；数据集留在本地不入库。
- git 提交时的 `LF will be replaced by CRLF` 警告是正常的。
