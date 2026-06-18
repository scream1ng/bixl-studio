import pytest
from analysis.assembly import parse_assembly, _is_purchase_part
from analysis.features import classify_component

SINGLE_PART = """
ISO-10303-21;
HEADER;
ENDSEC;
DATA;
#10=PRODUCT('P001','Bracket','A simple bracket',(#11));
#11=PRODUCT_CONTEXT('',#12,'mechanical');
#12=APPLICATION_CONTEXT('core data for automotive mechanical design processes');
#20=PRODUCT_DEFINITION_FORMATION('','',#10);
#30=PRODUCT_DEFINITION('design','',#20,#40);
#40=PRODUCT_DEFINITION_CONTEXT('part definition',#12,'design');
ENDSEC;
END-ISO-10303-21;
"""

ASSEMBLY_TWO_PARTS = """
ISO-10303-21;
HEADER;
ENDSEC;
DATA;
#10=PRODUCT('ASSY001','Main Assembly','',(#11));
#20=PRODUCT('P001','Part A','Sheet metal part',(#11));
#30=PRODUCT('P002','Part B','Machined part',(#11));
#11=PRODUCT_CONTEXT('',#12,'mechanical');
#12=APPLICATION_CONTEXT('core data for automotive mechanical design processes');
#100=PRODUCT_DEFINITION_FORMATION('','',#10);
#110=PRODUCT_DEFINITION_FORMATION('','',#20);
#120=PRODUCT_DEFINITION_FORMATION('','',#30);
#200=PRODUCT_DEFINITION('design','',#100,#40);
#210=PRODUCT_DEFINITION('design','',#110,#40);
#220=PRODUCT_DEFINITION('design','',#120,#40);
#40=PRODUCT_DEFINITION_CONTEXT('part definition',#12,'design');
#300=NEXT_ASSEMBLY_USAGE_OCCURENCE('1','ref1','',#200,#210,$);
#310=NEXT_ASSEMBLY_USAGE_OCCURENCE('2','ref2','',#200,#220,$);
ENDSEC;
END-ISO-10303-21;
"""

NESTED_ASSEMBLY = """
ISO-10303-21;
HEADER;
ENDSEC;
DATA;
#10=PRODUCT('TOP','Top Assembly','',(#11));
#20=PRODUCT('SUB','Sub Assembly','',(#11));
#30=PRODUCT('P001','Leaf Part','Leaf',(#11));
#11=PRODUCT_CONTEXT('',#12,'mechanical');
#12=APPLICATION_CONTEXT('');
#100=PRODUCT_DEFINITION_FORMATION('','',#10);
#110=PRODUCT_DEFINITION_FORMATION('','',#20);
#120=PRODUCT_DEFINITION_FORMATION('','',#30);
#200=PRODUCT_DEFINITION('design','',#100,#40);
#210=PRODUCT_DEFINITION('design','',#110,#40);
#220=PRODUCT_DEFINITION('design','',#120,#40);
#40=PRODUCT_DEFINITION_CONTEXT('',#12,'design');
#300=NEXT_ASSEMBLY_USAGE_OCCURENCE('1','','',#200,#210,$);
#310=NEXT_ASSEMBLY_USAGE_OCCURENCE('2','','',#210,#220,$);
ENDSEC;
END-ISO-10303-21;
"""


class TestSinglePart:
    def test_not_assembly(self):
        result = parse_assembly(SINGLE_PART)
        assert result["is_assembly"] is False

    def test_component_count(self):
        result = parse_assembly(SINGLE_PART)
        assert result["component_count"] == 1

    def test_component_fields(self):
        comp = parse_assembly(SINGLE_PART)["components"][0]
        assert comp["part_number"] == "P001"
        assert comp["description"] == "A simple bracket"
        assert comp["level"] == 0
        assert comp["is_assembly"] is False


class TestAssemblyTwoParts:
    def test_is_assembly(self):
        result = parse_assembly(ASSEMBLY_TWO_PARTS)
        assert result["is_assembly"] is True

    def test_component_count(self):
        result = parse_assembly(ASSEMBLY_TWO_PARTS)
        assert result["component_count"] == 3  # root + 2 children

    def test_root_level(self):
        comps = parse_assembly(ASSEMBLY_TWO_PARTS)["components"]
        root = next(c for c in comps if c["level"] == 0)
        assert root["is_assembly"] is True

    def test_children_level(self):
        comps = parse_assembly(ASSEMBLY_TWO_PARTS)["components"]
        children = [c for c in comps if c["level"] == 1]
        assert len(children) == 2

    def test_children_not_assembly(self):
        comps = parse_assembly(ASSEMBLY_TWO_PARTS)["components"]
        children = [c for c in comps if c["level"] == 1]
        assert all(not c["is_assembly"] for c in children)


class TestNestedAssembly:
    def test_levels(self):
        comps = parse_assembly(NESTED_ASSEMBLY)["components"]
        levels = sorted(c["level"] for c in comps)
        assert levels == [0, 1, 2]

    def test_leaf_not_assembly(self):
        comps = parse_assembly(NESTED_ASSEMBLY)["components"]
        leaf = next(c for c in comps if c["level"] == 2)
        assert leaf["is_assembly"] is False
        assert leaf["part_number"] == "P001"


