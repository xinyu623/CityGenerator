# 东京 / 旧金山核心区 4×4 km 城市模型 —— 参数方案

**目标：** 在 `models/city_cores` 现有十份（中国六城 + 纽约 / 洛杉矶 / 伦敦 + 香港）
之外，增加东京与旧金山两份 `city_<id>_core.stl`，使数据集扩到 12 城。口径沿用现有
数据：4×4 km、米制、+X 东 / +Y 北 / +Z 上、地面 z = 0、底面 z = −1、水密单一连通
实体、`--origin-zero`、`--density-cell 100 --density-max 8`、`--ground-margin 50`。

**状态：** 2026-11-05 执行完成。东京一份平地，旧金山一份真实地形（参照重庆）。

## 一、框选

| 城市 | 中心 | bbox（south,west,north,east） | 框内 | 选它的理由 |
|---|---|---|---|---|
| 东京 | 35.6640, 139.7440 | 35.645966,139.721886,35.682034,139.766114 | 虎之门、麻布台、六本木、东京塔、银座、霞关 | 三个候选框里 ≥150 m 最多（54 栋），最高 325.2；丸之内 43 栋 / 265.8，新宿 23 栋 / 234 且只有单簇 |
| 旧金山 | 37.7890, −122.4127 | 37.770966,−122.435434,37.807034,−122.389966 | 金融区、SoMa、Embarcadero、Nob Hill、Russian Hill、Telegraph Hill、Civic Center | 一个框同时装下 Salesforce 326 / Transamerica 260 / 181 Fremont 244 与三座山丘；东缘留 0.3–0.5 km 海湾，保证天际线完整 |

东京的候选数据来自 `scratch/global_census.py` 的普查（`tky-toranomon` 18 692 栋 /
54 栋 ≥150 m / 最高 332.6 / DEM 67 m）。旧金山的 DEM 用 `scratch/dem_check.py` 比较了
五个中心，选了 `sf-a`（起伏 140.9 m、边框 99.4 m、平面残差 rms 20.6 m、57% 的格点
偏离平面 >10 m），因为它比更靠东的框少 1/3 的海面，又比更靠西的框保住了 Salesforce。

## 二、数据来源：Overpass 不可用，改走 BBBike PBF

2026-11-05 三个公共 Overpass 端点对 2×2 km 分块全部 read timeout（probe 结果：
overpass-api.de / kumi / private.coffee 均 30 s 超时），所以两城都改离线：

- 下载 BBBike 城市抽取 `SanFrancisco.osm.pbf`（33 MB）与 `Tokyo.osm.pbf`（92 MB）；
- `scratch/cache_city_from_pbf.py`（`hk_cache_from_pbf.py` 的通用版）解析 PBF，先按
  目标框过滤 way，再按管线缓存键 `sha256("buildings-v1|<chunk bbox>")` 写 4 个
  2×2 km 缓存块；
- 之后原样跑 `python -m app.cli`，产物与走 Overpass 时同一条代码路径。

东京 PBF 有 1,502,344 条 building way；先做一次 1,000 万级的向量化 bbox 预筛，只保留
框内 18,803 条 + 关系成员，避免把 150 万条 way 全部建成几何。旧金山 17,528 条在框内。
关系成员全部保留（多边形的完整性），否则 multipolygon 会缺边。

因为当前沙箱对 `data/cache/osm` 的写入被 Windows 权限挡住，本次缓存写在
`out/global-cache/`，命令统一加 `--data-dir out/global-cache`。换一台机器可以直接写回
`data/cache/`。

## 三、参数

| 参数 | 东京 | 旧金山 | 与现有海外城 |
|---|---|---|---|
| 范围 | 4.000 × 4.000 km | 同 | 同 |
| 默认高度 / 层高 / 下沉 | 25 / 3 / 0.5 m | 同 | 同 |
| 最低高度 | 10 m | 10 m | 同 |
| 最高高度 | 600 m | 600 m | 同（纽约/洛杉矶/伦敦也是 600） |
| 轮廓简化 | 0.2 m | 0.2 m | 同 |
| 地面外扩 | 50 m | 50 m | 同 |
| 密度上限 | 100 m / 8 栋 | 同 | 同 |
| 地形 | **关**（1 m 平板） | **开**，30 m，250 m 缓冲 | 仅重庆开 |
| 高度抽样 | 开，抖动 10% | 开 | 同 |
| 保守高度约束 | **开**，k = 8 | 关 | 仅东京开 |
| 建筑数上限 | 30 000 | 30 000 | 同 |

