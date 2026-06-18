import re
from typing import Dict, List, Optional, Tuple

_PURCHASE_KEYWORDS = {
    'STUD', 'BOLT', 'NUT', 'WASHER', 'SCREW', 'RIVET',
}

# Compiled once — strip NX internal prefix (e.g. 'WAG-109459_0001_1-') and
# trailing variant tag (e.g. ' _DJ MCA MY26 WARLOCK_').
_NX_PREFIX = re.compile(r'^.+?_\d+_\d+-')
_NX_SUFFIX = re.compile(r'\s+_[^_]+_\s*$')
_AS_MACHINED_SUFFIX = re.compile(r'(?:_?Default)?\s*<As Machined>\s*$', re.IGNORECASE)
_STEP_EXT = re.compile(r'\.(?:step|stp)$', re.IGNORECASE)
_USELESS_PRODUCT_NAMES = {'', 'DEFAULT', 'MODEL', 'PART', 'PRODUCT', 'BODY', 'SOLID'}


def _clean_nx_name(name: str) -> str:
    s = _NX_PREFIX.sub('', name, count=1).strip()
    if not s:
        return name
    s = _AS_MACHINED_SUFFIX.sub('', s).strip()
    s = _NX_SUFFIX.sub('', s).strip()
    return s


def _is_purchase_part(name: str) -> bool:
    upper = name.upper()
    return any(kw in upper for kw in _PURCHASE_KEYWORDS)


def _filename_part_number(filename: Optional[str]) -> str:
    if not filename:
        return ""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    base = _STEP_EXT.sub('', base).strip()
    return _clean_nx_name(base)


def _is_useful_product_name(name: str) -> bool:
    cleaned = _clean_nx_name(name).strip()
    if not cleaned:
        return False
    return cleaned.upper() not in _USELESS_PRODUCT_NAMES


def _looks_like_part_number(name: str) -> bool:
    cleaned = _clean_nx_name(name).strip()
    if not cleaned:
        return False
    compact = cleaned.replace(" ", "")
    if not any(ch.isdigit() for ch in compact):
        return False
    return bool(re.fullmatch(r"[A-Z0-9._-]+", compact, re.IGNORECASE))


def _body_count(text: str) -> int:
    breps = len(re.findall(r"\bMANIFOLD_SOLID_BREP\s*\(", text, re.IGNORECASE))
    shells = len(re.findall(r"\bCLOSED_SHELL\s*\(", text, re.IGNORECASE))
    return breps or shells


def _parse_products(text: str) -> Dict[str, dict]:
    products = {}
    for m in re.finditer(
        r"#(\d+)\s*=\s*PRODUCT\s*\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'",
        text, re.IGNORECASE,
    ):
        eid, pid, name, desc = m.group(1), m.group(2), m.group(3), m.group(4)
        products[eid] = {"part_id": pid, "name": name, "description": desc}
    return products


def _parse_pdf(text: str) -> Dict[str, str]:
    """PRODUCT_DEFINITION_FORMATION #id -> PRODUCT #id"""
    pdf_to_product: Dict[str, str] = {}
    for m in re.finditer(
        r"#(\d+)\s*=\s*PRODUCT_DEFINITION_FORMATION[^(]*\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*#(\d+)",
        text, re.IGNORECASE,
    ):
        pdf_to_product[m.group(1)] = m.group(2)
    return pdf_to_product


def _parse_pd(text: str) -> Dict[str, str]:
    """PRODUCT_DEFINITION #id -> PRODUCT_DEFINITION_FORMATION #id"""
    pd_to_pdf: Dict[str, str] = {}
    for m in re.finditer(
        r"#(\d+)\s*=\s*PRODUCT_DEFINITION\s*\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*#(\d+)",
        text, re.IGNORECASE,
    ):
        pd_to_pdf[m.group(1)] = m.group(2)
    return pd_to_pdf


