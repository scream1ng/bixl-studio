def analyse_bends(raw_analysis: dict) -> dict:
    radii = raw_analysis.get("bend_radii_mm", [])
    return {
        "bend_count": raw_analysis.get("bend_count", len(radii)),
        "bend_radii_mm": radii,
        "bend_lengths_mm": raw_analysis.get("bend_lengths_mm", []),
        "bend_angles_deg": raw_analysis.get("bend_angles_deg", []),
    }

