# baidu-streetview-downloader

> 根据采样点**批量下载百度地图街景图片**的命令行工具。默认走百度全景逆向接口（**无需 AK**），支持按采样点下载左右前后四个方向的照片，或直接下载完整的 360° 全景图。


## 功能特性

- **双输入**：既可从路网（shp/gpkg/geojson / 离线 pbf / Overpass）生成采样点，也可直接喂一个 `name,lng,lat` 的 CSV。
- **两种下载方式**：逆向接口（默认，无需 AK，高清全景）＋ 官方全景静态图 API（备选，本服务属于百度地图高级权限，需付费开通）。
- **方向可配**：`--mode` 选择左/右/前/后四方向照片，或完整 360° 全景图，或只下左右/前后。
- **裁剪可配**：`--crop-bottom` / `--crop-top` 裁掉全景底部采集车顶、顶部天空。
- **稳健下载**：多 AK 轮换（官方 API）、请求限速、断点续跑、就近搜索兜底、错误码分类重试。
- **互动地图**：`map` 生成网页地图，点击采样点查看街景照片（可全屏放大），无照片的采样点灰色区分；自动叠加路网/研究范围图层（可勾选显示/隐藏）。
- **两种运行方式**：`run` 一步跑完采样→下载→地图（配置驱动），或 `sample`/`download`/`map` 三条命令分步跑。

## 安装

要求 Python 3.10+，建议使用虚拟环境。

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install .
```

安装后即可使用 `baidu-pano` 命令（也可用 `python -m baidu_pano`）。

## 配置

密钥有两种方式提供（**推荐用 `keys.txt`，字段=值，不提交真实密钥**）：

```bash
cp configs/keys.txt.example configs/keys.txt   # 然后填写
# 也可继续用 .env：cp .env.example .env
```

| 密钥字段（keys.txt） | 环境变量 | 用途 | 说明 |
|---|---|---|---|
| `baidu_ak` | `BAIDU_AK` | 官方全景静态图 API（仅 `--source api` 备选路线用） | 多个用英文逗号分隔轮换；逆向接口无需 AK |
| `tianditu_tk` | `TIANDITU_TK` | 天地图密钥（`map` 命令底图用，可选） | 无则退回无瓦片模式；申请见 `https://console.tianditu.gov.cn/` |
| `overpass_url` | `OVERPASS_URL` | Overpass 路网端点（可选） | 可换国内可达镜像 |

## 快速开始

**方式一：一个配置文件一步跑完全流程（推荐）**

```bash
# 1. 复制密钥模板并填写（逆向接口无需填 baidu_ak）
cp configs/keys.txt.example configs/keys.txt

# 2. 直接跑（默认读 configs/config.yaml，无需 -c）
baidu-pano run
# 想用别的配置：先复制改好，再 -c 指定
# cp configs/config.yaml configs/my.yaml && baidu-pano run -c configs/my.yaml
```

**方式二：三条命令分步跑**

```bash
# 1. 从路网生成采样点
baidu-pano sample --roads data/roads.gpkg --area data/study_area.gpkg

# 2. 逆向接口下载四方向照片（默认，无需 AK）
baidu-pano download

# 2'. 下载完整 360° 全景图
baidu-pano download --mode panorama

# 2''. 跳过 sample，直接用采样点 CSV 下载
baidu-pano download --points examples/sample_points.csv --mode panorama

# 3. 生成网页互动地图（双击 streetview_map.html 用浏览器打开）
baidu-pano map
```

> 分步命令也能读配置文件（命令行参数覆盖同名项）：`baidu-pano download -c configs/config.yaml --mode panorama`。

产物（默认落在数据根目录）：

```
data/XXX/
├── StreetSamples/
│   ├── street_samples.gpkg          # streets + points 两层
│   └── street_samples_points.csv    # name,lng,lat，可直接喂给 download
└── StreetViewBatchDownload/
    ├── point_1_XX路_20190519_left.jpg ...  # 四方向（文件名含路名+拍摄日期；缺失则省略；全景为 point_1_XX路_20190519_pano.jpg）
    ├── download_report.csv          # 每个点的状态、panoid、road_name（路名）、shot_date（拍摄日期）、car_heading（采集车朝向）
    └── streetview_map.html          # 网页互动地图（baidu-pano map 生成，双击打开）
```

