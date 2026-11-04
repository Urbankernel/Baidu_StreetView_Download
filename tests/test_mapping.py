"""mapping 互动地图单元测试（无网络、无密钥）。

覆盖：四方向点 / 全景点 / 无图点（灰点）三种场景 + lightbox + 相对路径。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
from PIL import Image
from shapely.geometry import Point, LineString, Polygon

from baidu_pano.mapping import build_map, _detect_images, _to_wgs84


def _mk_img(path: Path, w: int = 64, h: int = 48, color=(200, 30, 30)):
    Image.fromarray(np.full((h, w, 3), color, dtype=np.uint8)).save(path)


def _mk_points():
    return gpd.GeoDataFrame(
        {"point_id": ["p1", "p2", "p3"]},
        geometry=[Point(120.0, 30.0), Point(120.001, 30.0), Point(120.002, 30.0)],
        crs="EPSG:4326",
    )


def test_detect_images_modes():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # p1 四方向，p2 全景，p3 无
        for s in ("left", "right", "front", "back"):
            _mk_img(d / f"p1_{s}.jpg")
        _mk_img(d / "p2_pano.jpg")
        imgs1, pano1 = _detect_images(d, "p1")
        imgs2, pano2 = _detect_images(d, "p2")
        imgs3, pano3 = _detect_images(d, "p3")
        assert not pano1 and len(imgs1) == 4
        assert pano2 and len(imgs2) == 1
        assert not pano3 and len(imgs3) == 0


def test_detect_images_road_name():
    """带路名文件名（{pid}_{路名}_{view}.jpg）同样被识别。"""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for s in ("left", "right", "front", "back"):
            _mk_img(d / f"p1_归昌路_{s}.jpg")
        _mk_img(d / "p2_中山路_pano.jpg")
        imgs1, pano1 = _detect_images(d, "p1")
        imgs2, pano2 = _detect_images(d, "p2")
        assert not pano1 and len(imgs1) == 4
        assert pano2 and len(imgs2) == 1


def test_build_map_three_modes():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        images = base / "StreetViewBatchDownload"
        images.mkdir()
        for s in ("left", "right", "front", "back"):
            _mk_img(images / f"p1_{s}.jpg")
        _mk_img(images / "p2_pano.jpg")
        # p3 无图

        out = base / "map.html"
        build_map(_mk_points(), images, report=None, out_html=out, tk="")

        html = out.read_text(encoding="utf-8")
        # lightbox 存在
        assert "svOpen" in html and "svlbimg" in html
        # p1 四方向缩略图（4 张 base64）+ 相对路径指向原图
        assert html.count("p1_left.jpg") >= 1
        assert html.count("data:image/jpeg;base64") >= 5  # 4 方向 + 1 全景
        # p2 全景
        assert "p2_pano.jpg" in html
        # p3 无图 → 灰点，popup 显示"未下载"
        assert "未下载" in html


def test_build_map_no_pano_status():
    """无图点从 report 读取状态（no_pano → 无街景）。"""
    import pandas as pd

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        images = base / "StreetViewBatchDownload"
        images.mkdir()
        _mk_img(images / "p1_left.jpg")
        report = pd.DataFrame([
            {"image_name": "p2", "status": "no_pano", "lat": 0, "lon": 0},
            {"image_name": "p3", "status": "download_failed", "lat": 0, "lon": 0},
        ])
        out = base / "map.html"
        build_map(_mk_points(), images, report=report, out_html=out, tk="")
        html = out.read_text(encoding="utf-8")
        assert "无街景" in html
        assert "下载失败" in html


def test_build_map_popup_road_name():
    """report 带 road_name 时，弹窗显示路名。"""
    import pandas as pd

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        images = base / "StreetViewBatchDownload"
        images.mkdir()
        _mk_img(images / "p1_归昌路_left.jpg")
        report = pd.DataFrame([
            {"image_name": "p1", "status": "ok", "road_name": "归昌路",
             "lat": 30.0, "lon": 120.0},
        ])
        out = base / "map.html"
        build_map(_mk_points(), images, report=report, out_html=out, tk="")
        html = out.read_text(encoding="utf-8")
        assert "归昌路" in html


def test_build_map_with_road_area_layers():
    """提供 roads/area 时，路网与研究范围作为可勾选 overlay 写入 LayerControl。

    注：folium 会把中文图层名转义成 \\uXXXX（如 路网=\\u8def\\u7f51），
    浏览器加载后自动还原，故断言转义形式。
    """
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        images = base / "StreetViewBatchDownload"
        images.mkdir()
        _mk_img(images / "p1_left.jpg")

        streets = gpd.GeoDataFrame(
            geometry=[LineString([(120.0, 30.0), (120.001, 30.0)])], crs="EPSG:4326")
        area = gpd.GeoDataFrame(
            geometry=[Polygon([(119.999, 29.999), (120.002, 29.999),
                               (120.002, 30.002), (119.999, 30.002)])],
            crs="EPSG:4326")

        out = base / "map.html"
        build_map(_mk_points(), images, report=None, out_html=out, tk="",
                  streets=streets, area=area)
        html = out.read_text(encoding="utf-8")
        assert "\\u8def\\u7f51" in html            # 路网
        assert "\\u7814\\u7a76\\u8303\\u56f4" in html  # 研究范围
        assert "control.layers" in html            # 图层控件（可勾选）
        # 自定义 pane：固定层序，取消勾选再勾选后路网/研究范围不会盖住采样点
        assert "createPane('sv-area')" in html
        assert "createPane('sv-road')" in html
        assert '"pane": "sv-area"' in html
        assert '"pane": "sv-road"' in html


def test_to_wgs84_converts_crs():
    """非 WGS84 图层（如 EPSG:4526 高斯投影）转 WGS84 后再叠加。"""
    # EPSG:3857（Web Mercator）下的一点，转回 4326 应为约 (120, 30)
    import pyproj
    tr = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x, y = tr.transform(120.0, 30.0)
    gdf = gpd.GeoDataFrame(geometry=[Point(x, y)], crs="EPSG:3857")
    out = _to_wgs84(gdf)
    assert out.crs.to_epsg() == 4326
    px, py = out.geometry.iloc[0].x, out.geometry.iloc[0].y
    assert abs(px - 120.0) < 1e-4 and abs(py - 30.0) < 1e-4


def test_find_aux_file_auto_discover():
    """自动发现路网/研究范围：有则返回，无则 None（有几个加几个）。"""
    from baidu_pano.cli import _find_aux_file

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        # 空目录：都找不到
        assert _find_aux_file(base, ["roads.gpkg"]) is None
        assert _find_aux_file(base, ["study_area.gpkg"]) is None
        # 只有 roads → 找到 roads，找不到 study_area
        (base / "roads.gpkg").write_bytes(b"")
        assert _find_aux_file(base, ["roads.gpkg", "roads.geojson"]).name == "roads.gpkg"
        assert _find_aux_file(base, ["study_area.gpkg"]) is None
        # 两个都有 → 各找到各的
        (base / "study_area.gpkg").write_bytes(b"")
        assert _find_aux_file(base, ["roads.gpkg"]).name == "roads.gpkg"
        assert _find_aux_file(base, ["study_area.gpkg"]).name == "study_area.gpkg"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("test_mapping 全部通过")
