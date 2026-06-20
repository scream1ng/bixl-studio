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
# OCC binding compatibility
# Newer OCP exposes static methods with a `_s` suffix (Face_s, MapShapes_s);
# older builds (e.g. on Railway) expose them without it. Resolve per-binding.
# ---------------------------------------------------------------------------


def _static(cls, name):
    fn = getattr(cls, name + "_s", None)
    return fn if fn is not None else getattr(cls, name)


def _face(shape):
    return _static(TopoDS, "Face")(shape)


def _map_shapes(shape, typ, m):
    _static(TopExp, "MapShapes")(shape, typ, m)


def _triangulation(face, loc):
    return _static(BRep_Tool, "Triangulation")(face, loc)


def _degenerated(edge):
    return _static(BRep_Tool, "Degenerated")(edge)


def _surface_props(face, props):
    _static(BRepGProp, "SurfaceProperties")(face, props)


# ---------------------------------------------------------------------------
# Indexed maps (deterministic 1-based IDs)
# ---------------------------------------------------------------------------


def _face_map(shape) -> TopTools_IndexedMapOfShape:
    from OCP.TopAbs import TopAbs_FACE

    m = TopTools_IndexedMapOfShape()
    _map_shapes(shape, TopAbs_FACE, m)
    return m


def _edge_map(shape) -> TopTools_IndexedMapOfShape:
    from OCP.TopAbs import TopAbs_EDGE

    m = TopTools_IndexedMapOfShape()
    _map_shapes(shape, TopAbs_EDGE, m)
    return m


def _centroid(face) -> tuple[float, float, float]:
    props = GProp_GProps()
    _surface_props(face, props)
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


def _tri_node(tri, k):
    """Triangulation node access across OCC versions (Node(i) vs Nodes().Value(i))."""
    try:
        return tri.Node(k)
    except (AttributeError, TypeError):
        return tri.Nodes().Value(k)


def _tri_indices(triangle):
    """Triangle vertex indices across OCC versions."""
    g = triangle.Get()
    if g is not None:
        return g
    return (triangle.Value(1), triangle.Value(2), triangle.Value(3))


def faced_mesh(shape) -> dict:
    """Tessellate the shape, returning vertices + triangles tagged by face id.

    positions: flat [x,y,z, ...] float list
    indices:   flat triangle vertex indices into positions
    tri_face:  face id (1-based) for each triangle (len == indices/3)
    faces:     per-face metadata (type/normal/axis/centroid/radius/sweep)
    """
    # 4-arg form matches the proven cad.preview meshing — the 5-arg
    # (parallel) overload is missing on some OCC builds.
    BRepMesh_IncrementalMesh(shape, 0.3, False, 0.5).Perform()
    fmap = _face_map(shape)
    positions: list[float] = []
    indices: list[int] = []
    tri_face: list[int] = []
    faces: list[dict] = []
    vbase = 0
    for i in range(1, fmap.Extent() + 1):
        face = _face(fmap.FindKey(i))
        faces.append(_face_meta(face, i))
        loc = TopLoc_Location()
        tri = _triangulation(face, loc)
        if tri is None:
            continue
        trsf = loc.Transformation()
        n = tri.NbNodes()
        for k in range(1, n + 1):
            p = _tri_node(tri, k).Transformed(trsf)
            positions.extend((round(p.X(), 3), round(p.Y(), 3), round(p.Z(), 3)))
        reversed_ = face.Orientation() == TopAbs_REVERSED
        for t in range(1, tri.NbTriangles() + 1):
            a, b, c = _tri_indices(tri.Triangle(t))
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
            if _degenerated(edge):
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


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a):
    m = math.sqrt(_dot(a, a)) or 1.0
    return (a[0] / m, a[1] / m, a[2] / m)


def _rnd3(p):
    return [round(p[0], 3), round(p[1], 3), round(p[2], 3)]


def _solve3(rows, b):
    """Solve 3x3 linear system via Cramer's rule. rows = 3 vectors, b = 3-tuple."""
    (a, c, d), (e, f, g), (h, i, j) = rows
    det = a * (f * j - g * i) - c * (e * j - g * h) + d * (e * i - f * h)
    if abs(det) < 1e-9:
        return None
    bx, by, bz = b
    dx = bx * (f * j - g * i) - c * (by * j - g * bz) + d * (by * i - f * bz)
    dy = a * (by * j - g * bz) - bx * (e * j - g * h) + d * (e * bz - by * h)
    dz = a * (f * bz - by * i) - c * (e * bz - by * h) + bx * (e * i - f * h)
    return (dx / det, dy / det, dz / det)


def _cylinder_center(face):
    """Axis-projected centre, unit axis, radius of a cylindrical face."""
    s = BRepAdaptor_Surface(face)
    cyl = s.Cylinder()
    base = (cyl.Axis().Location().X(), cyl.Axis().Location().Y(), cyl.Axis().Location().Z())
    axis = _unit((cyl.Axis().Direction().X(), cyl.Axis().Direction().Y(), cyl.Axis().Direction().Z()))
    c = _centroid(face)
    t = _dot((c[0] - base[0], c[1] - base[1], c[2] - base[2]), axis)
    center = [base[i] + axis[i] * t for i in range(3)]
    return center, axis, cyl.Radius()


