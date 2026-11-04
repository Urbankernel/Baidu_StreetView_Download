"""百度街景全景逆向接口客户端。

替代官方「全景静态图 API」——个人开发者无调用权限，且官方版每点要发
4 次请求、只给 640×640 低清方向图。本模块走逆向接口（mapsv0.bdimg.com
的 qt=qsdata/pdata，无需 AK）：

    WGS84 经纬度
      → BD09MC 百度墨卡托坐标（coords.wgs84_to_bd09mc）
      → qsdata 拿 panoid（全景唯一标识）
      → pdata 逐块下载瓦片
      → Pillow 拼接为等距柱状投影全景图（8192×4096 级别可调）

坐标纪律：WGS84 → GCJ-02 → BD-09 → BD09MC 转换链仅用于发请求（逆向接口
要求 BD09MC 墨卡托坐标），成果点位仍是 WGS84，与官方 API 路线一致。

参考（已跑通的逆向脚本）：F:/城市研究领域Agent/baidu_streetview.py
"""
from __future__ import annotations

import math
import time
from io import BytesIO

import requests
from PIL import Image

# 各清晰度级别瓦片布局（ImgLevel -> (列, 行, 请求时 z 值)）
LEVEL_MAP = {
    1: (2, 1, 2),
    2: (4, 2, 3),
    3: (8, 4, 4),
    4: (16, 8, 5),
}
TILE_SIZE = 512

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0.0.0 Safari/537.36"),
    "Referer": "https://map.baidu.com/",
}


class BaiduReverseClient:
    """逆向接口客户端：qsdata 拿 panoid + pdata 逐瓦片下载拼接全景。"""

    def __init__(self, sleep_seconds: float = 0.3, timeout: int = 30,
                 max_retries: int = 2):
        self.session = requests.Session()
        self.sleep_seconds = sleep_seconds
        self.timeout = timeout
        self.max_retries = max_retries

    def _get(self, url: str):
        for attempt in range(self.max_retries + 1):
            try:
                return self.session.get(url, headers=HEADERS,
                                        timeout=self.timeout)
            except Exception:
                if attempt < self.max_retries:
                    time.sleep(self.sleep_seconds)
                    continue
                raise

    def get_panoid(self, mc_x: float, mc_y: float):
        """qt=qsdata：用 BD09MC 墨卡托坐标取 panoid。无街景返回 None。

        错误分类：``error==0`` 成功；``error==404`` 该坐标无街景（不重试）；
        其他 error（限流/服务端抖动）重试 ``max_retries`` 次再判定——批量下载
        时偶发撞上 error 会被误判为"无街景"，重试可显著降低误判。
        """
        url = "https://mapsv0.bdimg.com/?qt=qsdata&x=%.6f&y=%.6f" % (mc_x, mc_y)
        for attempt in range(self.max_retries + 1):
            data = self._get(url).json()
            err = data.get("result", {}).get("error")
            if err == 0:
                return data.get("content", {}).get("id")
            if err == 404:
                return None  # 真无街景，无需重试
            if attempt < self.max_retries:  # 限流/抖动：稍候重试
                time.sleep(self.sleep_seconds)
        return None

    def get_camera_info(self, panoid: str) -> dict | None:
        """qt=sdata：取采集车拍摄元数据（Heading/Pitch/拍摄日期等）。失败返回 None。

        其中 ``Heading`` 是采集车拍摄时的朝向（0=正北，顺时针，度），是全景图
        方向校准的关键真值：百度约定全景图第 0 列 = Heading 的左侧（即绝对方位
        Heading-90°），所以切片时需要逐点用 (90 - Heading) 修正，见 pano_slice。

        与 get_panoid 一致地重试：``error != 0``（限流/服务端抖动）与网络异常都
        重试 ``max_retries`` 次。批量下载 233 点 × 33 请求下 sdata 偶发抖动（实测
        约 2.6%），不重试会把个别点误判为"无 Heading"、按旧约定错切方向。
        """
        url = "https://mapsv0.bdimg.com/?qt=sdata&sid=%s" % panoid
        for attempt in range(self.max_retries + 1):
            try:
                data = self._get(url).json()
            except Exception:
                data = None
            if data is not None and data.get("result", {}).get("error") == 0:
                content = data.get("content")
                # 结构正常但无内容：panoid 存在却查不到元数据，重试无意义
                return content[0] if isinstance(content, list) and content else None
            if attempt < self.max_retries:
                time.sleep(self.sleep_seconds)
        return None

    def find_nearest_panoid(self, mc_x: float, mc_y: float,
                            max_radius: float = 5.0):
        """在坐标周围环形搜索最近的街景点，返回 (panoid, 距离米) 或 (None, None)。

        从近到远分档（max_radius 的 40%/70%/100%）采样 8 个方向，避免固定档位
        在 max_radius 较小时（如 5 米）被全部 break 掉导致搜索失效。BD09MC 在
        几十米范围内近似等距，直接用米做偏移。
        """
        for frac in (0.4, 0.7, 1.0):
            radius = max_radius * frac
            for angle in range(0, 360, 45):
                rad = math.radians(angle)
                xx = mc_x + radius * math.cos(rad)
                yy = mc_y + radius * math.sin(rad)
                pid = self.get_panoid(xx, yy)
                if pid:
                    return pid, radius
                time.sleep(self.sleep_seconds)
        return None, None

    def download_tile(self, panoid: str, row: int, col: int, z: int) -> Image.Image:
        """qt=pdata：下载单块瓦片。"""
        url = ("https://mapsv0.bdimg.com/?qt=pdata&sid=%s&pos=%d_%d&z=%d"
               % (panoid, row, col, z))
        resp = self._get(url)
        return Image.open(BytesIO(resp.content))

    def download_panorama(self, panoid: str, level: int = 3,
                          progress_cb=None) -> Image.Image:
        """下载并拼接指定清晰度级别的完整全景图（等距柱状投影）。

        progress_cb(done, total)：每下载完一块瓦片回调一次，用于终端进度显示；
        传 None 则静默。
        """
        cols, rows, z = LEVEL_MAP[level]
        total = cols * rows
        pano = Image.new("RGB", (cols * TILE_SIZE, rows * TILE_SIZE))
        done = 0
        for row in range(rows):
            for col in range(cols):
                tile = self.download_tile(panoid, row, col, z)
                pano.paste(tile, (col * TILE_SIZE, row * TILE_SIZE))
                done += 1
                if progress_cb is not None:
                    progress_cb(done, total)
                time.sleep(self.sleep_seconds)
        return pano
