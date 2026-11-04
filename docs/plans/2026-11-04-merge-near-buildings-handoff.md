# 交接：近邻楼体合并（merge near buildings）

**状态**：待实现。这份文档给新窗口的 agent 用，不需要读之前的对话。

## 任务

给这个项目加一个"把 2 m 以内的近邻楼体合并成整块"的参数，然后用它重新生成六城模型；
等纽约、洛杉矶的数据在另一个窗口跑完后，用同一套口径（含这个新参数）一起出。

用户接受的做法已经定了：**不删楼**，而是让间距 ≤ 2 m 的楼体在并集时融成一整块，
各自屋顶高度不变，所以合并后是**带台阶的整块**。街巷宽于 2 m 的地方不受影响。

## 仓库与命令

- 工作目录：`F:\College\nudt\code\CityGenerator`（以下路径都相对这里）
- Python：`.\.venv\Scripts\python.exe`
- 测试：`.\.venv\Scripts\python.exe -m pytest tests -q -p no:warnings`
- Lint：`.\.venv\Scripts\python.exe -m ruff check app tests`
- 生成单份：`.\.venv\Scripts\python.exe -m app.cli --bbox south,west,north,east --out out\<name> ...`
- 当前版本 `2.3.3`（`app/config.py` 的 `version`）

## 现有实现：`contact_overlap_m`

合并机制已经存在，只是只对"相切"生效。入口在 `app/services/buildings.py`：

```python
footprints = [item.polygon_local for item in prepared.items]      # 第 933 行附近
grow = max(0.0, float(contact_overlap_m)) / 2.0
expanded: dict[int, Polygon] = {}
if grow > 0.0:
    for index in _contact_indices(footprints, max(0.0, float(contact_overlap_m))):
        buffered = footprints[index].buffer(grow, join_style=2, mitre_limit=2.0)
        if buffered.is_valid and not buffered.is_empty:
            expanded[index] = buffered
```

- `_contact_indices(polygons, tolerance)`（第 887 行）用
  `STRtree.query(polygons, predicate="dwithin", distance=tolerance)` 找出"接触或近乎接触"的下标。
- 被标记的轮廓各外扩 `tolerance / 2`，挤出后两栋就有了公共体积，Manifold 并集会真正融成一个实体。
- `contact_overlap_m` 默认 `0.1`（`app/config.py`），经 `app/services/pipeline.py` 第 353 行传入。

**改动思路**：把"判定距离"和"外扩距离"分开，允许判定距离远大于 0.1 m。

- 新增请求选项（`app/models/schemas.py` 的 `GenerateOptions`）：`merge_gap_m: float = Field(default=0.0, ge=0.0, le=20.0)`。
- 新增 CLI 参数：`--merge-gap`（`app/cli.py`，与 `--min-height` 同一段），写进 `options` 字典。
- `extrude_prepared` 增加 `merge_gap_m: float = 0.0`；`pipeline.run_pipeline` 传入 `request.options.merge_gap_m`。
- 计算方式：`tolerance = max(contact_overlap_m, merge_gap_m)`；
  `grow = max(contact_overlap_m, merge_gap_m) / 2.0 + 0.05`（+0.05 保证是体积重叠而不是面相切，
  相切并集会得到非流形边）。
- 当 `merge_gap_m = 0` 时必须与现在**逐字节一致**（`tolerance` 仍是 `contact_overlap_m`，`grow` 仍是它的一半）。

**必须知道的副作用**：外扩是四边等距的，所以被合并的楼会整体"胖"一圈，
两侧街道各窄约 1 m（2 m 间距时外扩 1.05 m）。4 km 尺度下可接受，但小院子（5 m 见方）会被填掉。
如果用户不接受这一点，备选实现是"在近邻对之间加一根桥接柱"（连接体取两栋较矮者的屋顶高度），
只补间隙、不长胖——工作量大一些，先按外扩做，效果不满意再换。

## 测试

`tests/test_geometry.py` 已有 `prepare_buildings` / `extrude_prepared` 的夹具函数 `_feature(osm_id, tags, offset, size)`：

