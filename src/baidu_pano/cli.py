"""命令行入口。

子命令：
  run       一步跑完全流程（采样 → 下载 → 地图），默认读 configs/config.yaml
  sample    路网采样点生成（本地矢量 / 离线 pbf / Overpass 三选一）
  download  百度全景批量下载（默认逆向接口无需 AK；可切官方 API）
  map       生成网页互动地图 HTML（点级查看街景照片，可点击放大）

示例：
  # 0. 一步跑完全流程（推荐）：默认读 configs/config.yaml，无需任何参数
  baidu-pano run
  # 或用 -c 指定别的配置文件
  baidu-pano run -c configs/我的配置.yaml

  # 1. 从路网生成采样点（数据默认落 --area/--roads 所在文件夹）
  baidu-pano sample --roads data/roads.gpkg --area data/study_area.gpkg

  # 2a. 逆向接口下载四方向照片（默认，无需 AK）
  baidu-pano download

  # 2b. 下载完整 360° 全景图（不切方向）
  baidu-pano download --mode panorama

  # 2c. 跳过 sample，直接用采样点 CSV 下载
  baidu-pano download --points samples.csv --mode panorama

  # 2d. 官方全景静态图 API（备选，需 BAIDU_AK）
  baidu-pano download --source api

  # 2e. 分步命令也可读配置文件，命令行参数覆盖配置
  baidu-pano download -c configs/config.yaml --mode panorama

  # 3. 生成互动地图（双击 streetview_map.html 打开）
  baidu-pano map
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import Config, config_from_yaml

# map 命令自动发现路网/研究范围时的候选文件名（按优先级）
_ROADS_NAMES = ["roads.gpkg", "roads.geojson", "roads.shp"]
_AREA_NAMES = ["study_area.gpkg", "study_area.geojson", "study_area.shp"]

# run 命令默认读取的配置文件（相对当前工作目录）
_DEFAULT_CONFIG = Path("configs/config.yaml")


def _find_aux_file(base_dir: Path, candidates: list[str]) -> Path | None:
    """在数据目录下按候选文件名找第一个存在的文件。找不到返回 None。"""
    for name in candidates:
        p = base_dir / name
        if p.is_file():
            return p
    return None


def _require_file(path: Path, what: str) -> Path:
    """输入文件前置检查：找不到时给出人话提示和候选文件，而不是堆栈报错。"""
    if path.exists():
        return path
    cwd = Path.cwd()
    candidates = []
    for folder in (cwd, *[d for d in cwd.iterdir() if d.is_dir()
                           and d.name not in ("__pycache__", ".git")]):
        exts = [path.suffix] if path.suffix else []
        candidates += [f for ext in exts for f in sorted(folder.glob(f"*{ext}"))]
        candidates += sorted(folder.glob(path.name))
    if not candidates:  # 后缀打错时，放宽到常见矢量格式再找一遍
        for folder in (cwd, *[d for d in cwd.iterdir() if d.is_dir()
                               and d.name not in ("__pycache__", ".git")]):
            candidates += [f for ext in (".gpkg", ".geojson", ".shp", ".json")
                           for f in sorted(folder.glob(f"*{ext}"))]
    seen, uniq = set(), []
    for c in candidates:
        if c not in seen and c.is_file():
            seen.add(c)
            uniq.append(c)
    lines = [
        f"找不到{what}文件：{path}",
        f"当前所在文件夹：{cwd}",
        "排查提示：",
        "  1. 指令里的文件名必须和实际文件一字不差（注意 .geojson 和 .gpkg 后缀不同）；",
        "  2. 相对路径以『当前所在文件夹』为准，文件在子文件夹时要带上，如 data\\xxx.gpkg；",
        "  3. 也可以用完整路径，如 F:\\data\\xxx.gpkg。",
    ]
    if uniq:
        lines.append("在附近找到了这些可能是你要用的文件：")
        lines += [f"  - {c.relative_to(cwd)}" for c in uniq[:10]]
    raise SystemExit("\n".join(lines))


def _discover_case_dir(default: Path) -> Path | None:
    """在默认数据根目录的直接子目录里，找含 street_samples.gpkg 的目录。"""
    hits: list[Path] = []
    if default.is_dir():
        hits = [d for d in sorted(default.iterdir())
                if d.is_dir() and (d / "StreetSamples" / "street_samples.gpkg").exists()]
    return hits[0] if len(hits) == 1 else None


def _resolve_base_dir(args) -> Path:
    """确定数据根目录 base_dir。

    优先级：
      1. ``--base`` 参数（所有子命令，显式覆盖）
      2. ``sample``：``--area`` 所在文件夹，无则退 ``--roads`` 所在文件夹
      3. ``download``：``--points`` 所在文件夹（跳过 sample 直接下载时）
      4. 自动发现 ``./data`` 下唯一的 case 子目录
      5. ``BAIDU_PANO_BASE`` 环境变量（兜底）
    """
    if getattr(args, "base", None):
        return Path(args.base)

    if getattr(args, "cmd", None) == "sample":
        anchor = getattr(args, "area", None) or getattr(args, "roads", None)
        if anchor:
            parent = Path(anchor).parent
            if str(parent) not in ("", "."):
                return parent
    elif getattr(args, "points", None):
        parent = Path(args.points).parent
        if str(parent) not in ("", "."):
            return parent

    default = Path("./data")
    discovered = _discover_case_dir(default)
    if discovered:
        return discovered

    env = os.environ.get("BAIDU_PANO_BASE")
    return Path(env) if env else default


def _read_polygon(path: Path):
    import geopandas as gpd
    from shapely.ops import unary_union

    gdf = gpd.read_file(path)
    if gdf.crs and gdf.crs != "EPSG:4326":
        gdf = gdf.to_crs("EPSG:4326")
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if gdf.empty:
        raise ValueError(f"{path} 中无有效几何")
    geom = unary_union(gdf.geometry)
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    return geom


def cmd_sample(cfg: Config, args) -> None:
    from . import sampling

    polygon = None
    if args.area:
        polygon = _read_polygon(_require_file(Path(args.area), "研究区"))
        print(f"研究区：{args.area}（采样后仅保留研究区内点位）")

    if args.roads:
        print(f"路网来源：本地矢量 {args.roads}")
        streets = sampling.load_streets_from_vector(
            _require_file(Path(args.roads), "路网"), polygon)
    elif args.pbf:
        if polygon is None:
            raise SystemExit("使用离线 pbf 路网时必须提供 --area（用于界定研究范围）")
        print(f"路网来源：离线 pbf {args.pbf}")
        streets = sampling.load_streets_from_pbf(
            _require_file(Path(args.pbf), "离线路网"), polygon)
    else:
        if polygon is None:
            raise SystemExit("使用 Overpass 路网时必须提供 --area（用于界定研究范围）")
        print(f"路网来源：Overpass {cfg.overpass_url}")
        streets = sampling.load_streets_from_overpass(polygon, cfg.overpass_url)

    points, streets = sampling.generate_points(
        polygon=polygon, streets_gdf=streets,
        spacing_m=args.spacing, offset_m=args.offset,
        highway_types=args.highway or None,
    )
    cfg.ensure_dirs()
    gpkg = cfg.gpkg_path()
    sampling.save_case_gpkg(points, streets, gpkg)
    # 导出采样点坐标 CSV（name,lng,lat，WGS84），供直接下载/人工核点使用
    coords_csv = cfg.samples_dir / "street_samples_points.csv"
    points[["point_id", "Longitude", "Latitude"]].rename(
        columns={"point_id": "name", "Longitude": "lng", "Latitude": "lat"}
    ).to_csv(coords_csv, index=False, encoding="utf-8-sig")
    print(f"采样完成：{len(points)} 点 / {len(streets)} 街段 -> {gpkg}")
    print(f"坐标清单：{coords_csv}（name,lng,lat）")


def cmd_download(cfg: Config, args) -> None:
    points_csv = Path(args.points) if args.points else None
    gpkg_path = cfg.gpkg_path() if points_csv is None else None

    if args.source == "reverse":
        from .download import download_streetview_reverse

        download_streetview_reverse(
            download_dir=cfg.images_dir(),
            points_csv=points_csv,
            gpkg_path=gpkg_path,
            points_crs=args.points_crs,
            mode=args.mode,
            level=args.pano_level,
            sleep_seconds=args.interval,
            max_sub_m=args.max_sub,
            crop_bottom=args.crop_bottom,
            crop_top=args.crop_top,
            search_radius=args.search_radius,
        )
        return

    # 官方全景静态图 API（备选）
    from .api_client import BaiduPanoClient
    from .download import download_streetview_api

    if args.mode == "panorama":
        raise SystemExit("官方 API 路线不支持全景模式，请改用 --source reverse")
    client = BaiduPanoClient(
        aks=cfg.baidu_aks, base_url=cfg.baidu_pano_url,
        width=args.width, height=args.height,
        request_interval=args.interval,
    )
    download_streetview_api(
        download_dir=cfg.images_dir(),
        client=client,
        points_csv=points_csv,
        gpkg_path=gpkg_path,
        points_crs=args.points_crs,
        mode=args.mode,
        pitch=args.pitch,
        fov=args.fov,
        max_sub_m=args.max_sub,
        dedup_by_hash=not args.no_dedup,
    )


def cmd_map(cfg: Config, args) -> None:
    import geopandas as gpd
    import pandas as pd

    from .mapping import build_map

    points_csv = Path(args.points) if args.points else None
    if points_csv is not None:
        _require_file(points_csv, "采样点")
        df = pd.read_csv(points_csv, encoding="utf-8-sig")
        for col in ("name", "lng", "lat"):
            if col not in df.columns:
                raise SystemExit(f"采样点 CSV 缺少必需列 {col!r}（需要 name,lng,lat）")
        from .download import _convert_points_crs
        lngs, lats = _convert_points_crs(df["lng"], df["lat"], args.points_crs)
        points = gpd.GeoDataFrame(
            df, geometry=gpd.points_from_xy(lngs, lats), crs="EPSG:4326"
        ).rename(columns={"name": "point_id"})
    else:
        gpkg = cfg.gpkg_path()
        if not gpkg.exists():
            raise SystemExit(
                f"未找到采样点 gpkg：{gpkg}\n"
                f"请先运行 baidu-pano sample 生成，或用 --points 指定采样点 CSV")
        points = gpd.read_file(gpkg, layer="points")

    images_dir = cfg.images_dir()
    report_csv = images_dir / "download_report.csv"
    report = (pd.read_csv(report_csv, encoding="utf-8-sig")
              if report_csv.exists() else None)

    # 路网 / 研究范围：显式 --roads/--area 优先，否则在数据目录下自动发现同名文件
    roads_src = (Path(args.roads) if args.roads
                 else _find_aux_file(cfg.base_dir, _ROADS_NAMES))
    area_src = (Path(args.area) if args.area
                else _find_aux_file(cfg.base_dir, _AREA_NAMES))

    streets = area = None
    if roads_src:
        streets = gpd.read_file(_require_file(roads_src, "路网"))
        print(f"路网图层：{roads_src.name}")
    if area_src:
        area = gpd.read_file(_require_file(area_src, "研究范围"))
        print(f"研究范围图层：{area_src.name}")

    out_html = Path(args.out)
    if not out_html.is_absolute():
        # 默认落在数据目录，与原图目录同级，相对路径稳定
        out_html = cfg.base_dir / out_html
    build_map(points, images_dir, report, out_html, cfg.tianditu_tk,
              imagery=args.imagery, streets=streets, area=area)


# 各子命令「可被 YAML 覆盖」的参数默认值（无 -c 时等价于原 argparse 默认值）。
# key 对应 YAML 分节里的字段名。
_SAMPLE_DEFAULTS = {
    "area": None, "roads": None, "pbf": None,
    "spacing": 40, "offset": 15, "highway": None,
}
_DOWNLOAD_DEFAULTS = {
    "points": None, "points_crs": "wgs84", "source": "reverse", "mode": "4_views",
    "pano_level": 3, "pitch": -20, "fov": 90, "max_sub": 50.0,
    "width": 640, "height": 640, "interval": 0.3,
    "crop_bottom": 0.4, "crop_top": 0.0, "search_radius": 5.0,
    "no_dedup": False,
}
_MAP_DEFAULTS = {
    "points": None, "points_crs": "wgs84", "roads": None, "area": None,
    "out": "streetview_map.html", "imagery": False,
}
_SECTIONS = {"sample": _SAMPLE_DEFAULTS,
             "download": _DOWNLOAD_DEFAULTS,
             "map": _MAP_DEFAULTS}


def _apply_config(args, raw: dict, section: str) -> None:
    """把 YAML 分节里的值填到 args 里（仅当命令行未显式传参时覆盖）。

    命令行参数优先于 YAML：解析时用 ``default=argparse.SUPPRESS``，未显式传参的
    属性不会出现在 args 上，这里据此判断并回填 YAML / 默认值。
    """
    sec = raw.get(section) or {}
    for key, dflt in _SECTIONS[section].items():
        if hasattr(args, key):
            continue  # 命令行已显式传参
        setattr(args, key, sec.get(key, dflt))


def _ns_from_yaml(raw: dict, section: str) -> argparse.Namespace:
    """run 命令：从 YAML 分节构造等价 args（缺失键回退默认值）。"""
    sec = raw.get(section) or {}
    ns = argparse.Namespace()
    for key, dflt in _SECTIONS[section].items():
        setattr(ns, key, sec.get(key, dflt))
    return ns


def cmd_run(cfg: Config, raw: dict) -> None:
    """一步跑完 采样 → 下载 → 地图（顺序执行，可选跳）。"""
    download_sec = raw.get("download") or {}

    # 第一步：采样（download.points 显式指定采样点 CSV 时跳过）
    if download_sec.get("points"):
        print("跳过采样：download.points 已指定采样点 CSV")
    else:
        print("=" * 60)
        print("[1/3] 采样点生成")
        print("=" * 60)
        cmd_sample(cfg, _ns_from_yaml(raw, "sample"))

    # 第二步：下载（核心步骤，不可跳）
    print("=" * 60)
    print("[2/3] 街景图片下载")
    print("=" * 60)
    cmd_download(cfg, _ns_from_yaml(raw, "download"))

    # 第三步：地图（map: false 显式关闭时跳过）
    if raw.get("map") is False:
        print("跳过地图生成：配置里 map: false")
        return
    print("=" * 60)
    print("[3/3] 生成互动地图")
    print("=" * 60)
    map_ns = _ns_from_yaml(raw, "map")
    # 未在 map 节单独指定 points 时，沿用 download.points（保证与下载同一批点）
    if not map_ns.points and download_sec.get("points"):
        map_ns.points = download_sec["points"]
    # map 节未单独指定 points_crs 时，沿用 download 的（保证地图坐标转换与下载一致）
    map_sec = raw.get("map") or {}
    if "points_crs" not in map_sec and download_sec.get("points_crs"):
        map_ns.points_crs = download_sec["points_crs"]
    cmd_map(cfg, map_ns)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="baidu-pano", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--base",
        help="数据根目录（默认：sample 取 --area 或 --roads 所在文件夹；"
             "download 取 --points 所在文件夹，或自动发现 data 下唯一 case 目录，"
             "或读 BAIDU_PANO_BASE；用 -c 时优先 YAML 的 base_dir）")
    common.add_argument(
        "-c", "--config",
        help="YAML 配置文件（提供时命令行参数覆盖配置同名项；"
             "run 子命令默认读 configs/config.yaml，无需 -c）")

    sp = sub.add_parser("run", help="一步跑完 采样→下载→地图（默认读 configs/config.yaml）", parents=[common])

    sp = sub.add_parser("sample", help="路网采样点生成", parents=[common])
    sp.add_argument("--area", default=argparse.SUPPRESS,
                    help="研究区矢量文件 gpkg/geojson/shp（可选；提供时采样后"
                         "仅保留研究区内点位。用 --roads 本地路网时可省略；"
                         "--pbf / Overpass 源时必需）")
    src = sp.add_mutually_exclusive_group()
    src.add_argument("--roads", default=argparse.SUPPRESS,
                     help="本地路网矢量文件（推荐）")
    src.add_argument("--pbf", default=argparse.SUPPRESS,
                     help="离线 .osm.pbf 文件")
    sp.add_argument("--spacing", type=int, default=argparse.SUPPRESS,
                    help="采样间距（米，默认 40）")
    sp.add_argument("--offset", type=int, default=argparse.SUPPRESS,
                    help="端点偏移（米，默认 15）")
    sp.add_argument("--highway", nargs="*", default=argparse.SUPPRESS,
                    help="highway 类型过滤，默认全部保留")
    sp.set_defaults(func=cmd_sample)

    sp = sub.add_parser("download", help="百度全景批量下载", parents=[common])
    sp.add_argument("--points", default=argparse.SUPPRESS,
                    help="采样点 CSV（name,lng,lat[,bearing]；跳过 sample 直接下载）")
    sp.add_argument("--points-crs", choices=["wgs84", "gcj02", "bd09"],
                    default=argparse.SUPPRESS,
                    help="采样点 CSV 的坐标系：wgs84（默认）/ gcj02（高德/腾讯拾取）/ "
                         "bd09（百度地图拾取）。仅 --points（CSV 输入）生效，"
                         "其余自动转 WGS84")
    sp.add_argument("--source", choices=["reverse", "api"], default=argparse.SUPPRESS,
                    help="reverse=逆向接口（无需 AK，默认，高清全景切四视角/存全景）；"
                         "api=官方全景静态图 API（需 BAIDU_AK）")
    sp.add_argument("--mode", choices=["4_views", "left_right", "front_back", "panorama"],
                    default=argparse.SUPPRESS,
                    help="下载方向：4_views=左右前后四张（默认）；left_right=仅左右；"
                         "front_back=仅前后；panorama=完整 360° 全景图")
    sp.add_argument("--pano-level", type=int, choices=[1, 2, 3, 4], default=argparse.SUPPRESS,
                    help="reverse 源全景清晰度 1~4（默认 3=4096×2048；"
                         "4=8192×4096，请求量/存储 4 倍）")
    sp.add_argument("--pitch", type=int, default=argparse.SUPPRESS, help="仅 api 源生效")
    sp.add_argument("--fov", type=int, default=argparse.SUPPRESS, help="仅 api 源生效")
    sp.add_argument("--max-sub", type=float, default=argparse.SUPPRESS,
                    help="算 bearing 时街道细分长度（米，默认 50）")
    sp.add_argument("--width", type=int, default=argparse.SUPPRESS, help="仅 api 源生效")
    sp.add_argument("--height", type=int, default=argparse.SUPPRESS, help="仅 api 源生效")
    sp.add_argument("--interval", type=float, default=argparse.SUPPRESS,
                    help="请求间隔秒（reverse 为瓦片间隔，api 为请求限速）")
    sp.add_argument("--crop-bottom", type=float, default=argparse.SUPPRESS,
                    help="reverse 源：裁掉全景底部比例（默认 0.4 = 去掉下方 40%% 的"
                         "采集车顶/车头）；设 0 关闭")
    sp.add_argument("--crop-top", type=float, default=argparse.SUPPRESS,
                    help="reverse 源：裁掉全景顶部比例（默认 0 = 不裁天空）；"
                         "与 --crop-bottom 之和必须小于 1")
    sp.add_argument("--search-radius", type=float, default=argparse.SUPPRESS,
                    help="reverse 源：坐标本身无街景时的就近搜索半径（米，默认 5）；"
                         "设 0 关闭就近搜索")
    sp.add_argument("--no-dedup", action="store_true", default=argparse.SUPPRESS,
                    help="仅 api 源生效：关闭内容哈希去重")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("map", help="生成网页互动地图 HTML", parents=[common])
    sp.add_argument("--points", default=argparse.SUPPRESS,
                    help="采样点 CSV（name,lng,lat；不传则读 sample 生成的 gpkg）")
    sp.add_argument("--points-crs", choices=["wgs84", "gcj02", "bd09"],
                    default=argparse.SUPPRESS,
                    help="采样点 CSV 的坐标系：wgs84（默认）/ gcj02 / bd09。"
                         "仅 --points（CSV 输入）生效，自动转 WGS84")
    sp.add_argument("--roads", default=argparse.SUPPRESS,
                    help="路网矢量文件；不传则自动发现数据目录下的 "
                         "roads.gpkg / roads.geojson / roads.shp")
    sp.add_argument("--area", default=argparse.SUPPRESS,
                    help="研究范围矢量文件；不传则自动发现数据目录下的 "
                         "study_area.gpkg / study_area.geojson / study_area.shp")
    sp.add_argument("--out", default=argparse.SUPPRESS,
                    help="输出 HTML 路径（默认当前数据目录下 streetview_map.html）")
    sp.add_argument("--imagery", action="store_true", default=argparse.SUPPRESS,
                    help="默认显示天地图影像底图（默认矢量底图）")
    sp.set_defaults(func=cmd_map)

    args = p.parse_args(argv)

    # 配置驱动：run 默认读 configs/config.yaml（可 -c 覆盖），其余子命令 -c 可选
    raw: dict = {}
    config_path = getattr(args, "config", None)
    base_override = getattr(args, "base", None)
    if args.cmd == "run":
        if not config_path:
            config_path = _DEFAULT_CONFIG
            if not config_path.exists():
                raise SystemExit(
                    "未指定配置文件，且默认配置不存在：\n"
                    f"  默认找 {config_path}\n"
                    "  请先复制模板 configs/config.yaml 并编辑，"
                    "或用 -c 指定配置文件，例如：\n"
                    "  baidu-pano run -c 我的配置.yaml")
        cfg, raw = config_from_yaml(config_path, base_dir=base_override)
    elif config_path:
        cfg, raw = config_from_yaml(config_path, base_dir=base_override)
        _apply_config(args, raw, args.cmd)
    else:
        cfg = Config(base_dir=_resolve_base_dir(args))
        # 无 -c 时也回填默认值（等价于原 argparse 默认值）
        _apply_config(args, {}, args.cmd)

    cfg.ensure_dirs()

    if args.cmd == "run":
        cmd_run(cfg, raw)
        return 0

    args.func(cfg, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
