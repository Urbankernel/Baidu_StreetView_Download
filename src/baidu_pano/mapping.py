"""网页互动地图生成：点级展示街景照片（参考 sagai_cn 的 map 命令）。

双击生成的 HTML 即可在浏览器打开：
  - 有街景照片的采样点：彩色圆点，点击弹出该点照片（四方向 4 张 / 全景 1 张），
    再点照片放大查看原图；
  - 无街景照片的采样点：灰色圆点，点击提示状态（无街景 / 下载失败 / 未下载）。

底图纪律（沿用 sagai_cn）：
  - 默认天地图（CGCS2000，有审图号，可直接叠加 WGS84 采样点）；
  - 无 TIANDITU_TK 时退回无瓦片模式并明确提示，不悄悄用 GCJ-02/BD-09 偏移底图。
"""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import folium
import geopandas as gpd
import pandas as pd
from branca.element import Element, MacroElement
from jinja2 import Template
from PIL import Image

# 四视角后缀（与下载产物契约一致）
VIEW_SUFFIXES = ("left", "right", "front", "back")
VIEW_LABELS = {"left": "左", "right": "右", "front": "前", "back": "后"}

TIANDITU_VEC = "https://t{s}.tianditu.gov.cn/DataServer?T=vec_w&x={x}&y={y}&l={z}&tk={tk}"
TIANDITU_CVA = "https://t{s}.tianditu.gov.cn/DataServer?T=cva_w&x={x}&y={y}&l={z}&tk={tk}"
TIANDITU_IMG = "https://t{s}.tianditu.gov.cn/DataServer?T=img_w&x={x}&y={y}&l={z}&tk={tk}"
TIANDITU_CIA = "https://t{s}.tianditu.gov.cn/DataServer?T=cia_w&x={x}&y={y}&l={z}&tk={tk}"

# 有图 / 无图 点样式（蓝=有街景，灰=无街景）
_HAS_COLOR = "#1e88e5"
_NO_COLOR = "#9e9e9e"

# 路网 / 研究范围配色（图例与绘制共用，避免色值漂移）
_ROAD_CASING = "#212121"   # 路网底层描边（深色，保证与任意底图分离）
_ROAD_MAIN = "#e91e63"     # 路网顶层主线（品红，避开蓝点/橙面）
_AREA_EDGE = "#e65100"     # 研究范围描边（橙）
_AREA_FILL = "#ff9800"     # 研究范围填充（橙）


def _thumb_b64(path: Path, max_w: int = 320, quality: int = 78) -> str:
    """读图 → 等比缩到 max_w 宽 → JPEG base64。读失败返回空串。"""
    try:
        img = Image.open(path).convert("RGB")
        w, h = img.size
        if w > max_w:
            img = img.resize((max_w, max(1, round(h * max_w / w))), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def _base_map(center_lat: float, center_lon: float, tk: str,
              imagery: bool = False, zoom: int = 14) -> folium.Map:
    """天地图底图（矢量/影像 + 注记），无 tk 时退回无瓦片。

    必须显式用 folium.TileLayer 并传 subdomains="01234567"（天地图子域 t0~t7），
    不要用 folium.Map(tiles=URL)，否则 {s} 会回落到 "abc" 导致瓦片 404。
    """
    m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom, tiles=None)
    if not tk:
        print("[warn] 未配置 TIANDITU_TK，地图无底图瓦片（只显示采样点，不会退回偏移底图）")
        return m

    def _url(tpl: str) -> str:
        return tpl.format(s="{s}", x="{x}", y="{y}", z="{z}", tk=tk)

    common = dict(subdomains="01234567", max_zoom=18, max_native_zoom=18)
    folium.TileLayer(_url(TIANDITU_VEC), name="天地图矢量", attr="天地图",
                     show=not imagery, **common).add_to(m)
    folium.TileLayer(_url(TIANDITU_IMG), name="天地图影像", attr="天地图",
                     show=imagery, **common).add_to(m)
    folium.TileLayer(_url(TIANDITU_CVA), name="矢量注记", attr="天地图注记",
                     overlay=True, show=not imagery, **common).add_to(m)
    folium.TileLayer(_url(TIANDITU_CIA), name="影像注记", attr="天地图注记",
                     overlay=True, show=imagery, **common).add_to(m)
    return m


