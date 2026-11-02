# CityGenerator

从 OpenStreetMap（OSM）建筑轮廓生成可直接导入室外风环境 CFD 软件
（OpenFOAM、STAR-CCM+、Fluent 等）的**单一密闭城市实体 STL**。

本项目是一个本地 FastAPI 服务，附带浏览器界面：

1. 在 OpenStreetMap 底图上缩放、平移，拖拽框选区域。
2. 设置默认建筑高度（默认 **25 m**）、层高、地形选项和几何清理参数。
3. 后端通过 Overpass 获取 OSM 建筑轮廓，投影到本地米制坐标系，把 DEM 高程
   采样成实体地形块，在真实地形上拉伸闭合建筑壳体，并将所有几何融合为
   **一个水密的 `city.stl`**。
4. 下载 ZIP，或在内置 Three.js 查看器中检查结果。

所有几何都在本地生成。OSM 建筑轮廓和 DEM 高程瓦片是仅有的外部输入，都会
缓存到磁盘。

[English README](README.md)

![CityGenerator 界面](docs/images/frontend.png)

内置查看器把融合后的城市呈现为暖白色建筑模型，放置在展示底座上，带柔和
的棚拍光照；支持左键旋转、中键平移、滚轮缩放。查看器用单一材质显示融合后的
STL，不再额外区分建筑和地面。它加载一个仅供显示的预览网格：由干净的建筑
壳体和平整底座（开启地形时为地形块）组成；该预览不会列在任务文件里，也不
进入 ZIP。查看器使用对数深度缓冲，避免大面积地面在低角度观察时产生
z-fighting。导出的 `city.stl` 仍然是唯一的 Z 轴向上 CFD 网格。

![CityGenerator 三维预览](docs/images/preview.png)

## 快速开始

环境要求：Python 3.11+；首次 Overpass 请求、首次 DEM 瓦片请求以及底图/
Three.js CDN 资源需要联网。

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000/>，点击 **框选区域**，依次点击矩形对角两点，
调整参数后点击 **生成 STL**。

界面为中文；代码、注释、元数据字段和英文 README 使用英文。中文说明即本文
件。

### 命令行（无需启动服务）

后端流水线本身不依赖 FastAPI。`pip install -e .` 之后可以直接：

```powershell
citygen --bbox 29.560551,106.58197,29.569449,106.592431 --out .\out\chaotianmen
```

未安装命令行脚本时：

```powershell
.\.venv\Scripts\python.exe -m app.cli --bbox 29.560551,106.58197,29.569449,106.592431 --out .\out\chaotianmen
```

`--bbox` 顺序为 `south,west,north,east`；其余参数与请求选项一致：
`--height`、`--level-height`、`--embed`、`--ground-margin`、`--simplify`、
`--min-area`、`--terrain-resolution`、`--no-terrain`、`--refresh`、
`--data-dir`（缓存根目录）、`--quiet`。输出与网页任务相同（`<out>/output/`
下的 `city.stl` 等五个文件）；失败时返回非零退出码并打印错误码，方便批处理调用。

## 输出内容

任务完成后包含：

| 文件 | 内容 |
| --- | --- |
| `city.stl` | 二进制 STL。一个融合、闭合、法线朝外的城市实体：全部建筑 + 地形块（关闭地形时为 1 m 厚平整底座）。 |
| `metadata.json` | 模式版本、本地原点、CRS、参数、地形来源、统计、警告、数据来源和城市实体校验结果。 |
| `buildings.geojson` | WGS84 建筑轮廓，含高度、地形地面高程、屋顶高程、来源和筛选后的标签。 |
| `README.txt` | 简短说明和 CFD 导入建议。 |
| `result.zip` | 包含上述四个文件的压缩包（不包含自身）。 |

**不再分别输出 `buildings.stl` 和 `ground.stl`**。城市实体按一个闭合障碍物
导入。

### 城市实体如何融合

1. 先一次性完成轮廓投影、清理和高度解析，因此生成地面之前就能确定全部建筑
   的完整范围。
2. 在框选范围与全部建筑轮廓的并集下方生成地形块，并向外扩展
   `ground_margin_m`。地形块顶面是 DEM 高度场，侧面是竖直墙，底面是
   `z = -ground_slab_thickness_m` 的平面，天然闭合。关闭地形时改用 1 m 厚
   平整底座。
