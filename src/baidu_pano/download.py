"""批量下载主流程编排。

两条路线：
  reverse —— 百度逆向接口（默认，无需 AK）：qsdata 拿 panoid → pdata 拼全景
             → 按 mode 切四视角，或直接输出完整 360° 全景图。
  api     —— 官方全景静态图 API（备选，需 BAIDU_AK）：逐方向发请求。

两种采样点输入：
  gpkg —— sample 产物（含 streets 层，可算街道走向 bearing）
  CSV  —— name,lng,lat[,bearing]（跳过 sample，直接下载；缺 bearing 时四视角按 0° 定向）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .bearing import compute_bearing_for_point, get_headings_for_mode
from .coords import bd09_to_wgs84, gcj02_to_wgs84, wgs84_to_bd09mc
from .pano_client import BaiduReverseClient
from .pano_slice import crop_streetview, slice_views


# ------------------------------------------------------------------
# 终端进度条（零依赖，纯 ASCII 保证 Windows 控制台不乱码）
# ------------------------------------------------------------------
def _is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _render_bar(done: int, total: int, width: int = 24) -> str:
    """渲染 [####----] 形式进度条 + 计数 + 百分比。"""
    frac = min(done / max(total, 1), 1.0)
    filled = int(round(width * frac))
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {done}/{total} {frac * 100:5.1f}%"


# ------------------------------------------------------------------
# 采样点输入加载
# ------------------------------------------------------------------
def _convert_points_crs(lngs, lats, points_crs: str):
    """把采样点 CSV 的 lng/lat 归一化为 WGS84（成果库唯一坐标系）。

    wgs84 恒等返回；gcj02（火星坐标，高德/腾讯拾取）、bd09（百度坐标拾取）
    逐点逆变换到 WGS84。仅允许在 CSV 入口处调用，转换结果不写回 CSV。
    """
    if points_crs == "wgs84":
        return lngs, lats
    if points_crs == "gcj02":
        fn = gcj02_to_wgs84
    elif points_crs == "bd09":
        fn = bd09_to_wgs84
    else:
        raise ValueError(
            f"未知坐标系 {points_crs!r}，--points-crs 仅支持 wgs84 / gcj02 / bd09")
    out_lng, out_lat = [], []
    for lng, lat in zip(lngs, lats):
        wlng, wlat = fn(float(lng), float(lat))
        out_lng.append(wlng)
        out_lat.append(wlat)
    return out_lng, out_lat


def load_points_and_bearings(points_csv: Path | None, gpkg_path: Path | None,
                             max_sub_m: float = 50.0,
                             points_crs: str = "wgs84"):
    """加载采样点，返回 (points_gdf, bearings: dict[point_id, bearing])。

    points_csv 提供时优先用 CSV（name,lng,lat[,bearing]）；CSV 坐标按 points_crs
    归一化为 WGS84（wgs84/gcj02/bd09）。否则读 gpkg 的 points + streets 两层，
    按街道几何算 bearing。CSV 缺 bearing 列时一律按 0°。
    """
    if points_csv is not None:
        df = pd.read_csv(points_csv, encoding="utf-8-sig")
        for col in ("name", "lng", "lat"):
            if col not in df.columns:
                raise ValueError(f"采样点 CSV 缺少必需列 {col!r}（需要 name,lng,lat）")
        lngs, lats = _convert_points_crs(df["lng"], df["lat"], points_crs)
        gdf = gpd.GeoDataFrame(
            df, geometry=gpd.points_from_xy(lngs, lats), crs="EPSG:4326"
        ).rename(columns={"name": "point_id"})
        gdf["Latitude"] = gdf.geometry.y
        gdf["Longitude"] = gdf.geometry.x
        bearings: dict[str, float] = {}
        for _, row in df.iterrows():
            v = row.get("bearing") if "bearing" in df.columns else None
            bearings[row["name"]] = float(v) if pd.notna(v) else 0.0
        return gdf, bearings

    if gpkg_path is None or not Path(gpkg_path).exists():
        raise ValueError(
            "未找到采样点：请用 --points 指定采样点 CSV，或先运行 sample 生成 gpkg")

    points = gpd.read_file(gpkg_path, layer="points")
    streets = gpd.read_file(gpkg_path, layer="streets")

    # bearing：点位不吸附，街道走向用于切片定向（与上游 Phase 1 一致）
    proj = points.estimate_utm_crs()
    pts_p = points.to_crs(proj)
    sts_p = streets.to_crs(proj)
    lookup = dict(zip(sts_p["street_id"], sts_p.geometry))
    bearings = {}
    for _, row in pts_p.iterrows():
        geom = lookup.get(row.get("street_id"))
        bearings[row["point_id"]] = (
            round(compute_bearing_for_point(row.geometry, geom, max_sub_m), 1)
            if geom is not None else 0.0)
    points["bearing"] = points["point_id"].map(bearings)
    points.to_file(gpkg_path, layer="points", driver="GPKG")
    return points, bearings


# ------------------------------------------------------------------
# 覆盖率统计 + 报告落盘
# ------------------------------------------------------------------
# 文件系统非法字符（Windows 文件名不允许 \\ / : * ? " < > |，连同空白一并去掉）
_ILLEGAL_FS = re.compile(r'[\\/:*?"<>|\s]+')


def _safe_road_name(name) -> str:
    """把 sdata 的 Rname 清洗成可入文件名的安全串；空/无则返回 ''。"""
    if not name:
        return ""
    return _ILLEGAL_FS.sub("", str(name))


def _img_stem(pid: str, road_name, shot_date, suffix: str) -> str:
    """图片文件名主干：{pid}_{路名}_{拍摄时间}_{suffix}。

    路名、拍摄时间均为可选段，缺失则省略对应段；两者都缺时退化为 {pid}_{suffix}。
    """
    meta = "_".join(
        x for x in (_safe_road_name(road_name), _safe_road_name(shot_date)) if x)
    return f"{pid}_{meta}_{suffix}" if meta else f"{pid}_{suffix}"


def _has_image(download_dir: Path, pid: str, suffix: str) -> bool:
    """该点某方向图片是否存在（兼容带/不带路名两种命名）。"""
    if list(download_dir.glob(f"{pid}_*_{suffix}.jpg")):
        return True
    return (download_dir / f"{pid}_{suffix}.jpg").exists()


_DATE8 = re.compile(r"^\d{8}$")


def _parse_road_date(download_dir: Path, pid: str, suffixes) -> tuple[str | None, str | None]:
    """从已下载文件名解析路名与拍摄日期（断点续跑点恢复报告信息用）。

    文件名格式 {pid}_{路名}_{日期}_{suffix}.jpg（可选段可缺失）。解析规则：中间段
    以 "_" 拆分，最后一段为 8 位纯数字时视为拍摄日期、其余拼接为路名。
    旧命名 {pid}_{suffix}.jpg 无中间段，返回 (None, None)。
    """
    for s in suffixes:
        cand = sorted(download_dir.glob(f"{pid}_*_{s}.jpg"))
        legacy = download_dir / f"{pid}_{s}.jpg"
        path = cand[0] if cand else (legacy if legacy.exists() else None)
        if path is None:
            continue
        stem = path.stem
        if stem == f"{pid}_{s}":  # 旧命名，无路名/日期段
            return None, None
        meta = stem[len(pid) + 1:]
        if meta.endswith("_" + s):
            meta = meta[: -len(s) - 1]
        parts = meta.split("_") if meta else []
        if not parts:
            return None, None
        if _DATE8.match(parts[-1]):
            date = parts[-1]
            road = "_".join(parts[:-1]) or None
        else:
            date, road = None, "_".join(parts) or None
        return road, date
    return None, None


def _covered_point_ids(download_dir: Path, mode: str, point_ids) -> set[str]:
    """统计有产出影像的点集（含断点续跑此前下载的）。

    基于 point_ids 逐个检查文件是否存在，而非 glob 固定前缀——这样 CSV 输入的
    任意命名（如 p1）也能正确统计覆盖率。
    """
    if mode == "panorama":
        return {pid for pid in point_ids if _has_image(download_dir, pid, "pano")}
    suffixes = [s for _, s in get_headings_for_mode(mode, 0.0)]
    return {
        pid for pid in point_ids
        if any(_has_image(download_dir, pid, s) for s in suffixes)
    }


def _write_report(download_dir: Path, report: list[dict]) -> None:
    try:
        pd.DataFrame(report).to_csv(download_dir / "download_report.csv",
                                    index=False, encoding="utf-8-sig")
    except PermissionError:
        print("[warn] download_report.csv 写入失败（文件可能正被 Excel 打开占用）。"
              "本次结果未能记录，但街景图片已正常保存，可关闭占用后重跑以刷新报告。")


def _print_summary(download_dir: Path, point_ids, mode: str,
                   n_ok: int, n_nopano: int, n_skip: int, n_fail: int) -> None:
    n_points = len(point_ids)
    n_with = len(_covered_point_ids(download_dir, mode, point_ids))
    coverage = n_with / max(n_points, 1)
    print(f"下载完成：新点 {n_ok}，无街景 {n_nopano}，续跑跳过 {n_skip}，失败 {n_fail}")
    print(f"点位覆盖率：{n_with}/{n_points} = {coverage:.1%}")
    if coverage < 0.5:
        print("[warn] 覆盖率低于 50%，该区域百度全景覆盖不足，结果慎用")


# ------------------------------------------------------------------
# 路线一：逆向接口（默认）
# ------------------------------------------------------------------
def download_streetview_reverse(
    download_dir: Path,
    points_csv: Path | None = None,
    gpkg_path: Path | None = None,
    points_crs: str = "wgs84",
    mode: str = "4_views",
    level: int = 3,
    sleep_seconds: float = 0.3,
    timeout: int = 30,
    max_retries: int = 2,
    max_sub_m: float = 50.0,
    crop_bottom: float = 0.4,
    crop_top: float = 0.0,
    search_radius: float = 5.0,
) -> pd.DataFrame:
    """逆向接口批量下载。返回下载报告（也写 download_report.csv）。

    mode：4_views（左/右/前/后四张）、left_right、front_back、panorama（完整全景）。

    断点续下：四视角 {point_id}_{路名}_{拍摄时间}_{view}.jpg（路名/时间缺失时省略对应段）
    齐全即跳过；panorama 模式 {point_id}_{路名}_{拍摄时间}_pano.jpg 存在即跳过。no_pano
    点无文件，下次重跑自动再试 qsdata。
    """
    download_dir.mkdir(parents=True, exist_ok=True)
    points, bearings = load_points_and_bearings(points_csv, gpkg_path, max_sub_m, points_crs)

    is_pano = mode == "panorama"
    suffixes = ["pano"] if is_pano else [s for _, s in get_headings_for_mode(mode, 0.0)]

    client = BaiduReverseClient(sleep_seconds, timeout, max_retries)
    report: list[dict] = []
    n_ok = n_skip = n_nopano = n_fail = 0
    total = len(points)
    use_inline = _is_tty(sys.stderr)

    for i, (_, row) in enumerate(points.iterrows()):
        pid = row["point_id"]
        lat, lon = row["Latitude"], row["Longitude"]

        if is_pano:
            done = _has_image(download_dir, pid, "pano")
        else:
            done = all(_has_image(download_dir, pid, s) for s in suffixes)

        if done:
            n_skip += 1
            # 跳过的点也写报告：从文件名恢复路名/拍摄日期。否则 download_report.csv
            # 被重跑覆盖后，先下载点的路名会丢失，地图弹窗路名随之缺失。
            road_name, shot_date = _parse_road_date(download_dir, pid, suffixes)
            rep = {"image_name": pid, "status": "ok", "lat": lat, "lon": lon}
            if road_name:
                rep["road_name"] = road_name
            if shot_date:
                rep["shot_date"] = shot_date
            report.append(rep)
        else:
            mc_x, mc_y = wgs84_to_bd09mc(lon, lat)
            panoid = None
            nearest_m = None
            err = None
            try:
                panoid = client.get_panoid(mc_x, mc_y)
            except Exception as e:
                err = e
            # 坐标本身无街景时，就近搜索兜底
            if err is None and not panoid and search_radius > 0:
                try:
                    panoid, nearest_m = client.find_nearest_panoid(
                        mc_x, mc_y, search_radius)
                except Exception:
                    panoid, nearest_m = None, None

            if err is not None:
                n_fail += 1
                report.append({"image_name": pid, "status": "download_failed",
                               "lat": lat, "lon": lon,
                               "message": f"{type(err).__name__}"})
            elif not panoid:
                n_nopano += 1
                report.append({"image_name": pid, "status": "no_pano",
                               "lat": lat, "lon": lon})
            else:
                # 瓦片级进度回调：主进度始终显示点级（第几个点/共几个点），
                # 瓦片进度作为从属信息跟在同一行，避免 32 块瓦片的子进度
                # 反复覆盖点级进度、让用户误以为总共只有 32 个任务
                def tile_cb(done, total_tiles, _pid=pid, _done_pts=i):
                    if use_inline:
                        sys.stderr.write(
                            "\r" + _render_bar(_done_pts, total)
                            + f" 点 | " + _render_bar(done, total_tiles, width=12)
                            + f" 瓦片（{_pid}）      ")
                        sys.stderr.flush()

                try:
                    # 方向校准 + 路名 + 拍摄时间：先取 sdata（所有模式都取，用于文件名与报告），
                    # 再下瓦片拼全景。提前到瓦片下载前，既避开 32 块瓦片高频请求后的
                    # 限流窗口，失败也能更快重试（sdata 单次请求成本远低于 32 块瓦片）。
                    car_heading: float | None = None
                    road_name: str | None = None
                    shot_date: str | None = None
                    cam = client.get_camera_info(panoid)
                    if cam:
                        if cam.get("Rname"):
                            road_name = str(cam["Rname"])
                        if cam.get("Date"):
                            shot_date = str(cam["Date"])
                        if "Heading" in cam:
                            car_heading = float(cam["Heading"])
                    if not is_pano and car_heading is None:
                        print(f"  [warn] {pid}: sdata 未返回 Heading，"
                              f"方向图可能整体转偏，已按旧约定切片")
                    pano = client.download_panorama(panoid, level,
                                                    progress_cb=tile_cb)
                    if is_pano:
                        stem = _img_stem(pid, road_name, shot_date, "pano")
                        crop_streetview(pano, crop_bottom, crop_top).save(
                            download_dir / f"{stem}.jpg", quality=90)
                    else:
                        # 全景图第 0 列 = 采集车朝向(Heading)的左侧，
                        # 切方位角 h 需加偏移 (90 - Heading)，见 pano_slice 注释
                        heading_offset = (
                            (90.0 - car_heading) % 360
                            if car_heading is not None else 0.0)
                        for suffix, img in slice_views(
                                pano, bearings.get(pid, 0.0), mode,
                                crop_bottom, crop_top,
                                heading_offset=heading_offset).items():
                            stem = _img_stem(pid, road_name, shot_date, suffix)
                            img.save(download_dir / f"{stem}.jpg", quality=90)
                    n_ok += 1
                    rep = {"image_name": pid, "status": "ok",
                           "lat": lat, "lon": lon, "panoid": panoid}
                    if road_name:
                        rep["road_name"] = road_name
                    if shot_date:
                        rep["shot_date"] = shot_date
                    if car_heading is not None:
                        rep["car_heading"] = round(car_heading, 2)
                    if nearest_m is not None:
                        rep["message"] = f"就近匹配 {nearest_m:.0f}m"
                    report.append(rep)
                except Exception as e:
                    n_fail += 1
                    report.append({"image_name": pid, "status": "download_failed",
                                   "lat": lat, "lon": lon,
                                   "message": f"{type(e).__name__}"})

        # 点级进度：原地刷新或降级打印（行尾长空格清除瓦片行残留字符）
        if use_inline:
            sys.stderr.write(
                "\r" + _render_bar(i + 1, total) + " 点"
                + f"  新{n_ok} 无街景{n_nopano} 跳过{n_skip} 失败{n_fail}"
                + " " * 24)
            sys.stderr.flush()
        elif (i + 1) % 20 == 0 or (i + 1) == total:
            print(f"  进度 {i + 1}/{total} 点 | 新 {n_ok} 无街景 {n_nopano} "
                  f"跳过 {n_skip} 失败 {n_fail}")

    if use_inline:
        sys.stderr.write("\n")
        sys.stderr.flush()

    _write_report(download_dir, report)
    _print_summary(download_dir, points["point_id"], mode, n_ok, n_nopano, n_skip, n_fail)
    return pd.DataFrame(report)


# ------------------------------------------------------------------
# 路线二：官方全景静态图 API（备选）
# ------------------------------------------------------------------
def download_streetview_api(
    download_dir: Path,
    client,
    points_csv: Path | None = None,
    gpkg_path: Path | None = None,
    points_crs: str = "wgs84",
    mode: str = "4_views",
    pitch: int = -20,
    fov: int = 90,
    max_sub_m: float = 50.0,
    dedup_by_hash: bool = True,
) -> pd.DataFrame:
    """官方全景静态图 API 批量下载（逐方向发请求）。返回下载报告。"""
    import hashlib

    if mode == "panorama":
        raise ValueError("官方 API 路线不支持全景模式，请改用 --source reverse")

    download_dir.mkdir(parents=True, exist_ok=True)
    points, bearings = load_points_and_bearings(points_csv, gpkg_path, max_sub_m, points_crs)

    report: list[dict] = []
    n_new = n_skip = n_blank = n_fail = 0
    seen_hashes: dict[str, str] = {}  # md5 -> 首个文件名（内容去重）

    for i, (_, row) in enumerate(points.iterrows()):
        pid = row["point_id"]
        lat, lon = row["Latitude"], row["Longitude"]
        headings = get_headings_for_mode(mode, bearings.get(pid, 0.0))

        for heading, suffix in headings:
            base = f"{pid}_{suffix}"
            expected = [download_dir / f"{base}.jpg", download_dir / f"{base}_NA.jpg"]
            if any(p.exists() for p in expected):
                n_skip += 1
                continue

            img = client.fetch(lat, lon, heading, pitch, fov)
            if img is None:
                n_fail += 1
                report.append({"image_name": f"{base}.jpg", "status": "download_failed"})
                continue

            digest = hashlib.md5(img.tobytes()).hexdigest()
            status = "ok"
            from .api_client import is_blank_image

            if is_blank_image(img):
                status = "blank"
                base += "_NA"
                n_blank += 1
            elif dedup_by_hash and digest in seen_hashes:
                status = f"dup_of:{seen_hashes[digest]}"
                base += "_NA"  # 重复影像按无效处理
                n_blank += 1
            else:
                seen_hashes[digest] = f"{base}.jpg"
                n_new += 1

            img.save(download_dir / f"{base}.jpg", quality=90)
            report.append({"image_name": f"{base}.jpg", "status": status,
                           "lat": lat, "lon": lon, "heading": round(heading, 1)})

        if (i + 1) % 20 == 0 or (i + 1) == len(points):
            print(f"  进度 {i + 1}/{len(points)} 点 | 新 {n_new} 空/重 {n_blank} "
                  f"跳过 {n_skip} 失败 {n_fail}")

    _write_report(download_dir, report)
    n_with = len(_covered_point_ids(download_dir, mode, points["point_id"]))
    coverage = n_with / max(len(points), 1)
    print(f"下载完成：新图 {n_new}，空/重复 {n_blank}，续跑跳过 {n_skip}，失败 {n_fail}")
    print(f"点位覆盖率：{n_with}/{len(points)} = {coverage:.1%}")
    if coverage < 0.5:
        print("[warn] 覆盖率低于 50%，该区域百度全景覆盖不足，结果慎用")
    return pd.DataFrame(report)
