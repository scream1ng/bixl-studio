import logging
from typing import Callable, List, Tuple

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from cad.loader import Vec3, as_edge, finite, load_step

logger = logging.getLogger(__name__)

MAX_EDGE_POLYLINES = 2500
MAX_EDGE_POINTS = 40000


def extract_projected_edges(
    compound,
    project: Callable[[float, float, float], Tuple[float, float]],
    result: List,
) -> None:
    if compound.IsNull():
        return
    exp = TopExp_Explorer(compound, TopAbs_EDGE)
    while exp.More():
        edge = as_edge(exp.Current())
        try:
            curve = BRepAdaptor_Curve(edge)
            disc = GCPnts_TangentialDeflection()
            disc.Initialize(curve, 0.15, 0.02)
            pts = [project(*disc.Value(i).Coord()) for i in range(1, disc.NbPoints() + 1)]
            if len(pts) >= 2:
                result.append(pts)
        except Exception:
            pass
        exp.Next()


def visible_polylines(
    shape,
    n: Vec3,
    vx: Vec3,
    origin: Vec3 = (0.0, 0.0, 0.0),
    focus: float | None = None,
) -> List:
    ax2 = gp_Ax2(gp_Pnt(*origin), gp_Dir(*n), gp_Dir(*vx))
    projector = HLRAlgo_Projector(ax2, focus) if focus is not None else HLRAlgo_Projector(ax2)

    def project(x: float, y: float, z: float) -> Tuple[float, float]:
        px, py, _ = projector.Project(gp_Pnt(x, y, z))
        return (px, py)

    hlr = HLRBRep_Algo()
    hlr.Add(shape)
    hlr.Projector(projector)
    hlr.Update()
    hlr.Hide()
    hs = HLRBRep_HLRToShape(hlr)
    result = []
    for getter_name in ("VCompound", "Rg1LineVCompound", "RgNLineVCompound", "OutLineVCompound"):
        getter = getattr(hs, getter_name, None)
        if getter is not None:
            extract_projected_edges(getter(), project, result)
    return result


def brep_edges(shape) -> list:
    """Return BRep edges as 3D polylines, including smooth/tangent radius edges."""
    result = []
    total_points = 0
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        if len(result) >= MAX_EDGE_POLYLINES or total_points >= MAX_EDGE_POINTS:
            logger.warning(
                "brep_edges capped at %d polylines / %d points",
                len(result),
                total_points,
            )
            break
        curr = exp.Current()
        if curr.IsNull():
            exp.Next()
            continue
        try:
            edge = as_edge(curr)
            if BRep_Tool.Degenerated_s(edge):
                exp.Next()
                continue

            curve = BRepAdaptor_Curve(edge)
            first = curve.FirstParameter()
            last = curve.LastParameter()
            if not (finite(first) and finite(last) and last > first):
                exp.Next()
                continue
            disc = GCPnts_TangentialDeflection()
            disc.Initialize(curve, 0.3, 0.04)
            count = disc.NbPoints()
            if count < 2:
                exp.Next()
                continue
            pts = []
            for i in range(1, count + 1):
                p = disc.Value(i)
                x, y, z = p.X(), p.Y(), p.Z()
                if finite(x) and finite(y) and finite(z):
                    pts.append([round(x, 3), round(y, 3), round(z, 3)])
            if len(pts) >= 2:
                result.append(pts)
                total_points += len(pts)
        except Exception as exc:
            logger.warning("Edge skip: %s", exc)
        exp.Next()
    logger.info("brep_edges: %d polylines extracted", len(result))
    return result


def step_to_edges_json(path: str) -> list:
    return brep_edges(load_step(path))

