import io
import math
from typing import List, Tuple

from PIL import Image, ImageDraw

from cad.geometry import visible_polylines
from cad.loader import Vec3, load_step, norm

DEFAULT_DPI = 300
DEFAULT_LABEL_CM = 26.0
EXPORT_SUPERSAMPLE = 2
PADDING_FRAC = 0.08

_S2 = math.sqrt(2)
_S3 = math.sqrt(3)

VIEWS: dict[str, Tuple[Vec3, Vec3]] = {
    "isometric": ((1 / _S3, 1 / _S3, 1 / _S3), (1 / _S2, -1 / _S2, 0.0)),
    "front": ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
    "back": ((0.0, -1.0, 0.0), (-1.0, 0.0, 0.0)),
    "right": ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),
    "left": ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    "top": ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
    "bottom": ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0)),
}


def label_px(label_cm: float, dpi: int) -> int:
    return max(1, round((label_cm / 2.54) * dpi))


def polylines_to_jpg(
    lines: List,
    line_px: int = 3,
    label_cm: float = DEFAULT_LABEL_CM,
    dpi: int = DEFAULT_DPI,
    focus: float | None = None,
    fov_deg: float | None = None,
) -> bytes:
    if not lines:
        raise ValueError("No visible edges found; check STEP file")

    final_px = label_px(label_cm, dpi)
    work_px = final_px * EXPORT_SUPERSAMPLE

    if focus is not None and fov_deg is not None:
        half_view = focus * math.tan(math.radians(fov_deg) / 2.0)
        if half_view <= 0:
            raise ValueError("Invalid perspective framing")
        pixels_per_unit = (work_px / 2.0) / half_view

        def to_px(px: float, py: float) -> Tuple[int, int]:
            return (
                int(round(work_px / 2.0 + px * pixels_per_unit)),
                int(round(work_px / 2.0 - py * pixels_per_unit)),
            )
    else:
        all_pts = [p for polyline in lines for p in polyline]
        xs, ys = zip(*all_pts)
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        span = max(xmax - xmin, ymax - ymin) or 1.0
        draw_px = work_px * (1.0 - 2.0 * PADDING_FRAC)
        scale = draw_px / span
        ox = (work_px - (xmax - xmin) * scale) / 2.0
        oy = (work_px - (ymax - ymin) * scale) / 2.0

        def to_px(px: float, py: float) -> Tuple[int, int]:
            return (
                int((px - xmin) * scale + ox),
                int((ymax - py) * scale + oy),
            )

    img = Image.new("RGB", (work_px, work_px), "white")
    draw = ImageDraw.Draw(img)
    line_width = max(1, round(line_px * EXPORT_SUPERSAMPLE))
    for polyline in lines:
        if len(polyline) < 2:
            continue
        coords = [to_px(*p) for p in polyline]
        for i in range(len(coords) - 1):
            draw.line([coords[i], coords[i + 1]], fill="black", width=line_width)

    if work_px != final_px:
        img = img.resize((final_px, final_px), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=100, subsampling=0, dpi=(dpi, dpi))
    return buf.getvalue()


def step_to_jpg(
    path: str,
    line_px: int = 3,
    view: str = "isometric",
    eye: Vec3 | None = None,
    right: Vec3 | None = None,
    target: Vec3 | None = None,
    focus: float | None = None,
    fov_deg: float | None = None,
    label_cm: float = DEFAULT_LABEL_CM,
    dpi: int = DEFAULT_DPI,
) -> bytes:
    shape = load_step(path)

    if eye is not None and right is not None:
        origin = target if target is not None else (0.0, 0.0, 0.0)
        n = norm((eye[0] - origin[0], eye[1] - origin[1], eye[2] - origin[2]))
        vx = norm(right)
    else:
        if view not in VIEWS:
            raise ValueError(f"Unknown view '{view}'")
        n, vx = VIEWS[view]
        origin = (0.0, 0.0, 0.0)
        focus = None
        fov_deg = None

    lines = visible_polylines(shape, n, vx, origin=origin, focus=focus)
    return polylines_to_jpg(
        lines,
        line_px=line_px,
        label_cm=label_cm,
        dpi=dpi,
        focus=focus,
        fov_deg=fov_deg,
    )