## 采样点输入格式

两种来源，二选一：

**A. 路网矢量（`sample` 生成）**：shp/gpkg/geojson 线要素，需带 CRS。也可用 `--pbf`（离线 .osm.pbf）或省略走 Overpass。路网三种来源及离线 .osm.pbf 下载详见下文「如何获取路网数据」。

**B. 采样点 CSV（`download --points` 直接下载）**：

| 列 | 必需 | 说明 |
|---|---|---|
| `name` | ✅ | 采样点唯一名，作为文件名前缀 |
| `lng` | ✅ | 经度（默认 WGS84） |
| `lat` | ✅ | 纬度（默认 WGS84） |
| `bearing` | 可选 | 街道走向方位角（0=北，顺时针，[0,180)）。四方向模式靠它定向；缺省按 0°；**全景模式不需要** |

> 默认把 `lng`/`lat` 当作 WGS84；如果你的点来自百度地图拾取系统（百度坐标）或高德/腾讯拾取（火星坐标），下载时加 `--points-crs bd09` / `--points-crs gcj02`，工具会自动转成 WGS84，无需手动换算。

## 如何获取路网数据

`sample` 命令的路网来源有三种（**三选一**）：

| 方式 | 来源 | 适合场景 | 是否需要 `--area` |
|---|---|---|---|
| **A. 本地矢量** | shp/gpkg/geojson（自备路网） | 首选 | 可选（提供则仅保留研究区内采样点） |
| **B. 离线 .osm.pbf** | 从 BBBike / Geofabrik 下载 | 国内稳定可用 | **必需** |
| **C. Overpass 在线** | 在线 API（需可达端点） | 国外/能通网环境的快速试验 | **必需** |

> ⚠️国内访问公共 Overpass API 不稳定（实测多端点 403/超时），正式项目请用方式 A 或 B。

### 方式 B：离线 .osm.pbf 获取方式

`.osm.pbf` 是 OpenStreetMap（OSM）的路网数据包，离线可用、国内稳定。两个推荐下载渠道：

**① BBBike（推荐，可按自定义区域，国内可访问）**

1. 打开 <https://extract.bbbike.org/>；
2. 把地图移到你的研究区（可缩放/拖动，或用搜索框定位城市）；
3. 左侧 **格式（Format）** 里勾选 **Protocol Buffer (PBF)**；
4. 填区域名（Name）和邮箱（Email），点 **Extract**，可调整范围；
5. 设置完成，点击**提取**，下载得到 `.osm.pbf` 文件。

> 也有现成的中国/城市数据可直接下：<https://download.bbbike.org/osm/region/asia/china/>（选 Protocol Buffer (PBF) 一项）。
>
> ⚠️ **别选错格式**：BBBike 有 GeoPackage 等几十种格式。若下成 GeoPackage（.gpkg），路网被拆在 `lines` 图层里，不能直接喂 `--roads`；要 .osm.pbf 就务必勾 **PBF**。

**② Geofabrik（按国家/省份，文件较大）**

- 中国全境：<https://download.geofabrik.de/asia/china-latest.osm.pbf>（约 1.5 GB，偏大）
- 各省份：<https://download.geofabrik.de/asia/china.html> 选对应省份（如 `shanghai-latest.osm.pbf` 约 24 MB，更推荐）
- 服务器在海外，国内直连较慢，建议用支持断点续传的下载工具。

**下载后使用：**

```bash
# 命令行（--pbf 源必须同时给 --area 界定研究范围）
baidu-pano sample --pbf data/你的区域.osm.pbf --area data/study_area.gpkg
```

```yaml
# 或 config.yaml
sample:
  pbf: data/你的区域.osm.pbf
  area: data/study_area.gpkg   # pbf 源必需
```

**注意事项：**

