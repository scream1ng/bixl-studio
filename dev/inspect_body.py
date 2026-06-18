"""Dump full body composition of blank templates to see what survives after strip."""
import os
from docx import Document
from docx.oxml.ns import qn

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(HERE, "..", "docx", "sop", "source")

TARGETS = [
    "SOP Template (A3 Landscape).docx",
    "SOP Template (A3 Portrait).docx",
    "SOP Template (A4 Landscape).docx",
    "SOP Template (A4 Portrait).docx",
]

def has_drawing(el):
    return el.find('.//' + qn('w:drawing')) is not None

def has_image(el):
    try:
        return el.find('.//' + qn('a:blip')) is not None
    except Exception:
        return False

def body_dump(fname):
    path = os.path.join(TEMPLATE_DIR, fname)
    doc = Document(path)
    body = doc.element.body
    print(f"\n{'='*60}")
    print(f"BODY: {fname}")
    print(f"{'='*60}")
    for i, child in enumerate(body):
        tag = child.tag.split("}")[-1]
        text = child.text_content() if hasattr(child, 'text_content') else ""
        # get all text
        texts = [r.text or "" for r in child.iter() if r.tag.endswith("}t")]
        text_str = "".join(texts).strip()[:80]
        drawing = has_drawing(child)
        img = has_image(child)
        n_children = len(list(child))
        print(f"  [{i:02d}] <{tag}> children={n_children} drawing={drawing} img={img} text={repr(text_str)}")
        if tag == "tbl":
            rows = child.findall(qn("w:tr"))
            for ri, row in enumerate(rows):
                cells = row.findall(qn("w:tc"))
                for ci, cell in enumerate(cells):
                    ctexts = "".join(r.text or "" for r in cell.iter() if r.tag.endswith("}t")).strip()[:40]
                    cdraw = has_drawing(cell)
                    print(f"       row{ri} col{ci}: drawing={cdraw} text={repr(ctexts)}")

    print(f"\n  HEADER:")
    try:
        hdr = doc.sections[0].header
        hbody = hdr._element
        for i, child in enumerate(hbody):
            tag = child.tag.split("}")[-1]
            texts = [r.text or "" for r in child.iter() if r.tag.endswith("}t")]
            text_str = "".join(texts).strip()[:80]
            drawing = has_drawing(child)
            img = has_image(child)
            print(f"    [{i:02d}] <{tag}> drawing={drawing} img={img} text={repr(text_str)}")
    except Exception as e:
        print(f"    (error: {e})")

    print(f"\n  FOOTER:")
    try:
        ftr = doc.sections[0].footer
        fbody = ftr._element
        for i, child in enumerate(fbody):
            tag = child.tag.split("}")[-1]
            texts = [r.text or "" for r in child.iter() if r.tag.endswith("}t")]
            text_str = "".join(texts).strip()[:80]
            drawing = has_drawing(child)
            img = has_image(child)
            print(f"    [{i:02d}] <{tag}> drawing={drawing} img={img} text={repr(text_str)}")
    except Exception as e:
        print(f"    (error: {e})")

for fname in TARGETS:
    body_dump(fname)