东京开保守约束是这次唯一的口径偏离。东京 89.7% 的建筑没有 `height` /
`building:levels`，已知高度池里 100 m+ 的样本占比偏高，面积/类型加权抽样会"猜"出
一片假塔：不开约束时抽样建筑里 27 栋 >250 m、170 栋 >100 m；开约束后 1 栋 / 83 栋。
带标签的真实塔楼不受影响，最高仍是麻布台之丘 325.2 m。

**东京塔的"奇怪柱子"**：OSM 把东京塔画成 36 个 `building=yes` / `building=roof`
小轮廓，高度 40–332.6 m、面积 9–121 m²，全部叠在 35.6586,139.7454 同一点上。棱柱
挤出后就是 227 / 241 / 250 / 251 m 四根细柱 + 301 / 311 m 的塔身段 + 一堆 40 m
杆，在 4 km 预览里非常显眼。它们的 `min_height` 也没有支撑楼体，`FLOATING_BASE`
会把基座落到地面，于是柱子从 z = 0 直通到 250 m。处理办法：把这 36 个 id 统一
`height_overrides` 到 10 m（`scratch/tokyo_height_overrides.json`），密度上限后留下
11 个低矮基座，塔位不再参与天际线。没有改 `man_made=tower` 的读取逻辑——这类
lattice 塔本来就不该按建筑棱柱建模。

旧金山的地形判据与重庆一致：DEM 起伏 140.9 m，是 Salesforce（326 m）的 43%，且
Nob / Russian / Telegraph Hill 是城市形态本身，不是 DSM 在塔楼上的抬升。边框最高
99.4 m，所以必须开 `--terrain-edge-taper 250`，否则计算域入口是一堵 99 m 的墙。
底面因此从 4.0 km 涨到 4.78 × 5.09 km，三角形 587 k，是全集最大的一份。

**旧金山地形基准要知道：** DEM 最低点是海湾水下 −32.8 m，`build_terrain_grid` 按
"最低格点定基准"把陆地整体抬到 z = 0 以上：市中心约 37 m（Salesforce Tower 的
`ground_m`）、山丘最高 108 m，水面仍是 z = 0 的平地。这是基准约定的直接结果，不是
城市真的建在 37 m 高的台上。若 CFD 在意岸线那一道台阶，应把海湾裁出框外或增加
"海平面基准"选项，而不是关地形。

## 四、结果

| 城市 | 建筑 | 三角形 | 最高 | 底面 | STL | 体积 |
|---|---:|---:|---|---|---:|---:|
| 东京 | 7,817 | 206,346 | 325.2（麻布台之丘） | 4.31 × 4.34 km | 10.3 MB | 195.7 M m³ |
| 旧金山 | 8,793 | 587,384 | 326.0（Salesforce Tower） | 4.78 × 5.09 km | 29.4 MB | 1,332.1 M m³ |

高度来源：东京 `tag` 335 / `levels` 788 / 抽样 6,683 / override 11；旧金山 `tag` 6,610 /
`levels` 342 / 抽样 1,841。密度上限：东京裁掉 10,698 栋（100 m 格子比伦敦还满），
旧金山裁掉 7,866 栋。

旧金山地形块 56,378 个三角形；`STEEP_FOOTPRINT` 4,809 栋（55%），最大轮廓内高差
49.2 m；`HEIGHT_CLAMPED` 3,955（多数是低于 10 m 的坡地住宅被抬到下限）。

## 五、验收

`scratch/audit_six.py` 已扩到 12 城并独立读回 STL（不信任 metadata 自述）：

| 检查项 | 结果 |
|---|---|
| 水密 / 绕向一致 | 12 份全部 true |
| 非流形边 | 0 |
| 连通分量 | 每份 1 个实体 |
| `patches.json` 区间 | 连续、无空洞、逐三角形覆盖一次 |
| 原点 | 12 份最小 X/Y 都是 0，最小 z 都是 −1.0 |
| 元数据一致 | 三角形数、体积与 STL 读回值一致 |
| 总判定 | **12/12 PASS** |