def measure_single(shape, kind: str, ent_id: int) -> dict:
    """Smart single-entity dimension: cylinder face -> Ø (full hole) or R (arc/bend)."""
    if kind != "face":
        raise ValueError("single-entity dimension needs a cylindrical face")
    f = _face(_sub_shape(shape, "face", ent_id))
    s = BRepAdaptor_Surface(f)
    if s.GetType() != GeomAbs_Cylinder:
        raise ValueError("pick a hole or cylindrical face for Ø / R")
    cyl = s.Cylinder()
    r = cyl.Radius()
    ax = cyl.Axis()
    base = (ax.Location().X(), ax.Location().Y(), ax.Location().Z())
    axis = _unit((ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()))
    c = _centroid(f)
    t = _dot((c[0] - base[0], c[1] - base[1], c[2] - base[2]), axis)
    center = [base[i] + axis[i] * t for i in range(3)]
    perp = _cross(axis, (1.0, 0.0, 0.0))
    if math.sqrt(_dot(perp, perp)) < 1e-6:
        perp = _cross(axis, (0.0, 1.0, 0.0))
    perp = _unit(perp)
    sweep = math.degrees(s.LastUParameter() - s.FirstUParameter())
    if sweep >= 350:
        p1 = [center[i] + perp[i] * r for i in range(3)]
        p2 = [center[i] - perp[i] * r for i in range(3)]
        return {"type": "dia", "value_mm": round(2 * r, 3), "p1": _rnd3(p1), "p2": _rnd3(p2),
                "center": _rnd3(center), "suggested_gauge": "Vernier"}
    p2 = [center[i] + perp[i] * r for i in range(3)]
    return {"type": "rad", "value_mm": round(r, 3), "p1": _rnd3(center), "p2": _rnd3(p2),
            "center": _rnd3(center), "suggested_gauge": "Radius Gauge"}


def measure_single_from_file(path: str, kind, ent_id) -> dict:
    return measure_single(load_step(path), kind, ent_id)


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
        f1, f2 = _face(s1), _face(s2)
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
            # non-parallel planes -> angle, drawn at the shared (dihedral) edge
            n1u, n2u = _unit(n1), _unit(n2)
            ang = math.degrees(math.acos(min(1.0, abs(_dot(n1u, n2u)))))
            c1, c2 = _centroid(f1), _centroid(f2)
            P1 = (pln1.Location().X(), pln1.Location().Y(), pln1.Location().Z())
            pln2 = a2.Plane()
            P2 = (pln2.Location().X(), pln2.Location().Y(), pln2.Location().Z())
            e = _cross(n1u, n2u)
            res = {
                "type": "angle", "value_mm": round(ang, 1),
                "mode": "angle", "method": "face-angle", "suggested_gauge": "Protractor",
                "p1": _rnd3(c1), "p2": _rnd3(c2),
            }
            if _dot(e, e) > 1e-9:
                e = _unit(e)
                p0 = _solve3((n1u, n2u, e), (_dot(n1u, P1), _dot(n2u, P2), 0.0))
                if p0 is not None:
                    mid = [(c1[i] + c2[i]) / 2 for i in range(3)]
                    t = _dot((mid[0] - p0[0], mid[1] - p0[1], mid[2] - p0[2]), e)
                    vtx = [p0[i] + e[i] * t for i in range(3)]
                    def _stub(c):
                        w = [c[i] - vtx[i] for i in range(3)]
                        w = [w[i] - e[i] * _dot(w, e) for i in range(3)]
                        return _unit(w)
                    res["vertex"] = _rnd3(vtx)
                    res["dir1"] = _rnd3(_stub(c1))
                    res["dir2"] = _rnd3(_stub(c2))
            return res

        # cylinder (hole) + plane -> centre-to-surface distance
        if {a1.GetType(), a2.GetType()} == {GeomAbs_Cylinder, GeomAbs_Plane}:
            fcyl, fpln = (f1, f2) if a1.GetType() == GeomAbs_Cylinder else (f2, f1)
            apln = BRepAdaptor_Surface(fpln)
            center, _axis, _r = _cylinder_center(fcyl)
            pln = apln.Plane()
            nn = _unit((pln.Axis().Direction().X(), pln.Axis().Direction().Y(), pln.Axis().Direction().Z()))
            lp = (pln.Location().X(), pln.Location().Y(), pln.Location().Z())
            signed = _dot((center[0] - lp[0], center[1] - lp[1], center[2] - lp[2]), nn)
            foot = [center[i] - nn[i] * signed for i in range(3)]
            return {
                "value_mm": round(abs(signed), 3),
                "p1": _rnd3(center), "p2": _rnd3(foot),
                "mode": "surface-to-surface", "method": "center-to-plane",
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
        surf = BRepAdaptor_Surface(_face(fmap.FindKey(i)))
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
                "desc": f"Check overall {label.lower()}",
                "value_mm": round(val, 2),
                "tol": "+/- 0.5mm",
                "gauge": "Vernier",
                "source": "bbox",
            }
        )

    for d in info.get("holes_mm") or []:
        checks.append(
            {
                "desc": "Check hole diameter",
                "value_mm": round(d, 2),
                "tol": "+/- 0.1mm",
                "gauge": "Vernier",
                "source": "hole",
            }
        )

    for ang in _bend_angles(shape):
        checks.append(
            {
                "desc": "Ensure bend angle",
                "value_mm": ang,
                "tol": "+/- 1 deg",
                "gauge": "Protractor",
                "source": "bend",
            }
        )

    return {"checks": checks, "part": info.get("components", [{}])[0]}
