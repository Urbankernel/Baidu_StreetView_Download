"""街道走向（bearing）与视角方位角计算。

与上游 SAGAI Block 2 一致：canonical bearing 归一化到 [0,180)，
保证 left/right 与 OSM/矢量路网的数字化方向无关。
"""
from __future__ import annotations

import math

from shapely.ops import substring

# 四视角后缀（契约：{point_id}_{view}.jpg）
VIEW_SUFFIXES = ("left", "right", "front", "back")


def canonical_bearing(raw_bearing: float) -> float:
    """归一化到 [0,180)，保证 left/right 与路网数字化方向无关。"""
    b = raw_bearing % 360
    return b - 180 if b >= 180 else b


def subdivide_segment(line_geom, max_length: float = 50.0) -> list:
    """把线段几何切成 ≤max_length 的 LineString 子段列表。

    本地矢量路网常含 MultiLineString（CAD/DWG 转换、GIS 合并产生），
    而 shapely.ops.substring 只接受 LineString，故先展平为单部件再细分。
    """
    parts = (list(line_geom.geoms)
             if line_geom.geom_type == "MultiLineString" else [line_geom])
    subs = []
    for part in parts:
        total = part.length
        if total <= max_length:
            subs.append(part)
            continue
        n_parts = math.ceil(total / max_length)
        part_length = total / n_parts
        for i in range(n_parts):
            sub = substring(part, i * part_length, min((i + 1) * part_length, total))
            if sub is not None and not sub.is_empty and sub.length > 0:
                subs.append(sub)
    return subs or [line_geom]


def _node_to_node_bearing(line_geom) -> float:
    """取线段首尾节点算方位角（0=正北，顺时针）。"""
    coords = list(line_geom.coords)
    if len(coords) < 2:
        return 0.0
    dx = coords[-1][0] - coords[0][0]
    dy = coords[-1][1] - coords[0][1]
    return math.degrees(math.atan2(dx, dy)) % 360


def compute_bearing_for_point(point_geom, street_geom, max_sub_length: float = 50.0) -> float:
    """采样点在街道上的 canonical bearing：取离点最近的子段走向。"""
    sub_segs = subdivide_segment(street_geom, max_sub_length)
    best_seg = min(sub_segs, key=lambda s: s.distance(point_geom))
    return canonical_bearing(_node_to_node_bearing(best_seg))


def get_headings_for_mode(mode: str, bearing: float = 0.0) -> list[tuple[float, str]]:
    """bearing 为 canonical [0,180)。left/right 为垂直街道方向，front/back 沿街道。

    mode ∈ {"left_right", "front_back", "4_views"}；其余视为 4_views。
    """
    front = bearing % 360
    back = (bearing + 180) % 360
    left = (bearing + 270) % 360
    right = (bearing + 90) % 360
    if mode == "left_right":
        return [(left, "left"), (right, "right")]
    if mode == "front_back":
        return [(front, "front"), (back, "back")]
    return [(left, "left"), (right, "right"), (front, "front"), (back, "back")]
