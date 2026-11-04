"""官方全景静态图 API 客户端（备选路线，需 BAIDU_AK）。

相对逆向接口的差异（原因见 README 关键设计）：
  1. 百度无免费 Metadata API -> 取消"先查询后下载"，改为直接下载 + 判空。
     百度对无覆盖点返回均色占位图，命中单像素色占比判空规则。
  2. 拿不到 pano_id -> 用影像内容哈希去重替代 pano_id 去重。
  3. 百度不返回真实相机坐标 -> 不做吸附回写，点位保持原始位置。

坐标纪律：请求统一带 coordtype=wgs84ll，输入输出全程 WGS84。
"""
from __future__ import annotations

import time
from io import BytesIO

import numpy as np
import requests
from PIL import Image as PILImage

# 百度错误码中属于"配置类错误"的：换 AK、重试都没用，必须立即终止并给出指引
_FATAL_STATUS = {
    101: "AK 参数不存在或错误：请核对 .env 中的 BAIDU_AK 是否完整、无多余空格",
    102: "MCODE 安全码校验失败：检查应用的安全码（SN）设置，或创建应用时不设校验",
    200: "APP 不存在：AK 无效，请到控制台核对",
    201: "APP 已被禁用：到百度开放平台控制台启用该应用",
    202: "APP 已被管理员删除：请重新创建应用",
    210: "APP IP 白名单校验失败：到应用设置里把 IP 白名单清空或加入本机 IP",
    211: "APP SN 校验失败：服务端应用不要填 SN 校验，或在应用设置中关闭",
    220: "Referer 白名单校验失败：服务端应用不应设置 Referer 白名单",
    230: "APP MCODE 校验失败：检查应用类型与安全码设置",
    240: "APP 服务被禁用：该应用没有开通『全景静态图』服务。"
         "到百度开放平台控制台 -> 应用管理 -> 我的应用 -> 设置，"
         "在『选择服务』中勾选全景静态图 API；若列表里没有，"
         "需先在控制台的服务列表中申请开通该服务",
    250: "用户不存在：AK 对应的账号状态异常",
    260: "密钥错误：AK 复制不完整或已重置，请到控制台重新获取",
}


class BaiduConfigError(RuntimeError):
    """百度侧配置错误（AK/应用/服务未开通），继续跑只会全部失败。"""


class BaiduPanoClient:
    """百度全景静态图 API 客户端：多 AK 轮换 + 限速 + 判空。"""

    def __init__(
        self,
        aks: list[str],
        base_url: str = "https://api.map.baidu.com/panorama/v2",
        width: int = 640,
        height: int = 640,
        request_interval: float = 0.2,
        timeout: int = 15,
    ):
        if not aks:
            raise ValueError("未配置百度 AK：设置环境变量 BAIDU_AK（多个用英文逗号分隔）")
        self.aks = aks
        self.base_url = base_url
        self.width = width
        self.height = height
        self.request_interval = request_interval
        self.timeout = timeout
        self._ak_index = 0
        self._last_request_ts = 0.0

    def _next_ak(self) -> str:
        ak = self.aks[self._ak_index % len(self.aks)]
        self._ak_index += 1
        return ak

    def _throttle(self) -> None:
        import time as _t

        wait = self.request_interval - (_t.time() - self._last_request_ts)
        if wait > 0:
            _t.sleep(wait)
        self._last_request_ts = _t.time()

    def fetch(self, lat: float, lon: float, heading: float, pitch: int, fov: int,
              max_retries: int = 3) -> PILImage.Image | None:
        """下载一张全景切片。无覆盖/失败返回 None。

        注意百度 location 参数为 经度在前（与 Google 相反）。
        """
        params = (
            f"location={lon},{lat}&coordtype=wgs84ll"
            f"&width={self.width}&height={self.height}"
            f"&heading={heading:.1f}&pitch={pitch}&fov={fov}"
        )
        last_err: Exception | None = None
        for attempt in range(max_retries):
            ak = self._next_ak()
            url = f"{self.base_url}?ak={ak}&{params}"
            try:
                self._throttle()
                resp = requests.get(url, timeout=self.timeout)
                ctype = resp.headers.get("Content-Type", "")
                if resp.status_code == 200 and "image" in ctype:
                    return PILImage.open(BytesIO(resp.content)).convert("RGB")
                # 非图片响应：解析百度错误码，区分配置错误（立即终止）与配额/临时错误（换 AK 重试）
                resp.encoding = "utf-8"  # 修正乱码，让错误信息可读
                try:
                    err = resp.json()
                    status = int(err.get("status", -1))
                    message = err.get("message", "")
                except Exception:
                    status, message = -1, resp.text[:200]
                if status in _FATAL_STATUS:
                    raise BaiduConfigError(
                        f"百度接口配置错误（status={status} {message}）：\n"
                        f"  {_FATAL_STATUS[status]}\n"
                        f"  修复后重新运行同一指令即可（断点续跑，已下载的不会重来）")
                if status in (301, 302):
                    last_err = RuntimeError(
                        f"配额超限（status={status}）：换 AK 重试；全部 AK 用尽请明天再跑（断点续跑）")
                else:
                    last_err = RuntimeError(
                        f"HTTP {resp.status_code} status={status}: {message}")
            except BaiduConfigError:
                raise
            except Exception as e:  # 网络异常
                last_err = e
            time.sleep(min(2 ** attempt, 8))
        print(f"  [warn] 下载失败 lat={lat},lon={lon},heading={heading}: {last_err}")
        return None


def is_blank_image(image: PILImage.Image, threshold: float = 0.9) -> bool:
    """单像素色占比 > threshold 判为空图/占位图（沿用上游规则）。

    百度"暂无全景"占位图为均色灰图，天然命中此规则。
    """
    arr = np.array(image)
    _, counts = np.unique(arr.reshape(-1, 3), axis=0, return_counts=True)
    return counts.max() / counts.sum() > threshold
