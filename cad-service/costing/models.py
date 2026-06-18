from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessSelection:
    laser: bool = True
    bending: bool = True
    welding: bool = False
    finishing: bool = False
    packing: bool = True


@dataclass(frozen=True)
class CostParams:
    moq: int
    material_key: str
    sheet_cost: float
    thickness_override_mm: float | None
    sheet_index: int
    processes: ProcessSelection
    laser_pcs_per_hour: float
    bending_pcs_per_hour: float
    welding_pcs_per_hour: float
    finishing_pcs_per_hour: float
    packing_pcs_per_hour: float
    laser_setup_min: float
    bending_setup_min: float
    welding_setup_min: float
    finishing_setup_min: float
    packing_setup_min: float
    laser_rate: float
    bending_rate: float
    welding_rate: float
    finishing_rate: float
    finishing_cost: float
    packing_rate: float
    weld_length_mm: float
    box_length_mm: float
    box_width_mm: float
    box_height_mm: float