- **依赖**：`--pbf` 需要 `pyrosm`，用前先装：`pip install pyrosm`。
- **`--area` 必需**：pbf 源必须同时给 `--area` 界定研究范围（采样后仅保留区内点）；本地矢量 `--roads` 才可省略。
- **非机动车道**：.osm.pbf 里含 `footway`/`cycleway`/`pedestrian`/`steps`（人行道/骑行道等），默认（`--highway` 留空）会一并采样；只要车行道就用 `--highway` 过滤，如 `--highway motorway trunk primary secondary tertiary residential unclassified service`（完整车行道集合见 `sampling.py` 的 `DEFAULT_HIGHWAY_TYPES`）。
- **许可**：OSM 数据遵循 ODbL 1.0，引用/分发需遵守其条款。

## 命令行参考

### run

```bash
baidu-pano run                # 默认读 configs/config.yaml
baidu-pano run -c configs/my.yaml   # 或指定别的配置文件
```

一个 YAML 配置跑完 采样 → 下载 → 地图 三步（顺序执行，可选跳）。配置分节：

```yaml
base_dir: ./data              # 数据根目录
key_file: configs/keys.txt    # 密钥文件（字段=值）

sample:                       # 第一步：采样
  roads: data/roads.gpkg      # 路网来源：roads / pbf / overpass 三选一
  spacing: 40
  offset: 15
  highway: []

download:                     # 第二步：下载（核心）
  source: reverse             # reverse / api
  mode: 4_views               # 4_views / left_right / front_back / panorama
  pano_level: 3
  interval: 0.3
  crop_bottom: 0.4
  crop_top: 0.0
  search_radius: 5.0

map:                          # 第三步：地图
  out: streetview_map.html
  imagery: false
  points: data/samples.csv    # 可选：指定采样点 CSV（不传则读 sample 生成的 gpkg）
  points_crs: wgs84           # CSV 坐标系：wgs84 / gcj02 / bd09（仅当 points 指定 CSV 时生效）
```

- `sample` 可跳过：`download.points` 指定采样点 CSV 时自动跳过采样。
- `map` 可关闭：`map: false` 时跳过地图生成。
- 分步命令 `sample`/`download`/`map` 也可用 `-c` 读同一份配置，命令行参数覆盖配置同名项。

### sample

```bash
baidu-pano sample --roads <路网> [--area <研究区>] [--spacing 40] [--offset 15] [--highway ...]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--roads` | — | 本地路网矢量文件 shp/gpkg/geojson（线要素，推荐）；与 `--pbf` / Overpass 三选一 |
| `--pbf` | — | 离线 .osm.pbf 路网文件；与 `--roads` / Overpass 三选一 |
| `--area` | — | 研究区矢量文件 gpkg/geojson/shp（可选）。提供时采样后仅保留研究区内点位；用 `--roads` 时可省略，`--pbf` / Overpass 源时必需（界定研究范围） |
| `--spacing` | `40` | 采样间距（米） |
| `--offset` | `15` | 端点偏移（米） |
| `--highway` | 全部 | highway 类型过滤（如 `residential primary`），留空保留全部 |

> 省略 `--roads` 与 `--pbf` 时走 Overpass 在线拉取路网（需 `--area` 界定范围，且 `OVERPASS_URL` 可达）。三者互斥，任选其一。

### download

```bash
baidu-pano download [--points <CSV>] [--mode 4_views] [--source reverse] [--crop-bottom 0.4] ...
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--points` | — | 采样点 CSV；省略则读 `sample` 产出的 gpkg |
| `--points-crs` | `wgs84` | 采样点 CSV 的坐标系：`wgs84`（默认）/ `gcj02`（高德/腾讯拾取）/ `bd09`（百度地图拾取）；仅 `--points`（CSV 输入）生效，自动转 WGS84 |
| `--mode` | `4_views` | `4_views` / `left_right` / `front_back` / `panorama`（完整 360° 全景） |
| `--source` | `reverse` | `reverse`=逆向接口（无需 AK）；`api`=官方全景静态图 API（需 AK） |
| `--pano-level` | `3` | 逆向接口清晰度 1~4（3=4096×2048；4=8192×4096，请求量/存储 4 倍，一般无精度收益，不推荐）|
| `--crop-bottom` | `0.4` | 裁掉全景底部比例（采集车顶/车头）；设 0 关闭 |
| `--crop-top` | `0.0` | 裁掉全景顶部比例（天空）；与 `--crop-bottom` 之和须小于 1 |
| `--search-radius` | `5` | 坐标本身无街景时的就近搜索半径（米）；设 0 关闭 |
| `--interval` | `0.3` | 请求间隔秒（reverse 为瓦片间隔，api 为请求限速）。逆向接口无 AK、无日配额，但受服务端风控，保持默认 `--interval` 避免限速 |