东京：`INVALID_GEOMETRY 41`、`MISSING_HEIGHT 16,658`、`HEIGHT_CLAMPED 418`、
`DENSITY_THINNED 10,698`、`FLOATING_BASE 8`、`OVERLAP_DETECTED 75`。
旧金山：`INVALID_GEOMETRY 8`、`MISSING_HEIGHT 4,068`、`HEIGHT_CLAMPED 3,955`、
`DENSITY_THINNED 7,866`、`STEEP_FOOTPRINT 4,809`、`OVERLAP_DETECTED 209`。

## 六、已知局限

- **东京塔按 10 m 基座处理**：OSM 的 36 个塔身/顶棚轮廓被统一 override 到 10 m
  （`scratch/tokyo_height_overrides.json`），塔位不参与天际线；东京最高因此是
  麻布台之丘 325.2 m。没有建模 lattice 塔身。
- **东京天际线是估计值**：85.5% 的建筑靠抽样，保守约束只是压掉假超高层，不能把
  缺失的高度标签变成实测。
- **旧金山地形基准**：海湾水下 −32.8 m 抬升了整片陆地；水面不建模，岸线是落差台阶。
- **旧金山坡地切楼**：55% 的轮廓内高差 >3 m，坡上的楼被地形切掉一部分，是山城的
  预期表现。
- **水体不建模**：旧金山框东缘 0.3–0.5 km 海湾按平地处理；东京框内没有大面积水面。
- **密度上限按质心归属**：跨格的大楼只计入质心那一格，边界密区会有一点裁多/裁少。
- **东京塔类构筑物**：`man_made=tower`、`building:part` 不进入模型；生成器只读
  `building=*` 的外轮廓。

## 七、运行

```powershell
# 1) 下载 PBF（一次性）
#    https://download.bbbike.org/osm/bbbike/SanFrancisco/SanFrancisco.osm.pbf
#    https://download.bbbike.org/osm/bbbike/Tokyo/Tokyo.osm.pbf

# 2) 从 PBF 建缓存（当前沙箱把 data/cache 挡住，所以写到 out/global-cache）
$env:CITYGEN_DATA_DIR = "out\global-cache"
python scratch\cache_city_from_pbf.py --pbf scratch\pbf\Tokyo.osm.pbf `
  --bbox "35.645966,139.721886,35.682034,139.766114" --label bbbike/tokyo
python scratch\cache_city_from_pbf.py --pbf scratch\pbf\SanFrancisco.osm.pbf `
  --bbox "37.770966,-122.435434,37.807034,-122.389966" --label bbbike/san-francisco

# 3) 生成东京（平地 + 保守高度）
$env:CITYGEN_MAX_BUILDINGS = "30000"
python -m app.cli --bbox "35.645966,139.721886,35.682034,139.766114" `
  --out out\global-four\tokyo --height 25 --level-height 3 --embed 0.5 `
  --ground-margin 50 --simplify 0.2 --min-area 4 --min-height 10 --max-height 600 `
  --origin-zero --density-cell 100 --density-max 8 --no-terrain --conservative-heights `
  --height-overrides scratch\tokyo_height_overrides.json `
  --no-proxy --quiet --data-dir out\global-cache

# 4) 生成旧金山（地形 + 250 m 缓冲）
python -m app.cli --bbox "37.770966,-122.435434,37.807034,-122.389966" `
  --out out\global-four\san_francisco --height 25 --level-height 3 --embed 0.5 `
  --ground-margin 50 --simplify 0.2 --min-area 4 --min-height 10 --max-height 600 `
  --origin-zero --density-cell 100 --density-max 8 --terrain-resolution 30 `
  --terrain-edge-taper 250 --no-proxy --quiet --data-dir out\global-cache

# 5) 装配 + 重绘
python scratch\build_city_cores.py
python models\city_cores\render_previews.py
python scratch\audit_six.py
```

注意：`build_city_cores.py` 现在也装配香港（`out/hk-gen`，没有 per-city 子目录），
否则重新生成清单会把香港那一条丢掉。