ASSEMBLY_DOUBLE_R_NAUO = ASSEMBLY_TWO_PARTS.replace(
    "NEXT_ASSEMBLY_USAGE_OCCURENCE", "NEXT_ASSEMBLY_USAGE_OCCURRENCE"
)

DUPLICATE_CHILD_ASSEMBLY = ASSEMBLY_TWO_PARTS.replace(
    "#310=NEXT_ASSEMBLY_USAGE_OCCURENCE('2','ref2','',#200,#220,$);",
    "\n".join([
        "#310=NEXT_ASSEMBLY_USAGE_OCCURENCE('2','ref2','',#200,#220,$);",
        "#311=NEXT_ASSEMBLY_USAGE_OCCURENCE('3','ref3','',#200,#220,$);",
        "#312=NEXT_ASSEMBLY_USAGE_OCCURENCE('4','ref4','',#200,#220,$);",
        "#313=NEXT_ASSEMBLY_USAGE_OCCURENCE('5','ref5','',#200,#220,$);",
    ]),
)


class TestNauoSpellingVariants:
    def test_single_r_occurence(self):
        result = parse_assembly(ASSEMBLY_TWO_PARTS)
        assert result["is_assembly"] is True

    def test_double_r_occurrence(self):
        result = parse_assembly(ASSEMBLY_DOUBLE_R_NAUO)
        assert result["is_assembly"] is True
        assert result["component_count"] == 3


class TestOccurrenceQuantities:
    def test_duplicate_children_are_aggregated(self):
        comps = parse_assembly(DUPLICATE_CHILD_ASSEMBLY)["components"]
        p002 = next(c for c in comps if c["part_number"] == "P002")
        assert p002["qty"] == 4
        assert len([c for c in comps if c["part_number"] == "P002"]) == 1


class TestStepNameCleanup:
    def test_as_machined_suffix_removed(self):
        text = SINGLE_PART.replace("PRODUCT('P001'", "PRODUCT('202609_Default<As Machined>'")
        comp = parse_assembly(text)["components"][0]
        assert comp["part_number"] == "202609"

    def test_filename_fallback_for_useless_single_part_name(self):
        text = SINGLE_PART.replace("PRODUCT('P001'", "PRODUCT('Default<As Machined>'")
        comp = parse_assembly(text, filename="WAG bracket.step")["components"][0]
        assert comp["part_number"] == "WAG bracket"
        assert comp["description"] == "A simple bracket"

    def test_multi_body_warning_without_extra_components(self):
        text = SINGLE_PART + "\n#900=MANIFOLD_SOLID_BREP('',#901);\n#902=MANIFOLD_SOLID_BREP('',#903);\n"
        result = parse_assembly(text)
        assert result["component_count"] == 1
        assert "Multi-body STEP detected: 2 bodies" in result["warnings"][0]

    def test_assembly_root_can_fallback_to_filename_part_number(self):
        text = ASSEMBLY_TWO_PARTS.replace(
            "PRODUCT('ASSY001','Main Assembly','',(#11));",
            "PRODUCT('Main Assembly Bracket','Main Assembly Bracket','',(#11));",
        )
        comps = parse_assembly(text, filename="818242 - FG.stp")["components"]
        root = next(c for c in comps if c["level"] == 0)
        assert root["part_number"] == "818242 - FG"


class TestEmptyFile:
    def test_no_product_entities(self):
        result = parse_assembly("ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;")
        assert result["is_assembly"] is False
        assert result["component_count"] == 0
        assert result["components"] == []


class TestClassifyComponent:
    def test_sheet_metal(self):
        features = {"thickness_mm": 2.0, "bend_count": 2, "bbox_mm": [200.0, 100.0, 2.0]}
        assert classify_component(features) == "sheet_metal"

    def test_thick_part_other(self):
        features = {"thickness_mm": 20.0, "bend_count": 0, "bbox_mm": [50.0, 40.0, 20.0]}
        assert classify_component(features) == "other"

    def test_no_thickness_other(self):
        features = {"thickness_mm": 0.0, "bend_count": 0, "bbox_mm": [100.0, 80.0, 0.0]}
        assert classify_component(features) == "other"

    def test_borderline_ratio(self):
        # ratio exactly 5.0 → not > 5, so "other"
        features = {"thickness_mm": 2.0, "bend_count": 0, "bbox_mm": [10.0, 8.0, 2.0]}
        assert classify_component(features) == "other"

    def test_ratio_above_5(self):
        features = {"thickness_mm": 2.0, "bend_count": 0, "bbox_mm": [11.0, 8.0, 2.0]}
        assert classify_component(features) == "sheet_metal"


class TestPurchaseKeywords:
    def test_requested_purchase_keywords(self):
        assert _is_purchase_part("M10 bolt")
        assert _is_purchase_part("washer")

    def test_spacer_and_70_prefix_are_not_purchase(self):
        assert not _is_purchase_part("702162")
        assert not _is_purchase_part("spacer")
