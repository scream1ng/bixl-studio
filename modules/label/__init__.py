"""Label module — proxies STEP files to cad-service, returns wireframe JPG bytes."""
from __future__ import annotations

import os

import requests

_CAD_URL = os.environ.get("CAD_API_URL", "http://localhost:8000")


def step_to_wireframe(
    step_bytes: bytes,
    filename: str = "part.step",
    view: str = "isometric",
    line_px: int = 3,
    label_cm: float = 26.0,
    dpi: int = 300,
    eye: tuple | None = None,
    right: tuple | None = None,
    target: tuple | None = None,
    fov_deg: float | None = None,
) -> bytes:
    """POST STEP file to cad-service /api/convert, return JPG bytes."""
    params: dict = {
        "view": view,
        "line_px": line_px,
        "label_cm": label_cm,
        "dpi": dpi,
    }
    if eye is not None:
        params |= {"eye_x": eye[0], "eye_y": eye[1], "eye_z": eye[2]}
    if right is not None:
        params |= {"right_x": right[0], "right_y": right[1], "right_z": right[2]}
    if target is not None:
        params |= {"target_x": target[0], "target_y": target[1], "target_z": target[2]}
    if fov_deg is not None:
        params["fov_deg"] = fov_deg

    resp = requests.post(
        f"{_CAD_URL}/api/convert",
        files={"file": (filename, step_bytes, "application/octet-stream")},
        params=params,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def step_to_mesh_and_edges(step_bytes: bytes, filename: str = "part.step") -> tuple[bytes, list]:
    """Return (stl_bytes, edges_json) for Three.js viewer."""
    import concurrent.futures

    def _mesh():
        r = requests.post(
            f"{_CAD_URL}/api/mesh",
            files={"file": (filename, step_bytes, "application/octet-stream")},
            timeout=60,
        )
        r.raise_for_status()
        return r.content

    def _edges():
        r = requests.post(
            f"{_CAD_URL}/api/edges",
            files={"file": (filename, step_bytes, "application/octet-stream")},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_mesh = ex.submit(_mesh)
        f_edges = ex.submit(_edges)
        return f_mesh.result(), f_edges.result()