### map

```bash
baidu-pano map [--points <CSV>] [--roads <路网>] [--area <研究区>] [--out <HTML>] [--imagery]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--points` | — | 采样点 CSV（name,lng,lat）；省略则读 `sample` 产出的 gpkg |
| `--points-crs` | `wgs84` | 采样点 CSV 的坐标系：`wgs84`（默认）/ `gcj02`（高德/腾讯拾取）/ `bd09`（百度地图拾取）；仅 `--points`（CSV 输入）生效，自动转 WGS84。`run` 全流程默认沿用 `download.points_crs`，分步 `map -c` 则读 `map.points_crs` |
| `--roads` | — | 路网矢量文件；省略则自动发现数据目录下的 `roads.gpkg` / `roads.geojson` / `roads.shp` |
| `--area` | — | 研究范围矢量文件；省略则自动发现数据目录下的 `study_area.gpkg` / `study_area.geojson` / `study_area.shp` |
| `--out` | `streetview_map.html` | 输出 HTML 路径（默认落在当前数据目录下） |
| `--imagery` | `true` | 默认显示天地图影像底图（默认矢量底图） |

生成网页互动地图 HTML，双击用浏览器打开：

- **有街景照片**的采样点显示为蓝色圆点，点击弹出该点照片（顶部显示路名）——四方向模式显示左/右/前/后 4 张，全景模式显示 1 张 360° 全景图；**再点图片可全屏放大查看原图**。
- **无街景照片**的采样点显示为灰色圆点，点击提示状态（无街景 / 下载失败 / 未下载）。
- **路网 / 研究范围**：自动发现数据目录下的 `roads.gpkg` 和 `study_area.gpkg`（有几个画几个，都没有则不画），在右上角**图层控件里可勾选显示/隐藏**。非 WGS84 会自动转坐标。
- 底图默认天地图；需在 `.env` 配置 `TIANDITU_TK`（申请见 `https://console.tianditu.gov.cn/`），未配置时退回无瓦片模式、仅显示点位。

## 重试与异常处理

| 情形 | 策略 |
|---|---|
| qsdata `error==0` | 成功，取 panoid |
| qsdata `error==404` | 真无街景，**不重试**，记 `no_pano` |
| qsdata 其他 error / 网络异常 | 限流或抖动，重试后判定（避免批量时偶发误判 no_pano） |
| 瓦片下载失败 | 重试；整点失败记 `download_failed`，不中断 |
| 请求限速 | 瓦片级间隔（`--interval`） |
| 覆盖率 < 50% | 黄色警告，提示该区域百度全景覆盖不足 |

断点续跑：某点四视角齐全 / 全景图已存在即跳过；`no_pano` 点无文件、下次重跑自动再试。

## 合规声明

- **街景原始图受百度服务条款约束，不得对外发布原始影像**；逆向接口仅供研究内部分析，请勿商业发布或大规模对外传播。
- 官方全景静态图 API 有日配额，大规模研究建议企业认证 + 多 AK 轮换。


## 测试

```bash
# 无需网络/密钥：坐标、bearing、裁剪切片、采样、mock 下载端到端、互动地图、配置驱动
python tests/test_coords.py
python tests/test_bearing.py
python tests/test_slice.py
python tests/test_sampling.py
python tests/smoke_test.py
python tests/test_mapping.py
python tests/test_config.py
```

## LICENSE

本项目以 Apache 2.0 发布，见 [LICENSE](LICENSE)。
