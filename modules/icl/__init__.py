"""ICL module — proxies STEP files to cad-service for inspection measuring."""
from __future__ import annotations

import os

import requests

_CAD_URL = os.environ.get("CAD_API_URL", "http://localhost:8000")


def _post(endpoint: str, step_bytes: bytes, filename: str, params: dict | None = None):
    resp = requests.post(
        f"{_CAD_URL}{endpoint}",
        files={"file": (filename, step_bytes, "application/octet-stream")},
        params=params or {},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def icl_mesh(step_bytes: bytes, filename: str = "part.step") -> dict:
    return _post("/api/icl/mesh", step_bytes, filename)


def icl_edges(step_bytes: bytes, filename: str = "part.step") -> dict:
    return _post("/api/icl/edges", step_bytes, filename)


def icl_suggest(step_bytes: bytes, filename: str = "part.step") -> dict:
    return _post("/api/icl/suggest", step_bytes, filename)


def icl_measure(
    step_bytes: bytes,
    kind1: str,
    id1: int,
    kind2: str,
    id2: int,
    filename: str = "part.step",
) -> dict:
    return _post(
        "/api/icl/measure",
        step_bytes,
        filename,
        params={"kind1": kind1, "id1": id1, "kind2": kind2, "id2": id2},
    )
