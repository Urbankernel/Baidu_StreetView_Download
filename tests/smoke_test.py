"""端到端冒烟测试：无网络、无密钥。

覆盖：采样 → mock 逆向下载（四视角 / 全景 / 断点续跑 / no_pano / CSV 输入）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
from PIL import Image
from shapely.geometry import LineString

from baidu_pano.sampling import generate_points, save_case_gpkg
import baidu_pano.download as dl


def make_pano(w: int = 1024, h: int = 512) -> Image.Image:
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, : w // 2] = [255, 0, 0]
    arr[:, w // 2:] = [0, 0, 255]
    return Image.fromarray(arr)


def make_fake_client_cls(nopano_cond, car_heading: float = 0.0,
                         rname: str | None = None, shot_date: str | None = None):
    """返回一个 Fake 逆向客户端类。nopano_cond(x, y) 为真时该坐标判无街景。

    car_heading 模拟 qt=sdata 返回的采集车朝向，用于验证逐点方向校准传导；
    rname 模拟 sdata 返回的道路名 Rname；shot_date 模拟拍摄日期 Date——两者用于
    验证路名/拍摄时间写入文件名与报告。
    """

    class Fake:
        def __init__(self, *a, **k):
            pass

        def get_panoid(self, x, y):
            if nopano_cond(x, y):
                return None
            return "fake_panoid"

        def find_nearest_panoid(self, x, y, max_radius=5.0):
            return "fake_panoid", 3.0

        def get_camera_info(self, panoid):
            info = {"Heading": car_heading, "Pitch": 0.0}
            if rname:
                info["Rname"] = rname
            if shot_date:
                info["Date"] = shot_date
            return info

        def download_panorama(self, panoid, level=3, progress_cb=None):
            return make_pano()

    return Fake


def patch_fake(nopano_cond=lambda x, y: False, car_heading: float = 0.0,
               rname: str | None = None, shot_date: str | None = None):
    dl.BaiduReverseClient = make_fake_client_cls(nopano_cond, car_heading, rname, shot_date)
    dl.wgs84_to_bd09mc = lambda lon, lat: (lon, lat)


def build_sample_gpkg(tmp: str) -> tuple[Path, gpd.GeoDataFrame]:
    line = LineString([(120.0, 30.0), (120.0, 30.0045)])
    streets = gpd.GeoDataFrame(
        {"highway": ["residential"], "name": ["t"], "osmid": [1]},
        geometry=[line], crs="EPSG:4326")
    points, streets = generate_points(None, streets, spacing_m=40, offset_m=15)
    gpkg = Path(tmp) / "StreetSamples" / "street_samples.gpkg"
    save_case_gpkg(points, streets, gpkg)
    return gpkg, points


def test_reverse_4_views():
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        patch_fake()
        out = Path(tmp) / "StreetViewBatchDownload"
        report = dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="4_views")
        n = len(points)
        assert len(list(out.glob("point_*_*.jpg"))) == n * 4
        assert set(report["status"]) == {"ok"}


def test_reverse_breakpoint_resume():
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        patch_fake()
        out = Path(tmp) / "StreetViewBatchDownload"
        dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="4_views")
        report2 = dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="4_views")
        # 跳过的点也写报告（status=ok），保证报告覆盖后不丢已下载点信息
        assert len(report2) == len(points)
        assert set(report2["status"]) == {"ok"}


def test_reverse_panorama():
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        patch_fake()
        out = Path(tmp) / "StreetViewBatchDownload"
        report = dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="panorama")
        assert len(list(out.glob("point_*_pano.jpg"))) == len(points)


def test_reverse_no_pano():
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        # 前半段（lat < 30.002）判无街景；关闭就近搜索，直接暴露 no_pano
        patch_fake(lambda x, y: y < 30.002)
        out = Path(tmp) / "StreetViewBatchDownload"
        report = dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="4_views",
                                                search_radius=0)
        statuses = set(report["status"])
        assert "no_pano" in statuses and "ok" in statuses


def test_reverse_csv_input():
    with tempfile.TemporaryDirectory() as tmp:
        csv = Path(tmp) / "points.csv"
        csv.write_text("name,lng,lat\np1,120.0,30.0\np2,120.001,30.001\n",
                       encoding="utf-8-sig")
        patch_fake()
        out = Path(tmp) / "StreetViewBatchDownload"
        report = dl.download_streetview_reverse(out, points_csv=csv, mode="4_views")
        assert len(report) == 2
        assert (out / "p1_left.jpg").exists()
        assert (out / "p2_right.jpg").exists()


def test_reverse_heading_calibration():
    """方向校准传导：car_heading 变化时 front 图内容应按 (90-Heading) 平移。

    slice_view 对 offset 的像素级行为由 test_slice 用可控几何验证；这里只断言
    端到端传导（sdata Heading -> slice offset）真实生效：不同 Heading 产出
    不同内容的 front 图，且 report 记录了 car_heading。
    """
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, _ = build_sample_gpkg(tmp)
        patch_fake(car_heading=90.0)
        out1 = Path(tmp) / "out1"
        r1 = dl.download_streetview_reverse(out1, gpkg_path=gpkg, mode="front_back")
        assert set(r1["status"]) == {"ok"}
        assert "car_heading" in r1.columns
        assert (r1["car_heading"] == 90.0).all()
        f1 = np.asarray(Image.open(out1 / "point_1_front.jpg")).astype(int)

        patch_fake(car_heading=180.0)
        out2 = Path(tmp) / "out2"
        dl.download_streetview_reverse(out2, gpkg_path=gpkg, mode="front_back")
        f2 = np.asarray(Image.open(out2 / "point_1_front.jpg")).astype(int)
        assert np.abs(f1 - f2).mean() > 10  # offset 差 180°，内容应显著平移


def test_reverse_sdata_missing_heading():
    """降级路径：sdata 返回 None 时不崩溃、按旧约定（offset=0）切片，report 无 car_heading。"""
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        # 用返回 None 的 get_camera_info 覆盖 Fake
        base = make_fake_client_cls(lambda x, y: False)
        class FakeNoSdata(base):
            def get_camera_info(self, panoid):
                return None
        dl.BaiduReverseClient = FakeNoSdata
        dl.wgs84_to_bd09mc = lambda lon, lat: (lon, lat)
        out = Path(tmp) / "StreetViewBatchDownload"
        report = dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="front_back")
        assert set(report["status"]) == {"ok"}
        assert "car_heading" not in report.columns
        assert (out / "point_1_front.jpg").exists()


def test_reverse_nearest_search():
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        # 坐标本身无街景，但就近搜索（search_radius>0）找回
        patch_fake(lambda x, y: True)
        out = Path(tmp) / "StreetViewBatchDownload"
        report = dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="4_views",
                                                search_radius=5)
        assert set(report["status"]) == {"ok"}
        assert report["message"].str.contains("就近匹配", na=False).all()


def test_reverse_road_name_in_filename():
    """路名写入文件名与报告：sdata 返回 Rname 时四视角文件名含路名，report 含 road_name。"""
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        patch_fake(rname="归昌路")
        out = Path(tmp) / "StreetViewBatchDownload"
        report = dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="4_views")
        n = len(points)
        assert (out / "point_1_归昌路_left.jpg").exists()
        assert len(list(out.glob("point_*_归昌路_*.jpg"))) == n * 4
        assert "road_name" in report.columns
        assert (report["road_name"] == "归昌路").all()


def test_reverse_road_name_panorama():
    """panorama 模式同样写入路名文件名。"""
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        patch_fake(rname="归昌路")
        out = Path(tmp) / "StreetViewBatchDownload"
        dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="panorama")
        assert len(list(out.glob("point_*_归昌路_pano.jpg"))) == len(points)


def test_reverse_road_name_illegal_chars():
    """路名含文件系统非法字符时清洗掉，文件名仍合法。"""
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        patch_fake(rname="中山/路:辅路")
        out = Path(tmp) / "StreetViewBatchDownload"
        dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="panorama")
        # "/" 与 ":" 被去掉 → 中山路辅路
        assert (out / "point_1_中山路辅路_pano.jpg").exists()


def test_reverse_shot_date_in_filename():
    """拍摄日期写入文件名与报告：{point_id}_{路名}_{Date}_{方向}.jpg。"""
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        patch_fake(rname="归昌路", shot_date="20190519")
        out = Path(tmp) / "StreetViewBatchDownload"
        report = dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="4_views")
        n = len(points)
        assert (out / "point_1_归昌路_20190519_left.jpg").exists()
        assert len(list(out.glob("point_*_归昌路_20190519_*.jpg"))) == n * 4
        assert "shot_date" in report.columns
        assert (report["shot_date"] == "20190519").all()


def test_reverse_shot_date_only():
    """只有拍摄日期、无路名时，文件名退化为 {point_id}_{Date}_{方向}.jpg。"""
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        patch_fake(shot_date="20190519")
        out = Path(tmp) / "StreetViewBatchDownload"
        dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="panorama")
        assert (out / "point_1_20190519_pano.jpg").exists()


def test_reverse_resume_parses_road_date():
    """断点续跑跳过的点从文件名恢复路名/拍摄日期写入报告（弹窗路名来源）。"""
    with tempfile.TemporaryDirectory() as tmp:
        gpkg, points = build_sample_gpkg(tmp)
        patch_fake(rname="凌河路", shot_date="20200221")
        out = Path(tmp) / "StreetViewBatchDownload"
        dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="panorama")
        # 第二次全跳过，且模拟 sdata 拿不到路名——报告信息应从文件名解析恢复
        patch_fake(rname=None, shot_date=None)
        report2 = dl.download_streetview_reverse(out, gpkg_path=gpkg, mode="panorama")
        assert len(report2) == len(points)
        row = report2[report2["image_name"] == "point_1"].iloc[0]
        assert row["road_name"] == "凌河路"
        assert row["shot_date"] == "20200221"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("smoke_test 全部通过")
