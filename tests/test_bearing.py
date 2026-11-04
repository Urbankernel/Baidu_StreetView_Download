"""街道走向（bearing）与视角方位角单元测试。"""
from __future__ import annotations

from baidu_pano.bearing import canonical_bearing, get_headings_for_mode


def test_canonical_bearing_normalize():
    assert canonical_bearing(0.0) == 0.0
    assert canonical_bearing(90.0) == 90.0
    assert canonical_bearing(179.0) == 179.0
    assert canonical_bearing(180.0) == 0.0      # 180 归一化到 0
    assert canonical_bearing(270.0) == 90.0     # 270-180=90
    assert canonical_bearing(359.0) == 179.0


def test_headings_4_views():
    h = {suffix: heading for heading, suffix in get_headings_for_mode("4_views", 0.0)}
    assert h == {"left": 270.0, "right": 90.0, "front": 0.0, "back": 180.0}


def test_headings_left_right():
    # bearing=90：left=90+270=360%360=0，right=90+90=180
    h = {suffix: heading for heading, suffix in get_headings_for_mode("left_right", 90.0)}
    assert h == {"left": 0.0, "right": 180.0}


def test_headings_front_back():
    h = {suffix: heading for heading, suffix in get_headings_for_mode("front_back", 45.0)}
    assert h == {"front": 45.0, "back": 225.0}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("test_bearing 全部通过")