def _detect_images(images_dir: Path, pid: str) -> tuple[list[tuple[str, Path]], bool]:
    """检测采样点的照片文件。返回 ([(label, path), ...], is_panorama)。

    - 存在 {pid}_*_pano.jpg（带路名）或 {pid}_pano.jpg（无路名）→ 全景模式
    - 存在 {pid}_*_{view}.jpg 或 {pid}_{view}.jpg → 四方向（按 left/right/front/back 顺序）
    - 都无 → 空列表
    """
    pano = None
    cand = list(images_dir.glob(f"{pid}_*_pano.jpg"))
    legacy = images_dir / f"{pid}_pano.jpg"
    if cand:
        pano = cand[0]
    elif legacy.exists():
        pano = legacy
    if pano:
        return [("全景", pano)], True

    views = []
    for suffix in VIEW_SUFFIXES:
        cand = list(images_dir.glob(f"{pid}_*_{suffix}.jpg"))
        legacy = images_dir / f"{pid}_{suffix}.jpg"
        p = cand[0] if cand else (legacy if legacy.exists() else None)
        if p:
            views.append((VIEW_LABELS[suffix], p))
    return views, False


def _popup_html(pid: str, status: str, imgs: list[tuple[str, Path]],
                rel_fn, road_name: str = "") -> str:
    """构造点弹窗：标题 + 路名（如有） + 状态 + 缩略图网格（点击放大）。

    rel_fn(path)：把原图绝对路径转为相对 map.html 的相对路径（供 lightbox 放大）。
    """
    road_html = (
        f'<div style="font-size:13px;color:#333;margin:0 0 2px;">{road_name}</div>'
        if road_name else "")

    if not imgs:
        return (f'<div style="font-family:Arial;min-width:200px;">'
                f'<h4 style="margin:0 0 4px;">{pid}</h4>'
                f'{road_html}'
                f'<span style="color:#888;">{status}</span></div>')

    cells = []
    for label, path in imgs:
        b64 = _thumb_b64(path)
        rel = rel_fn(path)
        if b64:
            cells.append(
                f'<div style="display:inline-block;text-align:center;margin:3px;">'
                f'<img src="data:image/jpeg;base64,{b64}" '
                f'style="width:150px;height:100px;object-fit:cover;border-radius:3px;'
                f'cursor:zoom-in;" alt="{label}" '
                f'onclick="svOpen(\'{rel}\', this.src)" />'
                f'<div style="font-size:11px;color:#555;">{label}</div></div>')
        else:
            cells.append(
                f'<div style="display:inline-block;text-align:center;margin:3px;">'
                f'<div style="width:150px;height:100px;line-height:100px;'
                f'background:#eee;color:#999;border-radius:3px;">无图</div>'
                f'<div style="font-size:11px;color:#555;">{label}</div></div>')

    grid = ("<div style='display:grid;grid-template-columns:repeat(2,auto);"
            "gap:2px;justify-content:center;'>" + "".join(cells) + "</div>")
    return (f'<div style="font-family:Arial;min-width:200px;text-align:center;">'
            f'<h4 style="margin:0 0 4px;">{pid}</h4>'
            f'{road_html}'
            f'<span style="font-size:11px;color:#888;">{status}</span>'
            f"{grid}"
            f'<div style="font-size:11px;color:#999;margin-top:4px;">'
            f"点击图片放大</div></div>")


# lightbox 全屏放大（点击缩略图 → 显示原图相对路径，失败回退缩略图 base64）
_LIGHTBOX = (
    "<style>"
    "#svlb{position:fixed;inset:0;background:rgba(0,0,0,0.88);z-index:10000;"
    "display:none;align-items:center;justify-content:center;cursor:zoom-out;}"
    "#svlb img{max-width:94vw;max-height:94vh;object-fit:contain;"
    "box-shadow:0 4px 28px rgba(0,0,0,.7);}"
    "</style>"
    '<div id="svlb" onclick="this.style.display=\'none\'">'
    '<img id="svlbimg" src="" alt=""></div>'
    "<script>"
    "function svOpen(src,thumb){var im=document.getElementById('svlbimg');"
    "im.onerror=function(){if(im.src!==thumb){im.src=thumb;}else{im.onerror=null;}};"
    "im.src=src;document.getElementById('svlb').style.display='flex';}"
    "</script>"
)

