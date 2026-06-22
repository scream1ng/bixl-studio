"""Flow model → deterministic SVG process flow chart.

Hand-laid layout (no auto-layout engine) so lines are clean and positions are
exact and portrait-friendly:

  - operations form one vertical spine, top to bottom, linked by a straight
    down-arrow;
  - each operation's raw materials are stacked vertically to its LEFT and merge
    onto a single vertical bus, then one arrow enters the operation;
  - brand colours: operation = black w/ red border, outsourced = red,
    raw material = light grey.

Plain boxes to start (no formal PFC inspection/storage/transport symbols).
"""
from __future__ import annotations

from html import escape

# geometry (px)
_MARGIN = 26
_OP_W = 272
_RAW_W = 248
_GAP_H = 72          # horizontal gap between raw stack and op spine
_GAP_V = 56          # vertical gap between operations
_RAW_VGAP = 18       # vertical gap between stacked raws of one op
_LINE_H = 18
_PAD_Y = 13
_MIN_BOX_H = 58
_FONT = 13           # box text size

# colours — white cards, red border (brand), black text
_OP_FILL, _OP_STROKE, _OP_TEXT = "#FFFFFF", "#CC0000", "#1A1A1A"
_OUT_FILL, _OUT_STROKE, _OUT_TEXT = "#FCEFEF", "#CC0000", "#1A1A1A"   # faint tint = outsourced
_RAW_FILL, _RAW_STROKE, _RAW_TEXT = "#F2F2F2", "#6B6B6B", "#1A1A1A"
_LINE = "#2C2C2C"


def _wrap(text: str, width: int, max_lines: int) -> list[str]:
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if len(cand) <= width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    return lines or [""]


def _op_lines(op: dict) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    code = (op.get("code") or "").strip()
    if code:
        out.append((code, True))                 # WIP part number, bold
    head = f"Operation {op['op_no']}" if op.get("op_no") else (op.get("name") or "Step")
    out.append((head, not code))
    if op.get("machines"):
        for ln in _wrap(", ".join(op["machines"]), 28, 2):
            out.append((ln, False))
    if op.get("outsourced"):
        out.append(("(outsourced)", False))
    return out


def _raw_lines(raw: dict) -> list[tuple[str, bool]]:
    item = (raw.get("item") or "").strip()
    out: list[tuple[str, bool]] = []
    if item:
        out.append((item, True))
    for ln in _wrap(raw.get("desc") or "", 26, 2):
        if ln:
            out.append((ln, False))
    return out or [("", False)]


def _box_h(nlines: int) -> int:
    return max(_MIN_BOX_H, nlines * _LINE_H + _PAD_Y * 2)


def _rect(x, y, w, h, fill, stroke, sw) -> str:
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="4" ry="4" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')


def _text(cx, top, h, lines, color) -> str:
    n = len(lines)
    start = top + (h - n * _LINE_H) / 2 + _LINE_H - 4
    spans = []
    for k, (txt, bold) in enumerate(lines):
        weight = ' font-weight="700"' if bold else ""
        dy = 0 if k == 0 else _LINE_H
        spans.append(f'<tspan x="{cx:.1f}" dy="{dy}"{weight}>{escape(txt)}</tspan>')
    return (f'<text x="{cx:.1f}" y="{start:.1f}" text-anchor="middle" '
            f'font-size="{_FONT}" fill="{color}">{"".join(spans)}</text>')


