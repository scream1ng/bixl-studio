"""Compatibility wrappers for older imports.

New code should use:
- cad.loader for STEP loading
- cad.geometry for edge extraction
- cad.preview for STL preview export
- exports.jpg for label JPG export
"""

from cad.geometry import step_to_edges_json, visible_polylines
from cad.loader import load_step
from cad.preview import step_to_stl
from exports.jpg import polylines_to_jpg, step_to_jpg

__all__ = [
    "load_step",
    "polylines_to_jpg",
    "step_to_edges_json",
    "step_to_jpg",
    "step_to_stl",
    "visible_polylines",
]

