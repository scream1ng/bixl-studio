import os
import tempfile

from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.StlAPI import StlAPI_Writer

from cad.geometry import step_to_edges_json
from cad.loader import load_step


def shape_to_stl(shape) -> bytes:
    BRepMesh_IncrementalMesh(shape, 0.03, False, 0.12).Perform()
    tmp = tempfile.NamedTemporaryFile(suffix=".stl", delete=False)
    tmp.close()
    try:
        StlAPI_Writer().Write(shape, tmp.name)
        with open(tmp.name, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(tmp.name)


def step_to_stl(path: str) -> bytes:
    return shape_to_stl(load_step(path))


def preview_edges(path: str) -> list:
    return step_to_edges_json(path)