def model_to_svg(model: dict) -> str:
    ops = model.get("operations") or []
    has_raw = any(op.get("raws") for op in ops)

    spine_x = _MARGIN + (_RAW_W + _GAP_H if has_raw else 0)   # op box left edge
    raw_x = _MARGIN                                           # raw box left edge
    op_cx = spine_x + _OP_W / 2
    total_w = spine_x + _OP_W + _MARGIN

    # ── pass 1: measure rows, assign y ───────────────────────────────────────
    rows = []
    y = _MARGIN
    for op in ops:
        op_lines = _op_lines(op)
        op_h = _box_h(len(op_lines))
        raws = op.get("raws") or []
        raw_boxes = [(_raw_lines(r), _box_h(len(_raw_lines(r)))) for r in raws]
        stack_h = sum(h for _, h in raw_boxes) + max(0, len(raw_boxes) - 1) * _RAW_VGAP
        row_h = max(op_h, stack_h)
        # bottom-align the op in its row so it is the lowest element in the row;
        # the raw stack never drops below the op bottom. This keeps the final
        # process at the very bottom of the chart.
        op_y = y + row_h - op_h
        op_bottom = y + row_h
        cy = op_y + op_h / 2
        if len(raw_boxes) <= 1 or stack_h <= op_h:
            stack_top = cy - stack_h / 2          # centred → straight entry
        else:
            stack_top = op_bottom - stack_h       # bottom-aligned (keeps op lowest)
        rows.append({
            "op": op, "op_lines": op_lines, "op_h": op_h,
            "op_y": op_y, "cy": cy,
            "raw_boxes": raw_boxes, "stack_top": stack_top,
        })
        y += row_h + _GAP_V
    total_h = (y - _GAP_V + _MARGIN) if rows else (2 * _MARGIN)

    # ── pass 2: emit ─────────────────────────────────────────────────────────
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w:.0f}" '
        f'height="{total_h:.0f}" viewBox="0 0 {total_w:.0f} {total_h:.0f}" '
        f'font-family="Arial, Helvetica, sans-serif">',
        f'<defs><marker id="pfcArr" markerWidth="9" markerHeight="9" refX="7.5" '
        f'refY="3" orient="auto"><path d="M0,0 L7.5,3 L0,6 z" fill="{_LINE}"/>'
        f'</marker></defs>',
        f'<rect width="{total_w:.0f}" height="{total_h:.0f}" fill="#FFFFFF"/>',
    ]

    # spine arrows op[i] -> op[i+1]
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        y1 = a["op_y"] + a["op_h"]
        y2 = b["op_y"]
        out.append(f'<path d="M {op_cx:.1f},{y1:.1f} L {op_cx:.1f},{y2:.1f}" '
                   f'fill="none" stroke="{_LINE}" stroke-width="1.6" '
                   f'marker-end="url(#pfcArr)"/>')

    # raw feeds: draw boxes, then connect.
    raw_right = raw_x + _RAW_W
    bus_x = raw_right + 22          # merge bus sits just right of the raw stack
    for r in rows:
        boxes = r["raw_boxes"]
        if not boxes:
            continue
        cy = r["cy"]
        sy = r["stack_top"]
        centres = []
        for lines, h in boxes:
            rcy = sy + h / 2
            out.append(_rect(raw_x, sy, _RAW_W, h, _RAW_FILL, _RAW_STROKE, "1"))
            out.append(_text(raw_x + _RAW_W / 2, sy, h, lines, _RAW_TEXT))
            centres.append(rcy)
            sy += h + _RAW_VGAP
        if len(boxes) == 1:
            # straight horizontal arrow into the op (raw centred on op)
            out.append(f'<path d="M {raw_right:.1f},{centres[0]:.1f} '
                       f'L {spine_x:.1f},{cy:.1f}" fill="none" stroke="{_LINE}" '
                       f'stroke-width="1.4" marker-end="url(#pfcArr)"/>')
        else:
            # comb: short stubs to the bus, then one long arrow into the op
            for rcy in centres:
                out.append(f'<path d="M {raw_right:.1f},{rcy:.1f} '
                           f'L {bus_x:.1f},{rcy:.1f} L {bus_x:.1f},{cy:.1f}" '
                           f'fill="none" stroke="{_LINE}" stroke-width="1.4"/>')
            out.append(f'<path d="M {bus_x:.1f},{cy:.1f} L {spine_x:.1f},{cy:.1f}" '
                       f'fill="none" stroke="{_LINE}" stroke-width="1.4" '
                       f'marker-end="url(#pfcArr)"/>')

    # op boxes (drawn last, on top of lines)
    for r in rows:
        op = r["op"]
        if op.get("outsourced"):
            fill, stroke, txt = _OUT_FILL, _OUT_STROKE, _OUT_TEXT
        else:
            fill, stroke, txt = _OP_FILL, _OP_STROKE, _OP_TEXT
        out.append(_rect(spine_x, r["op_y"], _OP_W, r["op_h"], fill, stroke, "2"))
        out.append(_text(op_cx, r["op_y"], r["op_h"], r["op_lines"], txt))

    out.append("</svg>")
    return "".join(out)