1. 两栋间距 1 m 的楼，`merge_gap_m=2` 时并集后是**一个连通实体**（用 `trimesh` 的连通分量或 STL 回读校验）；
2. 同样两栋，`merge_gap_m=0` 时仍是两个壳（保持现状）；
3. 间距 5 m 的楼在 `merge_gap_m=2` 下不合并；
4. `merge_gap_m=0` 的几何与改动前一致（可用三角形数/体积断言）。

另外补一条 CLI 测试：`--merge-gap 2` 能传到 `GenerateOptions.merge_gap_m`（照抄 `tests/test_cli.py`
里 `test_cli_parser_accepts_a_raised_height_ceiling` / `test_cli_passes_the_edge_taper_to_the_request` 的写法）。

## 重跑六城

参数（六份共用）：`--height 25 --level-height 3 --min-height 10 --embed 0.5 --ground-margin 0
--simplify 0.05 --min-area 4 --max-height 700 --merge-gap 2 --quiet`

| 城市 | bbox（south,west,north,east） | 额外参数 |
| --- | --- | --- |
| beijing | 39.893966,116.431577,39.930034,116.478423 | `--no-terrain` |
| shanghai | 31.218966,121.478988,31.255034,121.521012 | `--no-terrain --height-overrides docs\plans\2026-11-02-height-overrides-shanghai.json` |
| guangzhou | 23.101966,113.305465,23.138034,113.344535 | `--no-terrain --height-overrides docs\plans\2026-11-02-height-overrides-guangzhou.json` |
| shenzhen | 22.521966,114.035548,22.558034,114.074452 | `--no-terrain` |
| changsha | 28.177966,112.956615,28.214034,112.997385 | `--no-terrain` |
| chongqing | 29.541966,106.549345,29.578034,106.590655 | `--terrain-resolution 30 --terrain-edge-taper 250` |

输出到 `out\six-city\<city>`。OSM / DEM 已缓存，六份全跑约 1 分钟。

然后重建数据集并重绘预览：

```powershell
.\.venv\Scripts\python.exe scratch\build_city_cores.py          # out\six-city -> models\city_cores
$env:CITY_CORES_DIR = 'models/city_cores'
.\.venv\Scripts\python.exe models\city_cores\render_previews.py # 六张版式 + 两张总览
```

依赖 `.venv` 里的 numpy / Pillow；不需要 matplotlib。

## 另一项要一起改的：四周 50 m 缓冲

用户要求六份模型四周各留 50 m 空地。**不需要改代码**，`--ground-margin` 已经是现成参数
（`GenerateOptions.ground_margin_m`，0–500 m）：它把底面（平板或地形块的 base_bounds）
向外扩这个距离，建筑不动。

- 六城的参数从 `--ground-margin 0` 改成 `--ground-margin 50`。
- 实测（北京，`--no-terrain`）：底面 4223.7 × 4366.5 m → **4323.7 × 4466.5 m**，三角形数基本不变
  （67 168 → 67 162），也就是外圈只是一块空地。
- 意义：现在边缘建筑会直接顶到模型边界、模型最外一圈就是楼体墙面；加 50 m 后边界一定是平地，
  CFD 进口/出口不会撞在半栋楼上。
- 重庆会叠加：`base_bounds` 先 +50 m，地形网格再按 `--terrain-edge-taper 250` 向外扩 250 m，
  所以重庆的底面是「框 ∪ 建筑 + 50 m 平地 + 250 m 坡道」，比现在再大 100 m。
- 文档里 `footprint_km` 这类数字会跟着变，`models/city_cores/layout_parameters.json` 由
  `scratch/build_city_cores.py` 自动重算，不用手改。

## 当前六城的基准数字（v2.3.3，改动前）

