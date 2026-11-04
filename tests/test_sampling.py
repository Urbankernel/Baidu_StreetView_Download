"""采样点生成单元测试。"""
from __future__ import annotations

import geopandas as gpd
from shapely.geometry import LineString, box

from baidu_pano.sampling import generate_points


def _streets(line: LineString, highway: str = "residential") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"highway": [highway], "name": ["t"], "osmid": [1]},
        geometry=[line], crs="EPSG:4326")


def test_generate_points_basic():
    # 纬度方向约 500m 的线，offset=15、spacing=40
    line = LineString([(120.0, 30.0), (120.0, 30.0045)])
    points, streets = generate_points(None, _streets(line), spacing_m=40, offset_m=15)
    assert len(points) > 0
    for col in ("point_id", "street_id", "Latitude", "Longitude"):
        assert col in points.columns
    assert 10 <= len(points) <= 14


def test_generate_points_clip_to_polygon():
    line = LineString([(120.0, 30.0), (120.0, 30.0045)])
    poly = box(119.99, 30.002, 120.01, 30.006)  # 只覆盖中部偏北
    points, _ = generate_points(poly, _streets(line), spacing_m=40, offset_m=15)
    assert len(points) > 0
    assert all(points.geometry.intersects(poly))


def test_generate_points_empty_after_filter():
    line = LineString([(120.0, 30.0), (120.0, 30.0045)])
    try:
        generate_points(None, _streets(line, "footway"),
                        spacing_m=40, offset_m=15, highway_types=["motorway"])
        assert False, "过滤后路网为空应报错"
    except ValueError:
        pass


def test_generate_points_too_short():
    # 短于 offset 的线不产生点
    line = LineString([(120.0, 30.0), (120.0, 30.00005)])  # 约 5m
    try:
        generate_points(None, _streets(line), spacing_m=40, offset_m=15)
        assert False, "路网过短应报错"
    except ValueError:
        pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("test_sampling 全部通过")
