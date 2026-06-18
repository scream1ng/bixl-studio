from costing.materials import MATERIALS, STANDARD_SHEETS
from costing.rates import RATES


def calculate_cost(analysis: dict, params: dict) -> dict:
    qty = max(int(params.get("moq", 1)), 1)
    material = MATERIALS.get(params.get("material_key"), MATERIALS["Mild Steel"])
    sheet = STANDARD_SHEETS[params.get("sheet_index", 0)]
    thickness = params.get("thickness_override_mm") or analysis["thickness_mm"]
    blank_w = analysis.get("flat_blank_w_mm") or analysis["bbox_mm"][0]
    blank_h = analysis.get("flat_blank_h_mm") or analysis["bbox_mm"][1]

    parts_per_sheet = max(1, int(sheet["w"] // blank_w) * int(sheet["h"] // blank_h))
    blank_mass_kg = ((blank_w * blank_h * thickness) / 1e9) * material["density"]
    material_unit = params.get("sheet_cost", 0.0) / parts_per_sheet

    processes = params.get("processes", {})
    laser_run_min = analysis.get("cut_perimeter_mm", 0.0) / material["laser_speed_mm_per_min"]
    laser_auto_pcs_per_hour = 60 / laser_run_min if laser_run_min > 0 else 0.0
    laser_pcs_per_hour = params.get("laser_pcs_per_hour") or laser_auto_pcs_per_hour

    bend_run_min = (analysis.get("bend_count", 0) * RATES["sec_per_bend"]) / 60
    bending_auto_pcs_per_hour = 60 / bend_run_min if bend_run_min > 0 else 0.0
    bending_pcs_per_hour = params.get("bending_pcs_per_hour") or bending_auto_pcs_per_hour

    weld_run_min = params.get("weld_length_mm", 0) / RATES["weld_speed_mm_per_min"]
    welding_auto_pcs_per_hour = 60 / weld_run_min if weld_run_min > 0 else 0.0
    welding_pcs_per_hour = params.get("welding_pcs_per_hour") or welding_auto_pcs_per_hour

    def process_unit(active: bool, rate: float, pcs_per_hour: float, setup_min: float) -> float:
        if not active or pcs_per_hour <= 0:
            return 0.0
        return (rate / pcs_per_hour) + ((setup_min / 60) * rate / qty)

    cutting_unit = process_unit(
        processes.get("laser", True),
        params.get("laser_rate", RATES["laser"]),
        laser_pcs_per_hour,
        params.get("laser_setup_min", 0),
    )
    bending_unit = process_unit(
        processes.get("bending", True) and analysis.get("bend_count", 0) > 0,
        params.get("bending_rate", RATES["bending"]),
        bending_pcs_per_hour,
        params.get("bending_setup_min", 0),
    )
    welding_unit = process_unit(
        processes.get("welding", False),
        params.get("welding_rate", RATES["welding"]),
        welding_pcs_per_hour,
        params.get("welding_setup_min", 0),
    )
    finishing_unit = max(0.0, params.get("finishing_cost", 0.0)) if processes.get("finishing", False) else 0.0
    packing_unit = process_unit(
        processes.get("packing", True),
        params.get("packing_rate", RATES["packing"]),
        params.get("packing_pcs_per_hour", 0),
        params.get("packing_setup_min", 0),
    )

    total_unit = material_unit + cutting_unit + bending_unit + welding_unit + finishing_unit + packing_unit
    return {
        "materialUnit": material_unit,
        "cuttingUnit": cutting_unit,
        "bendingUnit": bending_unit,
        "weldingUnit": welding_unit,
        "finishingUnit": finishing_unit,
        "packingUnit": packing_unit,
        "totalUnit": total_unit,
        "totalAll": total_unit * qty,
        "partsPerSheet": parts_per_sheet,
        "sheetsNeeded": -(-qty // parts_per_sheet),
        "blankMassKg": blank_mass_kg,
        "laserPcsPerHour": laser_pcs_per_hour,
        "bendingPcsPerHour": bending_pcs_per_hour,
        "weldingPcsPerHour": welding_pcs_per_hour,
        "finishingPcsPerHour": 0,
        "packingPcsPerHour": params.get("packing_pcs_per_hour", 0),
    }
