"""全景裁剪 / 切视角单元测试（含环绕与脏数据）。"""
from __future__ import annotations

import numpy as np
from PIL import Image

from baidu_pano.pano_slice import crop_streetview, slice_view, slice_views


def make_pano(w: int = 1024, h: int = 512) -> Image.Image:
    """合成全景：左半红、右半蓝，便于肉眼/像素验证窗口位置。"""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, : w // 2] = [255, 0, 0]
    arr[:, w // 2:] = [0, 0, 255]
    return Image.fromarray(arr)


def test_crop_bottom():
    out = crop_streetview(make_pano(), 0.4, 0.0)
    assert out.size == (1024, 307)  # 512*(1-0.4)=307.2 → round=307


def test_crop_top():
    out = crop_streetview(make_pano(), 0.0, 0.2)
    assert out.size == (1024, 410)  # 512*(1-0.2)=409.6 → round=410


def test_crop_disabled():
    out = crop_streetview(make_pano(), 0.0, 0.0)
    assert out.size == (1024, 512)


def test_crop_invalid_ratio():
    try:
        crop_streetview(make_pano(), 0.6, 0.5)  # 0.6+0.5 >= 1
        assert False, "应当抛 ValueError"
    except ValueError:
        pass


def test_slice_view_width():
    # 90° 水平窗口 = W/4 = 256px
    assert slice_view(make_pano(), 0.0).size[0] == 256
    assert slice_view(make_pano(), 90.0).size[0] == 256


def test_slice_view_wraparound():
    # heading=350 跨 0°/360° 环绕，不应报错且宽度正确
    img = slice_view(make_pano(), 350.0)
    assert img.size[0] == 256


def test_slice_views_keys():
    assert set(slice_views(make_pano(), 0.0, "4_views").keys()) == \
        {"left", "right", "front", "back"}
    assert set(slice_views(make_pano(), 0.0, "left_right").keys()) == \
        {"left", "right"}
    assert set(slice_views(make_pano(), 0.0, "front_back").keys()) == \
        {"front", "back"}


def test_slice_extreme_crop_not_empty():
    """极端 crop（如 crop_bottom=0.9）下窗口不应为空、不应崩溃。"""
    img = slice_view(make_pano(), 0.0, crop_bottom=0.9, crop_top=0.0)
    assert img.size[0] == 256 and img.size[1] >= 1


def test_slice_view_heading_offset():
    """heading_offset 逐点方向校准：offset 应把窗口在全景上平移。

    合成全景左半红（0~511px，即方位角 0~180°）、右半蓝（512~1023px，
    方位角 180~360°）。heading=0：
      offset=90  -> h=90  -> 窗口 128~384px -> 全红
      offset=270 -> h=270 -> 窗口 640~896px -> 全蓝
    """
    pano = make_pano()
    red = np.asarray(slice_view(pano, 0.0, heading_offset=90.0)).astype(int)
    blue = np.asarray(slice_view(pano, 0.0, heading_offset=270.0)).astype(int)
    assert red[..., 0].mean() > 250 and red[..., 2].mean() < 5
    assert blue[..., 2].mean() > 250 and blue[..., 0].mean() < 5


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("test_slice 全部通过")
