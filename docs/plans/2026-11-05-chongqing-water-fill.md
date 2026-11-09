# 重庆江面补平 —— 参数与实现

**目标：** 重庆是数据集里第一份真实地形（渝中半岛），但 DEM 把长江、嘉陵江的河床一起
刻了进来，4 km 预览里是两条下切的深槽。上海那份是平地，江面就是 z = 0 的平板；这次
把重庆的江面也按同一读法补平，同时保留两岸的山地地形。

**状态：** 2026-11-05 执行完成。

## 一、为什么不是关地形

关掉地形会把渝中半岛 159.8 m 的高差一起拍平，重庆就退化成另一份"上海"。要补的只是
水面以下那部分：把 DEM 低于水面的格子抬到水面，河床不再下切，水面成为 z = 0 的
平面；水面以上的山坡、台地、建筑基座都不动。

## 二、实现

新增 `TerrainOptions.water_level_m` / `--terrain-water-level`：

- 值是**绝对 DEM 高程**（米，海拔），不是相对当前 datum 的高度；
- `build_terrain_grid` 在平滑之后、datum 平移之前，把 `heights < water_level` 的格子
  抬到 `water_level`；
- 水面因此成为新的最低格点，`z = 0` 即水面；山坡仍从水面往上长；
- 记录 `terrain.water_level_m`、`terrain.water_filled_cells`，并给出
  `TERRAIN_WATER_FILLED` 警告；
- 新增 `water_fill_islands` / `--terrain-water-fill-islands`：水面以上的陆地块里，
  不接触选区边界、完全被水面包围的（沙洲/小岛）压到水面；记录
  `water_islands_filled` 和 `TERRAIN_WATER_ISLANDS_FILLED`；
- 新增 `water_bank_clearance_m` / `--terrain-water-bank-clearance`：把
  `water_level + clearance` 以下的岸坡/半岛格点也压到水面；记录
  `water_banks_cleared` 和 `TERRAIN_WATER_BANKS_CLEARED`。这是为了清掉与岸线
  相连、无法按"封闭小岛"判定的低滩；
- 默认 `None` / `False` / `0`，不改变现有城市的行为。

## 三、水位取 175 m

重庆段长江/嘉陵江的常水位在 163–168 m 量级，但 165 m 的常水位线在预览里仍留下
大片灰色低滩/沙洲（它们是 165 m 以上的地形，不是水）。这次按"把水面视为地面"的
要求，取 **175 m 的河漫滩线**，并把水面以上 **10 m** 内的低岸/半岛也一起压平
（`--terrain-water-bank-clearance 10`，即 185 m 等高线以下都算水面）。代价是江面
变宽、部分低岸被当成水面。

最终地形块（159 × 157，含 250 m 缓冲）的填充格点：

| 水位 | 低于该水位的格点 | 说明 |
|---|---:|---|
| 165 m | 5,210 | 常水位，低滩仍露出 |
| 170 m | 6,869 | 低滩大部分淹没 |
| **175 m** | **7,747** | 河漫滩线，再加 10 m 岸坡清理 |

## 四、结果

| 指标 | 补平前 | 补平后（175 m + 10 m 岸坡清理） |
|---|---:|---:|
| 地形起伏 | 183.939 m | **159.782 m** |
| datum / 最低格点 | 150.842 m | **175.000 m** |
| 低于水面的格点 | — | **7,747**（含平滑/边缘带） |
| 压平的低岸/半岛格点 | — | **8,676**（185 m 以下） |
| 封闭沙洲/小岛 | — | **0**（已被 10 m 清理覆盖） |
| 三角形 | 117,372 | **117,522** |
| 体积 | 1.343 B m³ | **0.946 B m³** |
| 江面 | DEM 河床，下切 | **z = 0 平面** |

建筑数 1,478、最高 363.0 m、底面 4.74 × 4.66 km 不变。补平后 `watertight = true`、
`non_manifold_edges = 0`、单一连通实体；12 城审计仍 **12/12 PASS**。

## 五、要知道的代价

- 水面仍是 **175 m**，但岸线取到 **185 m 等高线**：175–185 m 的低岸/半岛被压到
  水面，形成约 10 m 的岸坡台阶；不是 OSM 的真实水涯线，也不是常水位。
- 我查过 Geofabrik 的重庆 PBF：长江/嘉陵江在 OSM 里只有 `waterway=river` 中心线，
  没有 `natural=water` 水面多边形（框内只有 4 个水塘），所以做不了严格的水涯线掩膜。
- 水中沙洲/小岛按"完全被水面包围、且不接触选区边界"判定后压平；与岸线相连的
  半岛靠 10 m 岸坡清理处理。若以后要保留真实江心岛，关掉
  `--terrain-water-fill-islands` / 减小 `--terrain-water-bank-clearance` 即可。
- 江面是**平地**，不是水体：和上海、纽约、伦敦的江/河一样，CFD 里是可通行地面。
- 重庆 92% 的建筑轮廓内高差仍超过 3 m（`STEEP_FOOTPRINT` 1,359），坡地切楼不变。

## 六、运行

```powershell
python -m app.cli --bbox "29.541966,106.549345,29.578034,106.590655" `
  --out out\six-city\chongqing --height 25 --level-height 3 --embed 0.5 `
  --ground-margin 50 --simplify 0.05 --min-area 4 --min-height 10 --max-height 700 `
  --origin-zero --density-cell 100 --density-max 8 `
  --terrain-resolution 30 --terrain-edge-taper 250 --terrain-water-level 175 `
  --terrain-water-fill-islands --terrain-water-bank-clearance 10 --quiet
```

装配与预览：

```powershell
python scratch\build_city_cores.py
python models\city_cores\render_previews.py
python scratch\audit_six.py
```
