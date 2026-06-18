import math
import re


def _build_maps(text):
    pt, direction, axis = {}, {}, {}
    for m in re.finditer(
        r"#(\d+)=CARTESIAN_POINT\('[^']*',\(([\d.E+\-]+),([\d.E+\-]+),([\d.E+\-]+)\)\)",
        text,
    ):
        pt[m.group(1)] = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
    for m in re.finditer(
        r"#(\d+)=DIRECTION\('[^']*',\(([\d.E+\-]+),([\d.E+\-]+),([\d.E+\-]+)\)\)",
        text,
    ):
        direction[m.group(1)] = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
    for m in re.finditer(r"#(\d+)=AXIS2_PLACEMENT_3D\('[^']*',#(\d+),#(\d+),#(\d+)\)", text):
        axis[m.group(1)] = (m.group(2), m.group(3), m.group(4))
    return pt, direction, axis


def analyse_step_features_text(text: str) -> dict:
    raw_pts = re.findall(r"CARTESIAN_POINT\('[^']*',\(([\d.E+\-]+),", text)
    scale = 1.0
    for value in raw_pts:
        val = abs(float(value))
        if 0 < val < 0.5:
            scale = 1000.0
            break
        if val > 1.0:
            break

    pt, direction, axis = _build_maps(text)

    cylinders = []
    for m in re.finditer(r"#(\d+)=CYLINDRICAL_SURFACE\('[^']*',#(\d+),([\d.E+\-]+)\)", text):
        radius = float(m.group(3)) * scale
        ax_id = m.group(2)
        loc = (0.0, 0.0, 0.0)
        if ax_id in axis:
            loc_id = axis[ax_id][0]
            if loc_id in pt:
                loc = tuple(v * scale for v in pt[loc_id])
        cylinders.append({"r": radius, "loc": loc})

    planes = []
    for m in re.finditer(r"#(\d+)=PLANE\('[^']*',#(\d+)\)", text):
        ax_id = m.group(2)
        if ax_id not in axis:
            continue
        loc_id, dir_id, _ = axis[ax_id]
        if loc_id not in pt or dir_id not in direction:
            continue
        loc = tuple(v * scale for v in pt[loc_id])
        n = direction[dir_id]
        d = loc[0] * n[0] + loc[1] * n[1] + loc[2] * n[2]
        planes.append({"n": n, "d": d})

    votes: dict[float, int] = {}
    for i in range(len(planes)):
        for j in range(i + 1, len(planes)):
            n1, n2 = planes[i]["n"], planes[j]["n"]
            dot = n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2]
            if abs(abs(dot) - 1.0) < 1e-3:
                d2 = planes[j]["d"] if dot > 0 else -planes[j]["d"]
                dist = round(abs(planes[i]["d"] - d2), 1)
                if 0.1 < dist < 40.0:
                    votes[dist] = votes.get(dist, 0) + 1

    if votes:
        max_vote = max(votes.values())
        thickness_mm = min(d for d, vote in votes.items() if vote >= max_vote * 0.5)
    else:
        thickness_mm = 0.0

    def loc_key(loc):
        return (round(loc[0] / 2) * 2, round(loc[1] / 2) * 2, round(loc[2] / 2) * 2)

    groups: dict[tuple, list[float]] = {}
    for cylinder in cylinders:
        groups.setdefault(loc_key(cylinder["loc"]), []).append(cylinder["r"])

    bend_inner_radii, unpaired = [], []
    for radii in groups.values():
        rs = sorted(radii)
        if len(rs) >= 2 and thickness_mm > 0 and abs((rs[-1] - rs[0]) - thickness_mm) < thickness_mm * 0.3:
            bend_inner_radii.append(round(rs[0], 2))
        else:
            unpaired.append(round(max(rs), 2))

    min_bend_r = min(bend_inner_radii) if bend_inner_radii else (thickness_mm * 2)
    hole_diameters = [round(r * 2, 2) for r in unpaired if r > min_bend_r * 2.0]

    all_pts = list(pt.values())
    if all_pts:
        xs = [p[0] * scale for p in all_pts]
        ys = [p[1] * scale for p in all_pts]
        zs = [p[2] * scale for p in all_pts]
        dims = sorted([max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)], reverse=True)
        bbox_mm = [round(dim, 1) for dim in dims]
    else:
        bbox_mm = [0.0, 0.0, round(thickness_mm, 1)]

    return {
        "thickness_mm": round(thickness_mm, 1),
        "bend_count": len(bend_inner_radii),
        "hole_count": len(hole_diameters),
        "holes_mm": sorted(hole_diameters),
        "bend_radii_mm": sorted(bend_inner_radii, reverse=True),
        "bbox_mm": bbox_mm,
        "flat_area_mm2": 0.0,
        "flat_pattern_area_mm2": 0.0,
        "cut_perimeter_mm": 0.0,
        "flat_blank_w_mm": 0.0,
        "flat_blank_h_mm": 0.0,
    }


def analyse_step_features(path: str) -> dict:
    with open(path, "r", errors="replace") as fh:
        text = fh.read()
    return analyse_step_features_text(text)


def classify_component(features: dict) -> str:
    thickness = features.get("thickness_mm", 0.0)
    bbox = features.get("bbox_mm", [0.0, 0.0, 0.0])
    largest = max(bbox) if bbox else 0.0
    if thickness > 0 and largest / thickness > 5:
        return "sheet_metal"
    return "other"