3. 每栋有效建筑拉伸为闭合壳体，落在其轮廓范围内地形最低的那个格点上，再向
   下嵌入 `0.5 m`。
4. 使用 [Manifold](https://github.com/elalish/manifold) 对建筑壳体和地形块做
   布尔并集，内部面会被消除；于是山坡会自然地切出每栋楼的地面线。
5. 如果两栋建筑只在一条边或一个角点接触，它们的体积没有公共内部，并集在
   几何上就是非流形的。流水线会在 STL 回读后定位这些具体的边，把相邻的
   坏边聚类，并在每簇周围加入一个带 `0.4 m` 余量的实体“焊接盒”，再重新
   做并集。补丁数量记录在 `city.weld_repairs`。
6. 写出的 STL 会被重新读入，必须通过水密、法线一致、顶点有限、体积为正、
   非流形边为零的检查。失败会返回 `CITY_UNION_FAILED`；不会静默退化为
   简单拼接的三角面片。

大多数城市模型不需要补丁，或只需要少量补丁。它们只修复局部接触，不改变
整体建筑布局。

**为什么不能只在底部加一个平面？** 底座下表面本来就是闭合的；失败发生在
底座以上，两栋建筑的体积只沿一条竖直线接触、没有公共内部。底部平面无法
把这种线接触变成流形表面。要让整个模型成为单一闭合实体，必须在接触处
补一点材料（或者留出缝隙把两栋楼分开）；焊接盒补的就是这一点材料。

### 地形

地形默认开启，数据来自
[AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/)（Terrarium）
公开数据集：256×256 PNG 瓦片，`米 = R*256 + G + B/256 - 32768`，由
SRTM/GMTED 派生，无需 API key。这份数据是约 30 m 的**地表模型**而不是裸地：
在密集城区会贴着屋顶和树冠，个别格子会偏出几十米。程序因此在 30 m 本地网格
（与原始数据同间距）上采样，并做约 150 m 的中值滤波
（`metadata.json → terrain.smoothing_m`），滤掉这种噪声而保留真实地形。

- `terrain.resolution_m`（5–50 m）控制地形网格间距。为控制三角形数量，超出
  `max_terrain_cells` 时会自动加粗并给出警告。
- 平滑是刻意的：不做平滑时，一个错误的 SRTM 格子会在模型里变成一个地面根本
  不存在的百米土丘。实测 250 m 的山体只损失不到 3% 的起伏，平坦江边地块保持平坦。
- 滤波宽度按地面米数固定，所以把 `resolution_m` 调细只会多出三角形，不会多出
  信息——原始数据在 30 m 以下本来就没有细节。
- 垂向基准取**所选范围内最低的地形格点**，所以 `z = 0` 仍是 CFD 地面。
  `metadata.json → terrain.datum_offset_m` 是恢复 DEM 绝对高程需要加上的值。
- 每栋建筑落在其轮廓内地形最低点，因此不会悬空。坡地上坡侧会被埋入；轮廓
  范围内高差超过 3 m 的建筑会触发 `STEEP_FOOTPRINT` 警告。
- DEM 瓦片缓存在 `data/cache/dem/`，TTL 与 OSM 缓存一致（7 天）。DEM 请求
  失败会让任务以 `TERRAIN_FETCH_FAILED` 失败，而不会悄悄退化成平地。

把 `terrain.enabled` 设为 `false` 即可回到 v1 的平整地面行为。

### 坐标约定

- 单位：米。
- `+X` = 东，`+Y` = 北，`+Z` = 上（STL 为 Z-up）。
- 原点：所选 WGS84 包围盒的西南角，投影到合适的 UTM 带
  （北半球 `EPSG:326xx`，南半球 `EPSG:327xx`）。
- `z = 0` 是所选范围内最低的地形格点（关闭地形时为地面平面），因此模型底部
  从 `-ground_slab_thickness_m` 开始。
- `metadata.json → local_frame.origin` 记录原点的东坐标、北坐标、经度和
  纬度，便于重新地理配准。

`metadata.json → city` 的主要字段：

```json
{
  "triangle_count": 28346,
  "watertight": true,
  "winding_consistent": true,
  "volume_m3": 18294326.099,
  "non_manifold_edges": 0,
  "base_thickness_m": 1.0,
  "terrain_triangle_count": 20406,
  "union_engine": "manifold",
  "weld_repairs": 8
}
```

`metadata.json → terrain` 的主要字段：

```json
{
  "enabled": true,
  "source": "aws-terrarium",
  "zoom": 13,
  "pixel_m": 16.84,
  "resolution_m": 29.38,
  "grid": { "nx": 37, "ny": 40 },
  "smoothing_m": 146.9,
  "filter": "median 5x5 cells",
  "datum_offset_m": 18.69,
  "elevation_min_m": 18.69,
  "elevation_max_m": 56.89,
  "relief_m": 38.19,
  "max_footprint_relief_m": 8.9,
  "tiles": 2,
  "cached_tiles": 0,
  "nodata_pixels": 0,
  "nodata_filled_cells": 0,
  "z_reference": "lowest terrain cell in the selection"
}
```

## CFD 导入建议

1. 把 `city.stl` 作为**一个**闭合障碍物或实体导入，不再需要拼合两个 STL。
2. 确认求解器按米读取文件，且 `+Z` 向上。如果求解器使用 Y-up，绕 X 轴
   旋转 `-90°`。
3. 在城市周围建立流体域：入口、出口、侧面和顶部。建议初始距离为上游
   `5H`、下游 `10–15H`、两侧 `5H`、顶部 `5H`，其中 `H` 为最高建筑高度。
4. 地面已经包含在实体中，不需要再封闭开放的地面片；地形块下表面本身就是
   闭合边界。开启地形后地面不再平整，坡地上的建筑会被山体切掉一部分，这是
   预期的障碍物形状，但会增加近地面网格量。
5. 检查 `MISSING_HEIGHT`、`HEIGHT_CLAMPED` 和 `STEEP_FOOTPRINT` 警告。缺少
   高度标签的建筑会从本区域 OSM 已知高度中随机取值（`buildings.height_sampling`），
   天际线更贴近本地实际，但仍然是估计值，不是实测高度。
6. 如需了解是否有建筑轮廓发生接触修复，查看 `city.weld_repairs`；正常结果应为 `0`。
7. 如果网格工具处理地形块较吃力，可关闭地形重跑，或把
   `terrain.resolution_m` 调粗；平整底座的三角形数少得多。
8. 本工具只输出表面 STL，不生成体网格、边界层、湍流参数或求解器算例。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 存活检查和版本。 |
| `GET` | `/api/config` | 限制、默认值、数据署名、底图地址。 |
| `POST` | `/api/jobs` | 创建生成任务，返回 `202` 和 `job_id`。 |
| `GET` | `/api/jobs/{job_id}` | 状态、进度、阶段、统计、警告和文件列表。 |
| `GET` | `/api/jobs/{job_id}/download` | 下载 `result.zip`（仅已完成任务）。 |
| `GET` | `/api/jobs/{job_id}/preview.stl` | 仅用于查看器的显示预览网格。 |
| `GET` | `/api/jobs/{job_id}/files/{name}` | 下载单个输出文件。 |
| `DELETE` | `/api/jobs/{job_id}` | 取消运行中的任务，或删除已结束任务。 |

请求示例：

```json
{
  "bbox": { "south": 28.19, "west": 112.97, "north": 28.196, "east": 112.978 },
  "options": {
    "default_height_m": 25,
    "level_height_m": 3,
    "embed_depth_m": 0.5,
    "ground_margin_m": 0,
    "simplify_m": 0.05,
    "min_building_area_m2": 4,
    "terrain": { "enabled": true, "resolution_m": 30 }
  },
  "refresh": false
}
```

`bbox` 必须满足 `north > south` 且 `east > west`。`refresh` 会跳过该包围盒
的 Overpass 缓存。`terrain.resolution_m` 取值 5–50；调粗可减少三角形，调细
会更贴近 DEM，但受原始分辨率和 `max_terrain_cells` 限制。

进度阶段：`queued`、`starting`、`validating`、`fetching`、`parsing`、
`projecting`、`building`、`terrain`、`ground`、`fusing`、`exporting`、
`completed`（以及 `failed`、`cancelling`、`cancelled`）。界面约每秒轮询一次。

地形失败会明确报告：`TERRAIN_FETCH_FAILED`（瓦片下载失败）和
`TERRAIN_DATA_MISSING`（该范围没有可用高程）。可稍后重试，或把
`terrain.enabled` 设为 `false` 重新生成。

## 配置

配置使用 `CITYGEN_` 前缀，可写入 `.env`：

```dotenv
CITYGEN_DATA_DIR=./data
CITYGEN_MAX_AREA_KM2=17
CITYGEN_OVERPASS_CHUNK_AREA_KM2=4
CITYGEN_MAX_BUILDINGS=20000
CITYGEN_MAX_WORKERS=2
CITYGEN_OVERPASS_ENDPOINTS=https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter
CITYGEN_OVERPASS_TIMEOUT_S=180
CITYGEN_OVERPASS_MIN_INTERVAL_S=1.0
CITYGEN_JOB_TTL_DAYS=7
CITYGEN_CACHE_TTL_DAYS=7
CITYGEN_GROUND_SLAB_THICKNESS_M=1.0
CITYGEN_CONTACT_OVERLAP_M=0.1
CITYGEN_HEIGHT_SAMPLING_ENABLED=true
CITYGEN_HEIGHT_SAMPLING_MIN_SAMPLES=5
CITYGEN_HEIGHT_SAMPLING_JITTER=0.1
CITYGEN_HEIGHT_SAMPLING_SEED=0
CITYGEN_TERRAIN_ENABLED_DEFAULT=true
CITYGEN_TERRAIN_RESOLUTION_M=25
CITYGEN_MAX_TERRAIN_CELLS=80000
CITYGEN_DEM_ZOOM_MIN=10
CITYGEN_DEM_ZOOM_MAX=14
CITYGEN_DEM_MAX_TILES=24
CITYGEN_DEM_TIMEOUT_S=20
CITYGEN_DEM_MAX_RETRIES=3
```

主要默认值：

- 最大框选面积：`17 km²`（正好 4 × 4 km 在椭球面上是 16.005 km²），按
  `overpass_chunk_area_km2 = 4 km²` 分块请求后合并。
- 最大建筑数：`20,000`。
- 默认高度：`25 m`（仅当本区域已知高度太少时使用；否则缺失高度从本地 OSM
  高度分布中随机取值，抖动 `10 %`）；层高：`3 m`。
- 底部下沉深度：`0.5 m`；轮廓简化：`0.05 m`。
- 地面底座厚度：`1 m`。
- 地形：默认开启，网格 `30 m`，`150 m` 中值平滑，最多 `80,000` 个格点，
  DEM 缩放级别 `10–14`。
- 任务保留 / Overpass 与 DEM 缓存保留：`7 天`。
- 工作线程：`2`。

缓存和任务位于 `data/cache/osm/`、`data/cache/dem/` 与
`data/jobs/<job_id>/`，均已被 git 忽略。

## 开发

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check app tests
```

测试默认离线：Overpass 与 DEM 数据源都替换为合成 fixture。两个可选的真实
冒烟测试标记为 `network`，默认跳过：

```powershell
$env:CITYGEN_RUN_NETWORK = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_live_overpass.py tests/test_live_dem.py -q -m network
```

测试套件包含一个“建筑仅边接触”的 STL 回读回归测试：融合后的城市在写成
二进制 STL 并重新读入后，仍必须水密且非流形边为零。地形测试覆盖平面/对角
采样、轮廓最低点保守取值、闭合地形实体、坡地建筑的并集水密性，以及
Terrarium 瓦片解码与拼接。

## 限制（v2）

地形是从约 30 m 分辨率 DEM 重采样并平滑出的 30 m 网格：不是测绘成果，没有
陡坎、建筑和植被细节，也不会切出河道。不支持道路、植被、水体、坡屋顶、
`building:part` 或体网格。每栋建筑都是竖直棱柱，落在其轮廓内的最低格点上；
接触的建筑轮廓通过局部焊接融合，而不是合并成共享的建筑细节。多用户认证、
重启后的队列持久化和分布式 worker 不在范围内。

## 数据许可与署名

OSM 数据 © OpenStreetMap contributors，采用
[ODbL 1.0](https://www.openstreetmap.org/copyright) 许可。地形数据来自
[AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/) 公开数据集
（Mapzen/Amazon，由 SRTM 等公开 DEM 派生）。署名信息嵌入 `metadata.json`、
`README.txt`、界面和本文档。由 OSM 派生的几何仍受 ODbL 署名/相同方式共享
要求约束。

前端库：[Leaflet](https://leafletjs.com/)（BSD-2-Clause）和
[Three.js](https://threejs.org/)（MIT），从 jsDelivr 加载。底图瓦片由
OpenStreetMap 瓦片服务器提供。
