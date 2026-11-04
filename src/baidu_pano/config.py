"""全局配置：路径、密钥、服务端点。

密钥三级来源（优先级从高到低）：
  1. 配置文件 ``keys`` 字段
  2. ``key_file`` 文件（每行「字段=值」，# 开头为注释，如 configs/keys.txt）
  3. 环境变量（BAIDU_AK / TIANDITU_TK / OVERPASS_URL）

命令行参数覆盖 YAML 同名项。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv 为可选依赖
    pass


# 凭证字段名 → 环境变量名
_ENV_MAP = {
    "baidu_ak": "BAIDU_AK",
    "tianditu_tk": "TIANDITU_TK",
    "overpass_url": "OVERPASS_URL",
}


def _split_keys(raw: str) -> list[str]:
    return [k.strip() for k in raw.replace(";", ",").split(",") if k.strip()]


@dataclass
class Config:
    """运行配置。默认值即可用，全部可被环境变量覆盖。"""

    # ---- 存储 ----
    base_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("BAIDU_PANO_BASE", "./data"))
    )

    # ---- 百度全景（官方静态图 API，备选；逆向接口无需 AK）----
    # 支持多个 AK 轮换，英文逗号/分号分隔
    baidu_aks: list[str] = field(
        default_factory=lambda: _split_keys(os.environ.get("BAIDU_AK", ""))
    )
    baidu_pano_url: str = "https://api.map.baidu.com/panorama/v2"

    # ---- Overpass（可切国内可达镜像；或用本地数据替代，见 sampling.py）----
    overpass_url: str = field(
        default_factory=lambda: os.environ.get(
            "OVERPASS_URL", "https://overpass-api.de/api/interpreter"
        )
    )

    # ---- 天地图（map 命令生成互动地图底图用；无 tk 时退回无瓦片模式）----
    tianditu_tk: str = field(
        default_factory=lambda: os.environ.get("TIANDITU_TK", "")
    )

    # ---- 目录约定（与上游 SAGAI 的目录结构对应）----
    @property
    def samples_dir(self) -> Path:
        return self.base_dir / "StreetSamples"

    def images_dir(self) -> Path:
        return self.base_dir / "StreetViewBatchDownload"

    def gpkg_path(self) -> Path:
        return self.samples_dir / "street_samples.gpkg"

    def ensure_dirs(self) -> None:
        self.samples_dir.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------
# YAML 配置加载（对齐 get_aoi 的「配置文件驱动」模式）
# ------------------------------------------------------------------
def _read_key_file(key_file: str | Path) -> dict[str, str]:
    """读取 key_file（每行「字段=值」，# 开头为注释）。文件不存在返回空。"""
    creds: dict[str, str] = {}
    p = Path(key_file).expanduser()
    if not p.exists():
        return creds
    with open(p, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                creds[k] = v
    return creds


def _resolve_credentials(raw: dict) -> dict[str, str]:
    """三级来源合并凭证：配置文件 keys > key_file > 环境变量。"""
    creds: dict[str, str] = {}

    # 1) 配置文件 keys 字段
    keys = raw.get("keys") or {}
    if isinstance(keys, dict):
        for k, v in keys.items():
            if v and str(v).strip():
                creds[str(k).strip()] = str(v).strip()

    # 2) key_file 文件（不覆盖 YAML 里已有的）
    key_file = raw.get("key_file")
    if key_file:
        for k, v in _read_key_file(key_file).items():
            if k not in creds:
                creds[k] = v

    # 3) 环境变量
    for field, env_name in _ENV_MAP.items():
        v = os.environ.get(env_name, "").strip()
        if v and field not in creds:
            creds[field] = v

    return creds


def _load_yaml(path: str | Path) -> dict:
    """读取 YAML 文件（安全加载）。"""
    import yaml

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def config_from_yaml(path: str | Path, base_dir: str | Path | None = None) -> tuple[Config, dict]:
    """从 YAML 加载配置，返回 (Config, raw)。

    密钥走三级来源；base_dir 由 CLI 显式传入时优先（避免误用 YAML 缺省值），
    否则用 YAML 里的 base_dir，最后才退环境变量/默认。
    """
    raw = _load_yaml(path)
    creds = _resolve_credentials(raw)

    if base_dir is not None:
        resolved_base = Path(base_dir).expanduser()
    elif raw.get("base_dir"):
        resolved_base = Path(str(raw["base_dir"])).expanduser()
    else:
        resolved_base = Path(os.environ.get("BAIDU_PANO_BASE", "./data"))

    cfg = Config(
        base_dir=resolved_base,
        baidu_aks=_split_keys(creds.get("baidu_ak", "")),
        overpass_url=creds.get("overpass_url")
        or os.environ.get("OVERPASS_URL")
        or "https://overpass-api.de/api/interpreter",
        tianditu_tk=creds.get("tianditu_tk", ""),
    )
    return cfg, raw
