"""ICL module — face/edge picking + distance measurement on STEP parts.

Separate from cad/geometry (label wireframe) and cad/preview (plain STL).
Face and edge IDs are 1-based positions in OCC indexed maps, deterministic for
the same shape, so the frontend can pick an entity and the measure endpoint can
resolve it back to a sub-shape.
"""
from __future__ import annotations

import math
from typing import Optional

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GeomAbs import (
    GeomAbs_Circle,
    GeomAbs_Cylinder,
    GeomAbs_Line,
    GeomAbs_Plane,
)
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_REVERSED
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape

from cad.loader import load_step

# ---------------------------------------------------------------------------
# Indexed maps (deterministic 1-based IDs)
# ---------------------------------------------------------------------------


def _face_map(shape) -> TopTools_IndexedMapOfShape:
    from OCP.TopAbs import TopAbs_FACE

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, m)
    return m


def _edge_map(shape) -> TopTools_IndexedMapOfShape:
    from OCP.TopAbs import TopAbs_EDGE

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, m)
    return m


def _centroid(face) -> tuple[float, float, float]:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    c = props.CentreOfMass()
    return (c.X(), c.Y(), c.Z())


def _face_meta(face, idx: int) -> dict:
    surf = BRepAdaptor_Surface(face)
    t = surf.GetType()
    meta: dict = {"id": idx, "type": "other"}
    try:
        cx, cy, cz = _centroid(face)
        meta["centroid"] = [round(cx, 3), round(cy, 3), round(cz, 3)]
    except Exception:
        pass
    if t == GeomAbs_Plane:
        meta["type"] = "plane"
        ax = surf.Plane().Axis().Direction()
        n = [ax.X(), ax.Y(), ax.Z()]
        if face.Orientation() == TopAbs_REVERSED:
            n = [-v for v in n]
        meta["normal"] = [round(v, 5) for v in n]
    elif t == GeomAbs_Cylinder:
        meta["type"] = "cylinder"
        cyl = surf.Cylinder()
        meta["radius"] = round(cyl.Radius(), 3)
        sweep = math.degrees(surf.LastUParameter() - surf.FirstUParameter())
        meta["sweep_deg"] = round(sweep, 1)
        ax = cyl.Axis().Direction()
        meta["axis"] = [round(ax.X(), 5), round(ax.Y(), 5), round(ax.Z(), 5)]
    return meta


# ---------------------------------------------------------------------------
# Face-tagged mesh
# ---------------------------------------------------------------------------


def faced_mesh(shape) -> dict:
    """Tessellate the shape, returning vertices + triangles tagged by face id.

    positions: flat [x,y,z, ...] float list
    indices:   flat triangle vertex indices into positions
    tri_face:  face id (1-based) for each triangle (len == indices/3)
    faces:     per-face metadata (type/normal/axis/centroid/radius/sweep)
    """
    BRepMesh_IncrementalMesh(shape, 0.3, False, 0.5, True).Perform()
    fmap = _face_map(shape)
    positions: list[float] = []
    indices: list[int] = []
    tri_face: list[int] = []
    faces: list[dict] = []
    vbase = 0
    for i in range(1, fmap.Extent() + 1):
        face = TopoDS.Face_s(fmap.FindKey(i))
        faces.append(_face_meta(face, i))
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            continue
        trsf = loc.Transformation()
        n = tri.NbNodes()
        for k in range(1, n + 1):
            p = tri.Node(k).Transformed(trsf)
            positions.extend((round(p.X(), 3), round(p.Y(), 3), round(p.Z(), 3)))
        reversed_ = face.Orientation() == TopAbs_REVERSED
        for t in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(t).Get()
            if reversed_:
                b, c = c, b
            indices.extend((vbase + a - 1, vbase + b - 1, vbase + c - 1))
            tri_face.append(i)
        vbase += n
    return {
        "positions": positions,
        "indices": indices,
        "tri_face": tri_face,
        "faces": faces,
        "face_count": fmap.Extent(),
    }


# ---------------------------------------------------------------------------
# Indexed edges
# ---------------------------------------------------------------------------

_EDGE_TYPE = {GeomAbs_Line: "line", GeomAbs_Circle: "circle"}


def indexed_edges(shape) -> list:
    """Return edges as 3D polylines with a stable id and curve type."""
    from OCP.GCPnts import GCPnts_TangentialDeflection

    from cad.loader import as_edge, finite

    emap = _edge_map(shape)
    out = []
    for i in range(1, emap.Extent() + 1):
        sub = emap.FindKey(i)
        if sub.IsNull():
            continue
        try:
            edge = as_edge(sub)
            if BRep_Tool.Degenerated_s(edge):
                continue
            curve = BRepAdaptor_Curve(edge)
            first, last = curve.FirstParameter(), curve.LastParameter()
            if not (finite(first) and finite(last) and last > first):
                continue
            disc = GCPnts_TangentialDeflection()
            disc.Initialize(curve, 0.3, 0.04)
            if disc.NbPoints() < 2:
                continue
            pts = []
            for j in range(1, disc.NbPoints() + 1):
                p = disc.Value(j)
                if finite(p.X()) and finite(p.Y()) and finite(p.Z()):
                    pts.append([round(p.X(), 3), round(p.Y(), 3), round(p.Z(), 3)])
            if len(pts) < 2:
                continue
            out.append(
                {
                    "id": i,
                    "type": _EDGE_TYPE.get(curve.GetType(), "curve"),
                    "points": pts,
                }
            )
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Measure
# ---------------------------------------------------------------------------