_LEGEND = (
    '<div id="svlegend" style="position:fixed;bottom:28px;left:12px;z-index:9999;'
    'background:rgba(255,255,255,0.93);padding:10px 12px;border-radius:6px;'
    'font-family:Arial;font-size:12px;color:#333;box-shadow:0 1px 6px rgba(0,0,0,.35);">'
    '<div style="font-weight:bold;margin-bottom:4px;">采样点</div>'
    f'<div style="display:flex;align-items:center;gap:6px;">'
    f'<span style="width:10px;height:10px;background:{_HAS_COLOR};'
    f'border-radius:50%;display:inline-block;"></span>有街景照片</div>'
    f'<div style="display:flex;align-items:center;gap:6px;margin-top:3px;">'
    f'<span style="width:10px;height:10px;background:{_NO_COLOR};'
    f'border-radius:50%;display:inline-block;"></span>无街景照片</div>'
    '<div style="font-weight:bold;margin:8px 0 4px;">图层</div>'
    '<div style="display:flex;align-items:center;gap:6px;">'
    f'<span style="width:14px;height:3px;background:{_ROAD_MAIN};'
    f'display:inline-block;"></span>路网</div>'
    '<div style="display:flex;align-items:center;gap:6px;margin-top:3px;">'
    f'<span style="width:14px;height:10px;background:rgba(255,152,0,0.12);'
    f'border:1.5px dashed {_AREA_EDGE};display:inline-block;"></span>研究范围</div>'
    "</div>"
)


def _to_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """统一转 WGS84（无 CRS 或已是 4326 则原样返回）。"""
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        return gdf.to_crs("EPSG:4326")
    return gdf


class _PaneScript(MacroElement):
    """在地图创建脚本之后、成果图层脚本之前创建自定义 pane。

    必须用 MacroElement（jinja script 宏）注入——直接挂 Element 不会被渲染，
    且注入点必须在带 pane 的图层之前，否则图层初始化时 pane 不存在会报错。
    """

    def __init__(self, map_name: str):
        super().__init__()
        self._template = Template(
            "{% macro script(this, kwargs) %}\n"
            f"{map_name}.createPane('sv-area').style.zIndex = 350;\n"
            f"{map_name}.createPane('sv-road').style.zIndex = 380;\n"
            "{% endmacro %}"
        )