def _parse_nauo(text: str) -> List[Tuple[str, str]]:
    """Return (parent_pd_id, child_pd_id) pairs from NEXT_ASSEMBLY_USAGE_OCCUR(R)ENCE."""
    edges: List[Tuple[str, str]] = []
    for m in re.finditer(
        r"NEXT_ASSEMBLY_USAGE_OCCUR(?:R?ENCE)\s*\([^,]*,[^,]*,[^,]*,\s*#(\d+)\s*,\s*#(\d+)",
        text, re.IGNORECASE | re.DOTALL,
    ):
        edges.append((m.group(1), m.group(2)))
    return edges


def parse_assembly(text: str, filename: Optional[str] = None) -> dict:
    """
    Parse STEP product tree from raw text.

    Returns:
        {
            "is_assembly": bool,
            "component_count": int,
            "components": [
                {
                    "part_number": str,
                    "description": str,
                    "level": int,
                    "is_assembly": bool,
                },
                ...
            ]
        }
    """
    products = _parse_products(text)
    pdf_to_product = _parse_pdf(text)
    pd_to_pdf = _parse_pd(text)
    nauo_edges = _parse_nauo(text)
    body_count = _body_count(text)
    warnings: List[str] = []

    if len(products) == 1 and not nauo_edges and body_count > 1:
        warnings.append(
            f"Multi-body STEP detected: {body_count} bodies, but no assembly structure. "
            "Treating as one manufactured part. Review required."
        )

    def _resolve(pd_id: str) -> Optional[dict]:
        pdf_id = pd_to_pdf.get(pd_id)
        if pdf_id is None:
            return None
        prod_id = pdf_to_product.get(pdf_id)
        if prod_id is None:
            return None
        return products.get(prod_id)

    if not nauo_edges:
        if products:
            first = next(iter(products.values()))
            filename_part = _filename_part_number(filename)
            raw_part = first["part_id"] or first["name"]
            part_number = _clean_nx_name(raw_part)
            if filename_part and not _is_useful_product_name(part_number):
                part_number = filename_part
            description = first["description"].strip()
            if not description or not _is_useful_product_name(description):
                description = part_number
            return {
                "is_assembly": False,
                "component_count": 1,
                "warnings": warnings,
                "components": [{
                    "part_number": part_number,
                    "description": description,
                    "level": 0,
                    "is_assembly": False,
                    "qty": 1,
                }],
            }
        return {"is_assembly": False, "component_count": 0, "components": [], "warnings": warnings}

    parent_pds = {e[0] for e in nauo_edges}
    child_pds = {e[1] for e in nauo_edges}
    root_pds = parent_pds - child_pds

    children_of: Dict[str, List[str]] = {}
    for parent, child in nauo_edges:
        children_of.setdefault(parent, []).append(child)

    components: List[dict] = []

    def _component_key(pd_id: str) -> str:
        info = _resolve(pd_id) or {"part_id": "", "name": f"PD#{pd_id}", "description": ""}
        return _clean_nx_name(info["part_id"] or info["name"])

    def _walk(pd_id: str, level: int, branch: set, qty: int = 1) -> None:
        if pd_id in branch:
            return
        branch.add(pd_id)
        info = _resolve(pd_id) or {"part_id": "", "name": f"PD#{pd_id}", "description": ""}
        kid_refs: Dict[str, dict] = {}
        for child_pd in children_of.get(pd_id, []):
            key = _component_key(child_pd)
            if key not in kid_refs:
                kid_refs[key] = {"pd_id": child_pd, "qty": 0}
            kid_refs[key]["qty"] += 1
        kids = [ref["pd_id"] for ref in kid_refs.values()]
        raw_name = info["part_id"] or info["name"]
        part_number = _clean_nx_name(raw_name)
        filename_part = _filename_part_number(filename) if level == 0 else ""
        if level == 0 and filename_part and not _looks_like_part_number(part_number):
            part_number = filename_part
        components.append({
            "part_number": part_number,
            "description": info["description"],
            "level": level,
            "is_assembly": len(kids) > 0,
            "qty": qty,
        })
        for ref in kid_refs.values():
            _walk(ref["pd_id"], level + 1, set(branch), ref["qty"])

    for root in sorted(root_pds):
        _walk(root, 0, set())

    return {
        "is_assembly": True,
        "component_count": len(components),
        "components": components,
        "warnings": warnings,
    }