def _sub_shape(shape, kind: str, ent_id: int):
    m = _face_map(shape) if kind == "face" else _edge_map(shape)
    if ent_id < 1 or ent_id > m.Extent():
        raise ValueError(f"{kind} id {ent_id} out of range (1..{m.Extent()})")
    return m.FindKey(ent_id)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def measure(
    shape,
    kind1: str,
    id1: int,
    kind2: str,
    id2: int,
) -> dict:
    """Distance between two sub-shapes.

    Default = BRepExtrema minimum distance + the two closest points.
    Special case: two parallel planar faces -> perpendicular gap (the
    QA-intended dimension), drawn centroid-to-projection.
    """
    s1 = _sub_shape(shape, kind1, id1)
    s2 = _sub_shape(shape, kind2, id2)

    # Parallel-plane perpendicular distance
    if kind1 == "face" and kind2 == "face":
        f1, f2 = TopoDS.Face_s(s1), TopoDS.Face_s(s2)
        a1, a2 = BRepAdaptor_Surface(f1), BRepAdaptor_Surface(f2)
        if a1.GetType() == GeomAbs_Plane and a2.GetType() == GeomAbs_Plane:
            pln1 = a1.Plane()
            d1 = pln1.Axis().Direction()
            d2 = a2.Plane().Axis().Direction()
            n1 = (d1.X(), d1.Y(), d1.Z())
            n2 = (d2.X(), d2.Y(), d2.Z())
            if abs(_dot(n1, n2)) > 0.999:
                from OCP.gp import gp_Pnt

                c2 = _centroid(f2)
                gap = pln1.Distance(gp_Pnt(*c2))
                # project c2 onto plane1 along n1
                loc = pln1.Location()
                p0 = (loc.X(), loc.Y(), loc.Z())
                signed = _dot((c2[0] - p0[0], c2[1] - p0[1], c2[2] - p0[2]), n1)
                p1 = [c2[i] - n1[i] * signed for i in range(3)]
                return {
                    "value_mm": round(gap, 3),
                    "p1": [round(v, 3) for v in p1],
                    "p2": [round(v, 3) for v in c2],
                    "mode": "surface-to-surface",
                    "method": "parallel-plane",
                    "suggested_gauge": "Vernier",
                }

    dss = BRepExtrema_DistShapeShape(s1, s2)
    if not dss.IsDone():
        raise ValueError("distance computation failed")
    val = dss.Value()
    pa = dss.PointOnShape1(1)
    pb = dss.PointOnShape2(1)
    mode = "surface-to-surface" if kind1 == kind2 == "face" else (
        "edge-to-edge" if kind1 == kind2 == "edge" else "surface-to-edge"
    )
    return {
        "value_mm": round(val, 3),
        "p1": [round(pa.X(), 3), round(pa.Y(), 3), round(pa.Z(), 3)],
        "p2": [round(pb.X(), 3), round(pb.Y(), 3), round(pb.Z(), 3)],
        "mode": mode,
        "method": "min-distance",
        "suggested_gauge": "Vernier",
    }


def measure_from_file(path: str, kind1, id1, kind2, id2) -> dict:
    return measure(load_step(path), kind1, id1, kind2, id2)


# ---------------------------------------------------------------------------
# Auto-suggest checks
# ---------------------------------------------------------------------------


def _bend_angles(shape) -> list:
    """Bend angle = sweep of partial-cylinder bend faces (holes sweep ~360).

    Dedupe near-equal angles (inner/outer faces of one bend share an angle).
    """
    fmap = _face_map(shape)
    angles: list[float] = []
    for i in range(1, fmap.Extent() + 1):
        surf = BRepAdaptor_Surface(TopoDS.Face_s(fmap.FindKey(i)))
        if surf.GetType() != GeomAbs_Cylinder:
            continue
        sweep = math.degrees(surf.LastUParameter() - surf.FirstUParameter())
        if sweep >= 350 or sweep < 5:
            continue  # full hole or sliver
        angles.append(round(sweep))
    # merge within 2 deg
    uniq: list[int] = []
    for a in sorted(angles):
        if not any(abs(a - u) <= 2 for u in uniq):
            uniq.append(a)
    return uniq


def suggest(path: str) -> dict:
    """Candidate inspection checks from auto-extracted geometry."""
    from analysis.part_analyser import analyse_part

    shape = load_step(path)
    info = analyse_part(path)
    checks: list[dict] = []

    bbox = info.get("bbox_mm") or []
    for label, val in zip(("Length", "Width", "Height"), bbox):
        checks.append(
            {
                "desc": f"Check overall {label.lower()} {round(val, 1)}mm",
                "value_mm": round(val, 2),
                "tol": "+/- 0.5mm",
                "gauge": "Vernier",
                "source": "bbox",
            }
        )

    for d in info.get("holes_mm") or []:
        checks.append(
            {
                "desc": f"Check hole dia {round(d, 1)}mm",
                "value_mm": round(d, 2),
                "tol": "+/- 0.1mm",
                "gauge": "Vernier",
                "source": "hole",
            }
        )

    for ang in _bend_angles(shape):
        checks.append(
            {
                "desc": f"Ensure bend at {ang} deg",
                "value_mm": ang,
                "tol": "+/- 1 deg",
                "gauge": "Protractor",
                "source": "bend",
            }
        )

    return {"checks": checks, "part": info.get("components", [{}])[0]}