def build_map(points: gpd.GeoDataFrame, images_dir: Path,
              report: pd.DataFrame | None, out_html: Path, tk: str,
              imagery: bool = False,
              streets: gpd.GeoDataFrame | None = None,
              area: gpd.GeoDataFrame | None = None) -> folium.Map:
    """生成互动地图并落盘。points 需含 point_id 与 geometry（EPSG:4326）。

    report：download_report.csv（可选），image_name/status 用于无图点的状态说明。
    streets / area：可选的路网线、研究范围面图层，画进地图并在图层控件中可勾选。
    """
    points = _to_wgs84(points)

    status_map: dict[str, str] = {}
    road_map: dict[str, str] = {}
    if report is not None and len(report):
        for _, r in report.iterrows():
            st = r.get("status", "")
            label = {"ok": "", "no_pano": "无街景", "download_failed": "下载失败"}.get(st, st)
            status_map[str(r["image_name"])] = label
            if "road_name" in report.columns:
                rn = r.get("road_name", "")
                if pd.notna(rn) and str(rn).strip():
                    road_map[str(r["image_name"])] = str(rn)

    m = _base_map(float(points.geometry.y.mean()),
                  float(points.geometry.x.mean()), tk, imagery=imagery)

    # 自定义 pane：固定图层叠放顺序。Leaflet 把所有 overlay 放进同一个 pane
    # （z=400），同 pane 内按"加入顺序"叠放——勾选顺序会打乱层序（取消再勾选
    # 路网，它会跑到采样点上面）。给研究范围/路网独立 pane 后，z-index 固定，
    # 与勾选顺序无关：底图(200) < 研究范围(350) < 路网(380) < 采样点(默认 400)。
    # 注意：此宏必须在这两个图层加入之前 add_child，保证 pane 先创建。
    m.add_child(_PaneScript(m.get_name()))

    # 研究范围（面）：最底层，透明填充 + 虚线描边，可勾选
    if area is not None and len(area):
        folium.GeoJson(
            _to_wgs84(area).to_json(),
            name="研究范围",
            pane="sv-area",
            style_function=lambda x: {
                "color": _AREA_EDGE, "weight": 2, "opacity": 0.9,
                "dashArray": "6,4",
                "fillColor": _AREA_FILL, "fillOpacity": 0.06},
        ).add_to(m)

    # 路网（线）：双描边 casing（深色描边 + 品红主线），包进 FeatureGroup 保持图层控件单开关
    if streets is not None and len(streets):
        streets_wgs = _to_wgs84(streets)
        fg = folium.FeatureGroup(name="路网")
        # 底层描边：深色更宽，提供与浅色/深色底图的分离度
        folium.GeoJson(
            streets_wgs.to_json(),
            pane="sv-road",
            style_function=lambda x: {"color": _ROAD_CASING, "weight": 6, "opacity": 1.0},
        ).add_to(fg)
        # 顶层主线：品红高亮（避开蓝采样点、橙研究范围），悬停显示道路标签
        # 标签字段按优先级自动探测：路名 → 道路等级 → 兜底"道路 N"（保证悬停总有反馈）
        label_col = alias = None
        for cand, a in (("name", "路名"), ("name:zh", "路名"), ("NAME", "路名"),
                        ("road_name", "路名"), ("路名", "路名")):
            if cand in streets_wgs.columns:
                label_col, alias = cand, a
                break
        if label_col is None and "highway" in streets_wgs.columns:
            label_col, alias = "highway", "道路等级"
        if label_col is None:
            streets_wgs = streets_wgs.copy()
            streets_wgs["_rid"] = [f"道路 {i + 1}" for i in range(len(streets_wgs))]
            label_col, alias = "_rid", "道路"
        main_kwargs: dict = {}
        main_kwargs["tooltip"] = folium.GeoJsonTooltip(
            fields=[label_col], aliases=[alias], localize=True)
        folium.GeoJson(
            streets_wgs.to_json(),
            pane="sv-road",
            style_function=lambda x: {"color": _ROAD_MAIN, "weight": 3.5, "opacity": 1.0},
            **main_kwargs,
        ).add_to(fg)
        fg.add_to(m)

    # 原图相对路径以 map.html 所在目录为基准（双击 file:// 打开时能加载同目录树图片）
    import os

    def rel_fn(p: Path) -> str:
        try:
            return os.path.relpath(p, out_html.parent).replace(os.sep, "/")
        except ValueError:
            return p.as_posix()

    has_n = no_n = 0
    for _, row in points.iterrows():
        pid = str(row["point_id"])
        lat, lon = float(row.geometry.y), float(row.geometry.x)
        imgs, _ = _detect_images(images_dir, pid)
        has_img = len(imgs) > 0

        if has_img:
            status = "已下载"
            color = _HAS_COLOR
            has_n += 1
        else:
            status = status_map.get(pid, "未下载")
            color = _NO_COLOR
            no_n += 1

        popup = _popup_html(pid, status, imgs, rel_fn, road_map.get(pid, ""))
        folium.CircleMarker(
            [lat, lon],
            radius=6 if has_img else 4,
            color="#ffffff", weight=1.5,
            fillColor=color, fillOpacity=0.95,
            popup=folium.Popup(popup, max_width=420),
            tooltip=f"{pid}",
        ).add_to(m)

    m.get_root().html.add_child(Element(_LIGHTBOX))
    m.get_root().html.add_child(Element(_LEGEND))
    # LayerControl 必须最后 add，否则切换面板与成果图层会丢（folium 的坑）
    folium.LayerControl(collapsed=False).add_to(m)

    m.save(str(out_html))
    print(f"地图已生成：{out_html}")
    print(f"采样点 {len(points)} 个：有街景照片 {has_n} 个，无街景照片 {no_n} 个")
    if not tk:
        print("[warn] 当前无底图瓦片（未配置 TIANDITU_TK），点位仍可交互查看")
    return m
