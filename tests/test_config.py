"""config + cli 配置驱动单元测试（无网络、无密钥）。

覆盖：YAML 加载、keys.txt 三级来源合并、CLI 参数覆盖 YAML、run 编排、
无 -c 时回填默认值、run 缺 -c 报错。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from baidu_pano.config import (Config, _read_key_file, _resolve_credentials,
                               config_from_yaml)
from baidu_pano import cli


# ---------- keys.txt ----------
def test_read_key_file():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "keys.txt"
        p.write_text(
            "# 注释\n"
            "baidu_ak=abc123\n"
            "tianditu_tk=tk456\n"
            "overpass_url=https://overpass.example.com\n"
            "\n"
            "bad_line_without_equals\n",
            encoding="utf-8",
        )
        creds = _read_key_file(p)
        assert creds["baidu_ak"] == "abc123"
        assert creds["tianditu_tk"] == "tk456"
        assert creds["overpass_url"] == "https://overpass.example.com"
        assert "bad_line_without_equals" not in creds


def test_read_key_file_missing():
    with tempfile.TemporaryDirectory() as tmp:
        assert _read_key_file(Path(tmp) / "nope.txt") == {}


def test_resolve_credentials_priority():
    """优先级：YAML keys > key_file > 环境变量。"""
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmp:
        key_file = Path(tmp) / "keys.txt"
        key_file.write_text("baidu_ak=from_file\ntianditu_tk=tk_file\n", encoding="utf-8")
        env = {"BAIDU_AK": "from_env", "TIANDITU_TK": "tk_env"}

        with patch.dict("os.environ", env):
            # 三级都有：YAML 最优先
            raw = {"keys": {"baidu_ak": "from_yaml"},
                   "key_file": str(key_file)}
            creds = _resolve_credentials(raw)
            assert creds["baidu_ak"] == "from_yaml"
            assert creds["tianditu_tk"] == "tk_file"  # YAML 无 → key_file

            # 无 YAML keys：key_file 优先于环境变量
            raw2 = {"key_file": str(key_file)}
            creds2 = _resolve_credentials(raw2)
            assert creds2["baidu_ak"] == "from_file"
            assert creds2["tianditu_tk"] == "tk_file"

            # 无 key_file：环境变量兜底
            creds3 = _resolve_credentials({})
            assert creds3["baidu_ak"] == "from_env"


# ---------- YAML 加载 ----------
def _write_yaml(tmp: Path, content: str) -> Path:
    p = tmp / "cfg.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_config_from_yaml_base_dir():
    with tempfile.TemporaryDirectory() as tmp:
        y = _write_yaml(Path(tmp), "base_dir: ./my_data\nkey_file: nope\n")
        cfg, raw = config_from_yaml(y)
        assert cfg.base_dir == Path("./my_data")
        assert raw["base_dir"] == "./my_data"


def test_config_from_yaml_base_dir_override():
    with tempfile.TemporaryDirectory() as tmp:
        y = _write_yaml(Path(tmp), "base_dir: ./my_data\n")
        cfg, _ = config_from_yaml(y, base_dir="./cli_override")
        assert cfg.base_dir == Path("./cli_override")


def test_config_from_yaml_baidu_aks():
    with tempfile.TemporaryDirectory() as tmp:
        y = _write_yaml(Path(tmp), 'keys:\n  baidu_ak: "k1,k2"\n')
        cfg, _ = config_from_yaml(y)
        assert cfg.baidu_aks == ["k1", "k2"]


# ---------- CLI 覆盖与默认值 ----------
def _parse(argv: list[str]):
    import sys
    # 直接调 main 会执行下载，这里只测参数解析层，改用 parser 手动解析
    return cli


def test_apply_config_cli_overrides_yaml():
    """命令行已传的参数不被 YAML 覆盖。"""
    import argparse

    raw = {"download": {"mode": "panorama", "interval": 9.9}}
    args = argparse.Namespace(mode="4_views")  # 命令行显式传了 mode
    cli._apply_config(args, raw, "download")
    assert args.mode == "4_views"          # CLI 优先
    assert args.interval == 9.9            # 未传 → YAML
    assert args.source == "reverse"        # 都无 → 默认
    assert args.pano_level == 3


def test_apply_config_no_yaml_fills_defaults():
    import argparse

    args = argparse.Namespace()  # 什么都没传
    cli._apply_config(args, {}, "download")
    assert args.mode == "4_views"
    assert args.source == "reverse"
    assert args.crop_bottom == 0.4
    assert args.no_dedup is False


def test_ns_from_yaml():
    ns = cli._ns_from_yaml({"sample": {"spacing": 100, "highway": ["residential"]}},
                           "sample")
    assert ns.spacing == 100
    assert ns.highway == ["residential"]
    assert ns.offset == 15  # 默认
    assert ns.roads is None


def test_run_requires_config():
    import sys

    # run 无 -c 时：默认 configs/config.yaml 存在则正常走，不存在则报错。
    # 这里在临时目录下跑（无 configs/config.yaml），应报错提示默认路径。
    import os

    with tempfile.TemporaryDirectory() as tmp:
        old = os.getcwd()
        os.chdir(tmp)
        try:
            cli.main(["run"])
        except SystemExit as e:
            assert "默认配置" in str(e) and "configs/config.yaml" in str(e)
        else:
            raise AssertionError("run 在无默认配置时应报错")
        finally:
            os.chdir(old)


def test_run_default_config():
    """run 无 -c 时默认读 configs/config.yaml。"""
    from pathlib import Path

    assert cli._DEFAULT_CONFIG == Path("configs/config.yaml")


def test_apply_config_reads_optional_params_from_yaml():
    """修复回归：可选参数（roads/pbf/area/points）在 SUPPRESS 下也能从 YAML 读。

    这类"默认为空"的参数若漏写 default=argparse.SUPPRESS，argparse 会默认赋 None，
    _apply_config 的 hasattr 判断会误判为"已显式传参"而跳过 YAML 覆盖。
    """
    import argparse

    def parse(argv, *names):
        p = argparse.ArgumentParser()
        for n in names:
            p.add_argument(n, default=argparse.SUPPRESS)
        return p.parse_args(argv)

    # sample：roads/area 从 YAML 读，pbf 缺省回退 None
    args = parse([], "--roads", "--pbf", "--area")
    cli._apply_config(args, {"sample": {"roads": "r.gpkg", "area": "a.gpkg"}}, "sample")
    assert args.roads == "r.gpkg"
    assert args.area == "a.gpkg"
    assert args.pbf is None

    # map：roads/area 从 YAML 读，points 缺省回退 None
    args = parse([], "--points", "--roads", "--area")
    cli._apply_config(args, {"map": {"roads": "r.gpkg", "area": "a.gpkg"}}, "map")
    assert args.roads == "r.gpkg"
    assert args.area == "a.gpkg"
    assert args.points is None


def test_download_c_reads_points_from_yaml():
    """修复回归：download -c 能从 YAML 读到 points（真实 cli.main 走 download 分支）。"""
    import os
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "samples.csv").write_text("name,lng,lat\np1,121.5,31.2\n", encoding="utf-8")
        yaml = base / "cfg.yaml"
        yaml.write_text(
            "base_dir: .\n"
            "download:\n"
            "  points: samples.csv\n"
            "  mode: panorama\n",
            encoding="utf-8",
        )
        captured = {}

        def fake_download(download_dir, points_csv=None, **kw):
            captured["points_csv"] = points_csv
            captured["mode"] = kw.get("mode")

        old = os.getcwd()
        os.chdir(tmp)
        try:
            with patch("baidu_pano.download.download_streetview_reverse", fake_download):
                rc = cli.main(["download", "-c", str(yaml)])
        finally:
            os.chdir(old)

        assert rc == 0
        assert captured["points_csv"] == Path("samples.csv")
        assert captured["mode"] == "panorama"


def test_points_crs_default_in_apply_config():
    """download 未指定 points_crs 时回填默认值 wgs84。"""
    import argparse

    args = argparse.Namespace()
    cli._apply_config(args, {}, "download")
    assert args.points_crs == "wgs84"


def test_points_crs_reads_from_yaml():
    """--points-crs 未显式传参时从 YAML 读取（SUPPRESS 机制）。"""
    import argparse

    def parse(argv, *names):
        p = argparse.ArgumentParser()
        for n in names:
            p.add_argument(n, default=argparse.SUPPRESS)
        return p.parse_args(argv)

    args = parse([], "--points-crs")
    cli._apply_config(args, {"download": {"points_crs": "bd09"}}, "download")
    assert args.points_crs == "bd09"


def test_convert_points_crs():
    """CSV 坐标归一化：wgs84 恒等、bd09/gcj02 转 WGS84、非法值报错。"""
    from baidu_pano.coords import wgs84_to_bd09, wgs84_to_gcj02
    from baidu_pano.download import _convert_points_crs

    # wgs84 恒等
    lngs, lats = _convert_points_crs([121.5], [31.2], "wgs84")
    assert (lngs[0], lats[0]) == (121.5, 31.2)

    # bd09 往返：wgs84 -> bd09 再转回，误差应 < 1e-4 度（约 10 米）
    blng, blat = wgs84_to_bd09(121.5, 31.2)
    rlng, rlat = _convert_points_crs([blng], [blat], "bd09")
    assert abs(rlng[0] - 121.5) < 1e-4
    assert abs(rlat[0] - 31.2) < 1e-4

    # gcj02 往返
    glng, glat = wgs84_to_gcj02(121.5, 31.2)
    rlng, rlat = _convert_points_crs([glng], [glat], "gcj02")
    assert abs(rlng[0] - 121.5) < 1e-4
    assert abs(rlat[0] - 31.2) < 1e-4

    # 非法坐标系应报错
    try:
        _convert_points_crs([121.5], [31.2], "xxx")
        raise AssertionError("未知坐标系应报 ValueError")
    except ValueError:
        pass


def test_map_points_crs_default():
    """map 未指定 points_crs 时回填默认值 wgs84。"""
    import argparse

    args = argparse.Namespace()
    cli._apply_config(args, {}, "map")
    assert args.points_crs == "wgs84"


def test_run_map_inherits_download_points_crs():
    """run 全流程：map 节未单独设 points_crs 时，沿用 download.points_crs。"""
    import os
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        yaml = base / "cfg.yaml"
        yaml.write_text(
            "base_dir: .\n"
            "download:\n"
            "  points: samples.csv\n"
            "  points_crs: bd09\n"
            "  mode: panorama\n",
            encoding="utf-8",
        )
        captured = {}

        def fake_download(cfg, args):
            captured["download_points_crs"] = args.points_crs

        def fake_map(cfg, args):
            captured["map_points_crs"] = args.points_crs

        old = os.getcwd()
        os.chdir(tmp)
        try:
            with patch("baidu_pano.cli.cmd_download", fake_download), \
                 patch("baidu_pano.cli.cmd_map", fake_map):
                rc = cli.main(["run", "-c", str(yaml)])
        finally:
            os.chdir(old)

        assert rc == 0
        assert captured["download_points_crs"] == "bd09"
        assert captured["map_points_crs"] == "bd09"


def test_map_points_crs_converts_csv():
    """map --points 指定 bd09 CSV 时，画点坐标应先转成 WGS84。"""
    import os
    from unittest.mock import patch

    from baidu_pano.coords import wgs84_to_bd09

    wlng, wlat = 121.4737, 31.2304
    blng, blat = wgs84_to_bd09(wlng, wlat)

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        csv = base / "pts.csv"
        csv.write_text(f"name,lng,lat\np1,{blng},{blat}\n", encoding="utf-8")
        captured = {}

        def fake_build_map(points, images_dir, report, out_html, tk, **kw):
            captured["lon"] = float(points.geometry.x.iloc[0])
            captured["lat"] = float(points.geometry.y.iloc[0])

        old = os.getcwd()
        os.chdir(tmp)
        try:
            with patch("baidu_pano.mapping.build_map", fake_build_map):
                rc = cli.main(["map", "--points", str(csv), "--points-crs", "bd09",
                               "--base", "."])
        finally:
            os.chdir(old)

        assert rc == 0
        assert abs(captured["lon"] - wlng) < 1e-4
        assert abs(captured["lat"] - wlat) < 1e-4


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("test_config 全部通过")
