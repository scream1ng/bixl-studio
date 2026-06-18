def analyse_holes(raw_analysis: dict) -> dict:
    holes = raw_analysis.get("holes_mm", [])
    return {
        "hole_count": raw_analysis.get("hole_count", len(holes)),
        "holes_mm": sorted(holes),
    }

