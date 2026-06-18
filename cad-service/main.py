import os
import tempfile
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from fastapi.responses import JSONResponse
import base64
from analysis.blank import analyse_blank
from analysis.part_analyser import analyse_part

try:
    from cad.geometry import step_to_edges_json, brep_edges
    from cad.preview import step_to_stl, shape_to_stl
    from cad.loader import load_step
    from exports.jpg import step_to_jpg
    _OCC_AVAILABLE = True
except ImportError:
    _OCC_AVAILABLE = False

app = FastAPI(title="step-to-label API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Recommend changing "*" to your production frontend URL later
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _save_upload(data: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as tmp:
        tmp.write(data)
        return tmp.name


def _check_file(file: UploadFile) -> None:
    name = (file.filename or "").lower()
    if not name.endswith((".step", ".stp")):
        raise HTTPException(400, "Only .step / .stp files accepted")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/full-process")
async def full_process(file: UploadFile = File(...)):
    """Unified endpoint to return mesh, edges, and analysis in one request."""
    _check_file(file)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    tmp_path = _save_upload(data)
    try:
        result = {}
        
        # 1. Regex-based analysis (fast, works without OCC)
        try:
            analysis = analyse_part(tmp_path, filename=file.filename)
            result["analysis"] = analysis
            result["is_assembly"] = analysis.get("is_assembly", False)
            result["component_count"] = analysis.get("component_count", 0)
            result["components"] = analysis.get("components", [])
            result["warnings"] = analysis.get("warnings", [])
        except Exception as e:
            result["analysis_error"] = str(e)
            
        # 2. OCC-based processing
        if _OCC_AVAILABLE:
            try:
                shape = load_step(tmp_path)
                # STL Mesh
                stl_bytes = shape_to_stl(shape)
                result["stl_base64"] = base64.b64encode(stl_bytes).decode("utf-8")
                # Edges
                result["edges"] = brep_edges(shape)
            except Exception as e:
                result["occ_error"] = str(e)
        else:
            result["occ_error"] = "OCC not available"
            
        return JSONResponse(content=result)
    finally:
        os.unlink(tmp_path)


@app.post("/api/mesh")
async def mesh(file: UploadFile = File(...)):
    """Return binary STL for Three.js interactive preview."""
    if not _OCC_AVAILABLE:
        raise HTTPException(503, "3D viewer requires pythonocc-core (conda install)")
    _check_file(file)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    tmp_path = _save_upload(data)
    try:
        stl = step_to_stl(tmp_path)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Mesh error: {e}")
    finally:
        os.unlink(tmp_path)
    return Response(content=stl, media_type="model/stl")


@app.post("/api/edges")
async def edges(file: UploadFile = File(...)):
    """Return BRep edges as JSON 3D polylines (includes fillet/smooth edges)."""
    if not _OCC_AVAILABLE:
        raise HTTPException(503, "3D viewer requires pythonocc-core (conda install)")
    _check_file(file)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    tmp_path = _save_upload(data)
    try:
        edge_data = step_to_edges_json(tmp_path)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Edge error: {e}")
    finally:
        os.unlink(tmp_path)
    return JSONResponse(content=edge_data)


@app.post("/api/convert")
async def convert(
    file: UploadFile = File(...),
    line_px: int = Query(3, ge=1, le=20),
    view: str = Query("isometric"),
    label_cm: float = Query(26.0, ge=1.0, le=100.0),
    dpi: int = Query(300, ge=72, le=1200),
    eye_x: Optional[float] = Query(None),
    eye_y: Optional[float] = Query(None),
    eye_z: Optional[float] = Query(None),
    right_x: Optional[float] = Query(None),
    right_y: Optional[float] = Query(None),
    right_z: Optional[float] = Query(None),
    target_x: Optional[float] = Query(None),
    target_y: Optional[float] = Query(None),
    target_z: Optional[float] = Query(None),
    focus: Optional[float] = Query(None, gt=0),
    fov_deg: Optional[float] = Query(None, gt=0, lt=180),
):
    if not _OCC_AVAILABLE:
        raise HTTPException(503, "Convert requires pythonocc-core (conda install)")
    _check_file(file)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    tmp_path = _save_upload(data)

    # Use camera vectors from Three.js if all six supplied
    eye = (eye_x, eye_y, eye_z) if None not in (eye_x, eye_y, eye_z) else None
    right = (right_x, right_y, right_z) if None not in (right_x, right_y, right_z) else None
    target = (target_x, target_y, target_z) if None not in (target_x, target_y, target_z) else None

    try:
        jpg = step_to_jpg(
            tmp_path,
            line_px=line_px,
            view=view,
            eye=eye,
            right=right,
            target=target,
            focus=focus,
            fov_deg=fov_deg,
            label_cm=label_cm,
            dpi=dpi,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Processing error: {e}")
    finally:
        os.unlink(tmp_path)

    stem = os.path.splitext(file.filename or "part")[0]
    return Response(
        content=jpg,
        media_type="image/jpeg",
        headers={"Content-Disposition": f'attachment; filename="{stem}_label.jpg"'},
    )


@app.post("/api/analyse")
async def analyse(file: UploadFile = File(...)):
    """Return fabrication feature analysis (bbox, bends, perimeter, etc.) for cost estimation."""
    _check_file(file)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    tmp_path = _save_upload(data)
    try:
        result = analyse_part(tmp_path, filename=file.filename)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Analysis error: {e}")
    finally:
        os.unlink(tmp_path)
    return JSONResponse(content=result)


@app.post("/api/flat-pattern")
async def flat_pattern(
    file: UploadFile = File(...),
    k_factor: float = Query(0.33, ge=0.0, le=0.5),
):
    """Return flat pattern as SVG + metadata (bends, blank size, bend allowances)."""
    _check_file(file)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    tmp_path = _save_upload(data)
    try:
        result = analyse_blank(tmp_path, k_factor=k_factor)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Flat pattern error: {e}")
    finally:
        os.unlink(tmp_path)
    # Return SVG directly; include metadata in headers
    w, h = result["bbox_mm"]
    meta = {
        "thickness_mm": result["thickness_mm"],
        "bends": result["bends"],
        "blank_w_mm": round(w, 1),
        "blank_h_mm": round(h, 1),
    }
    import json
    return Response(
        content=result["svg"].encode("utf-8"),
        media_type="image/svg+xml",
        headers={"X-Flat-Pattern-Meta": json.dumps(meta)},
    )
