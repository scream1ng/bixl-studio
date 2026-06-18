from flat_pattern import unfold


def analyse_blank(path: str, k_factor: float = 0.33) -> dict:
    flat = unfold(path, k_factor=k_factor)
    width_mm, height_mm = flat["bbox_mm"]
    return {
        "flat_blank_w_mm": round(width_mm, 1),
        "flat_blank_h_mm": round(height_mm, 1),
        "flat_pattern_area_mm2": round(width_mm * height_mm, 1),
        "svg": flat["svg"],
        "thickness_mm": flat["thickness_mm"],
        "bends": flat["bends"],
        "bbox_mm": flat["bbox_mm"],
    }


def estimate_cut_perimeter(blank_w_mm: float, blank_h_mm: float, holes_mm: list[float]) -> float:
    hole_perimeter = sum(3.14159 * diameter for diameter in holes_mm)
    return round(2 * (blank_w_mm + blank_h_mm) + hole_perimeter, 1)

