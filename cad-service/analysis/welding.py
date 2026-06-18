def analyse_welding(raw_analysis: dict) -> dict:
    """Conservative placeholder for future weld seam detection."""
    return {
        "potential_join_length_mm": raw_analysis.get("potential_join_length_mm", 0.0),
        "confidence": "unknown",
        "notes": [],
    }

