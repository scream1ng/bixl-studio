import math
from typing import Tuple

from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_Reader
from OCP.TopoDS import TopoDS, TopoDS_Edge

Vec3 = Tuple[float, float, float]


def as_edge(shape) -> TopoDS_Edge:
    """Downcast TopoDS_Shape to TopoDS_Edge across pythonocc-core and cadquery-ocp."""
    if hasattr(TopoDS, "Edge_s"):
        return TopoDS.Edge_s(shape)
    if hasattr(TopoDS, "Edge"):
        try:
            return TopoDS.Edge(shape)
        except Exception:
            pass
    edge = TopoDS_Edge()
    edge.TShape(shape.TShape())
    edge.Location(shape.Location())
    edge.Orientation(shape.Orientation())
    return edge


def finite(value: float) -> bool:
    return math.isfinite(value)


def norm(vector: Vec3) -> Vec3:
    x, y, z = vector
    magnitude = math.sqrt(x * x + y * y + z * z) or 1.0
    return (x / magnitude, y / magnitude, z / magnitude)


def load_step(path: str):
    reader = STEPControl_Reader()
    if reader.ReadFile(path) != IFSelect_RetDone:
        raise ValueError(f"Cannot read STEP file: {path}")
    reader.TransferRoots()
    return reader.OneShape()

