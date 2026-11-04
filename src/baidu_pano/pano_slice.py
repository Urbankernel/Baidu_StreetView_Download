"""全景图裁剪 + 切视角。

逆向接口下载到的是一张 360° 等距柱状投影全景图。本模块负责：
  1. 按比例裁剪（去掉底部采集车顶 / 顶部天空）—— crop_bottom / crop_top；
  2. 按采样点 bearing 把全景切成 left/right/front/back 方向图（四视角模式）；
  3. 全景图模式则对整图裁剪后直接输出（不切方向）。
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .bearing import get_headings_for_mode

# 模拟官方静态图 API 的取景参数：相机俯仰 -20°、视场角 90°
PITCH_CENTER = -20.0
FOV_H = 90.0
FOV_V = 90.0
# 全景图第 0 列的方向约定（2026-10-17 真实抓图实测校准，推翻了旧假设
# "第 0 列 = 正北"）：百度全景图第 0 列 = 采集车朝向(Heading)的左侧，即绝对方位
# (Heading - 90°)。因此切方位角 h 的视角，实际列中心 = (h - Heading + 90)/360*W，
# 等价于给 h 加偏移 (90 - Heading)。该偏移随每个 panoid 的采集车朝向变化，
# 不是全局常量——reverse 流程对每个 panoid 调 qt=sdata 取 Heading 后传入
# slice_views(..., heading_offset=90 - Heading)；Heading 缺失时退化为 0 并告警。


def crop_streetview(img: Image.Image, bottom_ratio: float = 0.0,
                    top_ratio: float = 0.0) -> Image.Image:
    """按比例裁剪等距柱状投影全景图（照搬 baidu_streetview.py）。

    bottom_ratio：裁掉底部比例（0.4 = 去掉下方 40% 的采集车顶/车头区域）
    top_ratio   ：裁掉顶部比例（0.2 = 去掉上方 20% 的天空）
    两项均设为 0 可关闭裁剪，保留完整全景。
    """
    bottom_ratio = max(0.0, min(bottom_ratio, 0.9))
    top_ratio = max(0.0, min(top_ratio, 0.9))
    if top_ratio + bottom_ratio >= 1.0:
        raise ValueError("crop_top + crop_bottom 之和必须小于 1")
    w, h = img.size
    top = int(round(h * top_ratio))
    bottom = int(round(h * (1.0 - bottom_ratio)))
    if bottom - top < 1:
        return img
    return img.crop((0, top, w, bottom))


def slice_view(pano: Image.Image, heading: float,
               crop_bottom: float = 0.0, crop_top: float = 0.0,
               heading_offset: float = 0.0) -> Image.Image:
    """从等距柱状全景图切出一个方向的视角窗口（水平 fov 90°、俯仰 -65°~+25°）。

    等距柱状投影：宽 W 对应 360° 水平方位，高 H 对应 180° 垂直（顶部 +90°）。
    ``heading`` 为方位角（0=北，顺时针），窗口跨 0°/360° 边界时自动环绕。

    ``heading_offset``：全景图第 0 列并非正北（实测第 0 列 = 采集车朝向的左侧），
    传入 (90 - 采集车Heading) 做逐点修正；详见模块注释。0 表示不修正。

    crop_bottom / crop_top：先按比例裁掉底部车顶 / 顶部天空（等价于先调
    crop_streetview 再切片），取景窗口被裁剪范围约束——例如 crop_bottom=0.4
    时，窗口底部从 -65° 抬到 -18°，去掉更多采集车车顶区域。
    """
    W, H = pano.size
    h = (heading + heading_offset) % 360
    cx = h / 360.0 * W                       # 窗口中心列
    half = FOV_H / 360.0 * W / 2.0           # 半宽（像素）
    width = int(round(half * 2))
    x0 = cx - half
    # 垂直窗口：先按 crop 裁掉车顶/天空，再取 pitch [-65°, +25°]，并约束在裁剪范围内
    crop_bottom = max(0.0, min(crop_bottom, 0.9))
    crop_top = max(0.0, min(crop_top, 0.9))
    pitch_bottom = -90.0 + crop_bottom * 180.0      # 裁剪后最低俯仰
    pitch_top = 90.0 - crop_top * 180.0             # 裁剪后最高俯仰
    win_top = min(PITCH_CENTER + FOV_V / 2.0, pitch_top)        # 窗口上边界（+25° 或更低）
    win_bottom = max(PITCH_CENTER - FOV_V / 2.0, pitch_bottom)  # 窗口下边界（-65° 或更高）
    y0 = int(round((90.0 - win_top) / 180.0 * H))
    y1 = int(round((90.0 - win_bottom) / 180.0 * H))
    y1 = max(y1, y0 + 1)                     # 极端 crop 下保证非空
    # 水平滚动对齐到窗口起点，天然处理跨 0°/360° 环绕
    arr = np.asarray(pano)
    rolled = np.roll(arr, -int(round(x0)), axis=1)
    return Image.fromarray(rolled[y0:y1, 0:width, :])


def slice_views(pano: Image.Image, bearing: float, mode: str = "4_views",
                crop_bottom: float = 0.0, crop_top: float = 0.0,
                heading_offset: float = 0.0) -> dict[str, Image.Image]:
    """按采样点 bearing 把全景切成多视角。返回 {suffix: Image}。

    mode ∈ {"left_right", "front_back", "4_views"}（与 get_headings_for_mode 一致）。
    heading_offset：逐点方向修正（见 slice_view），由调用方按 sdata 的 Heading 计算。
    """
    return {suffix: slice_view(pano, heading, crop_bottom, crop_top, heading_offset)
            for heading, suffix in get_headings_for_mode(mode, bearing)}
