"""坐标转换单元测试（pytest 风格，也可 python 直接运行）。"""
from __future__ import annotations

from baidu_pano.coords import (
    wgs84_to_gcj02, gcj02_to_wgs84, wgs84_to_bd09, bd09_to_wgs84,
    wgs84_to_bd09mc, haversine_m,
)


def test_bd09mc_tiananmen_scale():
    """天安门 BD09MC 量级：x 千万级、y 百万级（与官方算法量级一致）。"""
    x, y = wgs84_to_bd09mc(116.3974, 39.9087)
    assert 1.29e7 < x < 1.30e7, f"x={x}"
    assert 4.7e6 < y < 4.9e6, f"y={y}"


def test_gcj02_roundtrip():
    lng, lat = 121.4737, 31.2304  # 上海
    glng, glat = wgs84_to_gcj02(lng, lat)
    # 国内有偏移（约几百米），不应等于原值
    assert abs(glng - lng) > 1e-4 or abs(glat - lat) > 1e-4
    # 逆变换为一次迭代近似，误差约 1~2 米
    rlng, rlat = gcj02_to_wgs84(glng, glat)
    assert abs(rlng - lng) < 2e-5
    assert abs(rlat - lat) < 2e-5


def test_out_of_china_identity():
    """境外坐标不偏移。"""
    lng, lat = -122.4194, 37.7749  # 旧金山
    assert wgs84_to_gcj02(lng, lat) == (lng, lat)
    assert wgs84_to_bd09(lng, lat) == (lng, lat)


def test_bd09_roundtrip():
    lng, lat = 121.4737, 31.2304
    blng, blat = wgs84_to_bd09(lng, lat)
    rlng, rlat = bd09_to_wgs84(blng, blat)
    # gcj02_to_wgs84 为一次迭代近似，叠加 bd09 往返后误差约 1~2 米
    assert abs(rlng - lng) < 2e-5
    assert abs(rlat - lat) < 2e-5


def test_haversine():
    # 北京到上海约 1067 km
    d = haversine_m(39.9087, 116.3974, 31.2304, 121.4737)
    assert 1.0e6 < d < 1.15e6, f"d={d}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("test_coords 全部通过")