| 城市 | 建筑 | 三角形 | 最高 | 底面 |
| --- | ---: | ---: | ---: | --- |
| 北京 国贸 CBD | 2949 | 67 166 | 528.0（中国尊） | 4.22 × 4.37 km |
| 上海 陆家嘴 | 3958 | 112 988 | 632.0（上海中心） | 4.23 × 4.17 km |
| 广州 珠江新城 | 2366 | 65 136 | 600.0（广州塔） | 4.15 × 4.18 km |
| 深圳 福田 | 2562 | 115 178 | 599.1（平安） | 4.11 × 4.22 km |
| 长沙 五一广场 | 5338 | 150 366 | 452.0（IFS） | 4.23 × 4.13 km |
| 重庆 解放碑 | 1539 | 116 730 | 363.0 | 4.63 × 4.56 km |

六份全部 `watertight = true`、`non_manifold_edges = 0`、`weld_repairs = 0`、**连通分量 1**。
合并会让三角形数下降（相邻墙面被并集消掉），这是预期，不算回归；但水密/单实体/非流形三条不能变。

## 验收

```powershell
.\.venv\Scripts\python.exe scratch\audit_six.py
```

它独立读回 `out\six-city\*\output\city.stl`，检查水密、绕向、非流形边、连通分量、退化面、重复面、
`patches.json` 区间覆盖是否覆盖每个三角形一次、三角形数与 metadata 是否一致。六份必须全 `PASS`。

## 要同步的文档

1. `docs/plans/2026-11-02-six-city-core-stl.md`：参数表加 `merge_gap_m` 一行、结果表更新三角形数、
   已知现象里说明合并的影响（多少栋被合并、街巷窄了多少）。
2. `models/city_cores/README.md`：表格里的三角形数、以及"高度/合并"约定。
3. `CHANGELOG.md`：新版本小节（建议 `2.4.0`，因为新增了公开参数），写清楚默认 0 = 现状。
4. `app/config.py` 的 `version` 同步。

## 环境坑（这个工作区踩过的，别再踩）

- **文件沙箱只认工作区相对路径**：绝对路径（包括 `F:\College\nudt\...` 和别的仓库）会被静默拒绝或返回空。
  一律用相对路径；曾经把成果复制到 `..\hxy-lbm-code\models\city_cores`，文件全部没落地。
- **写文件的工具会偶发"报成功但没写"**：`write` 之后用 `Test-Path` 确认；不可靠时改用
  `[System.IO.File]::WriteAllText($path, $text, [System.Text.UTF8Encoding]::new($false))`。
- **不要用 PowerShell `Set-Content` 改源码**：会把 UTF-8 写成 ANSI，中文注释变乱码（本项目踩过一次）。
  改 Python 用 `edit` 工具或 python 脚本。
- **控制台是 GBK**：`--quiet` 之后打印的摘要不能含 `²`、emoji 之类，否则 `UnicodeEncodeError`
  让已经成功的任务以退出码 1 结束（`tests/test_cli.py::test_summary_prints_on_a_gbk_console` 守着这条）。
- **PIL 保存大 PNG 偶发 `OSError: EINVAL`**：`models/city_cores/render_previews.py` 里的 `save_png()`
  已经做了"写临时名 + 重试 + replace"，改渲染脚本时别把它删掉。
- `models/`、`out/`、`data/`、`scratch/` 都在 `.gitignore` 里；`models/city_cores` 是本地交付物，不入库。
- git 提交时会有 `LF will be replaced by CRLF` 警告，正常。

## 纽约 / 洛杉矶（另一窗口在跑）

拿到那两份 bbox 后，用同一套口径（`--min-height 10 --max-height 700 --merge-gap 2`），并注意：

- **建筑数会撞上限**：`CITYGEN_MAX_BUILDINGS` 默认 20 000，美国城市 OSM 覆盖密，4×4 km 曼哈顿
  很可能超过。先用 `scratch/census.py count --ids ...`（或直接跑一次看报错）确认，必要时用环境变量
  调高，例如 `$env:CITYGEN_MAX_BUILDINGS = '40000'`。
- **地标高度**：世贸一号 541 m、帝国大厦 443 m、Wilshire Grand 335 m；不要指望 OSM 都有 `height` 标签，
  没有的按 `docs\plans\2026-11-02-height-overrides-*.json` 的格式补一份覆盖文件。
- **地形**：曼哈顿与洛杉矶市中心都按平地处理（`--no-terrain`），和现有五份平原城市一致。