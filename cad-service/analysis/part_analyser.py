import os
import re
import tempfile
from typing import Any

from analysis.assembly import parse_assembly, _is_purchase_part, _clean_nx_name
from analysis.bends import analyse_bends
from analysis.blank import analyse_blank, estimate_cut_perimeter
from analysis.features import analyse_step_features_text, classify_component
from analysis.holes import analyse_holes
from analysis.welding import analyse_welding

_NX_PREFIX = re.compile(r'^.+?_\d+_\d+-')
_NX_SUFFIX = re.compile(r'\s+_[^_]+_\s*$')


def _norm(name: str) -> str:
    """Normalise a product/part name for fuzzy matching."""
    s = _NX_PREFIX.sub('', name, count=1).strip()
    s = _NX_SUFFIX.sub('', s).strip() if s else name
    return s.upper()


def _extract_component_shapes(path: str) -> dict[str, Any]:
    """Return {normalised_name: TopoDS_Shape} for each product in the assembly."""
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.TDF import TDF_LabelSequence

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document("MDTV-XCAF")
    app.NewDocument("MDTV-XCAF", doc)

    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if reader.ReadFile(path) != 1:   # IFSelect_RetDone == 1
        return {}
    reader.Transfer(doc)

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    free_labels = TDF_LabelSequence()
    shape_tool.GetFreeShapes(free_labels)

    result: dict[str, Any] = {}

    def _label_name(label) -> str:
        from OCP.TDataStd import TDataStd_Name
        n = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), n):
            return n.Get().ToExtString()
        return ""

    def _visit(label, depth: int) -> None:
        name = _label_name(label)
        shape = shape_tool.GetShape_s(label)
        if name and not shape.IsNull():
            key = _norm(_clean_nx_name(name))
            if key and key not in result:
                result[key] = shape

        comp_labels = TDF_LabelSequence()
        shape_tool.GetComponents_s(label, comp_labels, False)
        for i in range(1, comp_labels.Size() + 1):
            comp_lbl = comp_labels.Value(i)
            referred = comp_lbl
            if shape_tool.GetReferredShape_s(comp_lbl, referred):
                comp_lbl = referred
            _visit(comp_lbl, depth + 1)

    for i in range(1, free_labels.Size() + 1):
        _visit(free_labels.Value(i), 0)

    return result


def _analyse_shape_geometry(shape) -> dict:
    """Write shape to a temp STEP file and run the full analysis pipeline on it."""
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
    from OCP.IFSelect import IFSelect_RetDone

    geom: dict = {}
    fd, tmp_path = tempfile.mkstemp(suffix=".stp")
    os.close(fd)
    try:
        writer = STEPControl_Writer()
        writer.Transfer(shape, STEPControl_AsIs)
        if writer.Write(tmp_path) != IFSelect_RetDone:
            return geom

        with open(tmp_path, "r", errors="replace") as f:
            text = f.read()

        raw = analyse_step_features_text(text)
        holes = analyse_holes(raw)
        bends = analyse_bends(raw)
        geom = {**raw, **holes, **bends}

        try:
            blank = analyse_blank(tmp_path, k_factor=0.33)
            geom["flat_blank_w_mm"] = blank["flat_blank_w_mm"]
            geom["flat_blank_h_mm"] = blank["flat_blank_h_mm"]
            geom["flat_pattern_area_mm2"] = blank["flat_pattern_area_mm2"]
            geom["cut_perimeter_mm"] = estimate_cut_perimeter(
                blank["flat_blank_w_mm"],
                blank["flat_blank_h_mm"],
                holes["holes_mm"],
            )
        except Exception:
            pass
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return geom


def analyse_part(path: str, filename: str | None = None) -> dict:
    with open(path, "r", errors="replace") as fh:
        text = fh.read()

    assembly = parse_assembly(text, filename=filename)

    if assembly["is_assembly"]:
        result = {
            "thickness_mm": 0.0,
            "bend_count": 0,
            "hole_count": 0,
            "holes_mm": [],
            "bend_radii_mm": [],
            "bbox_mm": [0.0, 0.0, 0.0],
            "flat_area_mm2": 0.0,
            "flat_pattern_area_mm2": 0.0,
            "cut_perimeter_mm": 0.0,
            "flat_blank_w_mm": 0.0,
            "flat_blank_h_mm": 0.0,
            "welding": {"weld_length_mm": 0.0},
        }
        # Try to extract per-component shapes via XDE — fail silently
        try:
            component_shapes = _extract_component_shapes(path)
        except Exception:
            component_shapes = {}

        for comp in assembly["components"]:
            if comp["is_assembly"]:
                comp["type"] = "assembly"
            elif _is_purchase_part(comp["part_number"]) or _is_purchase_part(comp.get("description", "")):
                comp["type"] = "purchase"
            else:
                comp["type"] = "sheet_metal"
                # Try to extract and merge geometry for this sheet_metal component
                try:
                    key = _norm(comp["part_number"])
                    shape = component_shapes.get(key)
                    if shape is not None and not shape.IsNull():
                        geom = _analyse_shape_geometry(shape)
                        _GEOM_FIELDS = (
                            "thickness_mm", "bend_count", "bend_radii_mm",
                            "hole_count", "holes_mm", "flat_area_mm2",
                            "flat_pattern_area_mm2", "flat_blank_w_mm",
                            "flat_blank_h_mm", "cut_perimeter_mm", "bbox_mm",
                        )
                        for field in _GEOM_FIELDS:
                            if field in geom:
                                comp[field] = geom[field]
                except Exception:
                    pass
    else:
        raw = analyse_step_features_text(text)
        holes = analyse_holes(raw)
        bends = analyse_bends(raw)
        welding = analyse_welding(raw)

        result = {**raw, **holes, **bends, "welding": welding}

        try:
            blank = analyse_blank(path, k_factor=0.33)
            result["flat_blank_w_mm"] = blank["flat_blank_w_mm"]
            result["flat_blank_h_mm"] = blank["flat_blank_h_mm"]
            result["flat_pattern_area_mm2"] = blank["flat_pattern_area_mm2"]
            result["cut_perimeter_mm"] = estimate_cut_perimeter(
                blank["flat_blank_w_mm"],
                blank["flat_blank_h_mm"],
                holes["holes_mm"],
            )
        except Exception:
            pass

        geom_type = classify_component(result)
        for comp in assembly["components"]:
            if comp["is_assembly"]:
                comp["type"] = "assembly"
            elif _is_purchase_part(comp["part_number"]) or _is_purchase_part(comp.get("description", "")):
                comp["type"] = "purchase"
            else:
                comp["type"] = geom_type

    result["is_assembly"] = assembly["is_assembly"]
    result["component_count"] = assembly["component_count"]
    result["components"] = assembly["components"]
    result["warnings"] = assembly.get("warnings", [])

    return result
