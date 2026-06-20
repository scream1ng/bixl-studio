# BIXL Studio

Manufacturing documentation platform for IXL Group. First module: an **SOP generator** that turns shop-floor photos and short notes into a formatted Word document built from IXL's existing template.

Capture on a phone, build in a web app, generate a `.docx` — without ever losing track of which note belongs to which photo.

> **For Claude Code:** read `CLAUDE.md` first. It is the build spec — stack, template structure, API surface, brand, build order, and status.

---

## What's in this repo right now

| File | What it is |
|------|------------|
| `CLAUDE.md` | The build specification Claude Code reads automatically |
| `fill_sop.py` | **Working** document engine — fills the IXL template with images + text |
| `SOP_Template_A3.docx` | The real IXL SOP template (A3 landscape). Read-only source of truth |
| `requirements.txt` | Python dependencies |
| `Procfile` / `railway.json` | Railway deploy config |
| `.gitignore` | Standard Python ignores (keeps the template tracked) |

The Flask API (`app.py`), database, and frontend are **not built yet** — that's Phase 1–3 in `CLAUDE.md`.

---

## The document engine (already works)

`fill_sop.py` exposes one entry point:

```python
from fill_sop import fill_sop

fill_sop(
    steps=[{"image": "photo1.jpg", "text": "Place component on fixture"}, ...],
    part_no="36611",
    part_name="Tastic Luminate Heat Module",
    doc_no="A0866",
    template_path="SOP_Template_A3.docx",
    output_path="out.docx",          # omit to get bytes back
)
```

It handles:
- Images placed in the correct step cells, scaled to fit (any aspect ratio), vertically centred
- Description text in the matching cells
- Header part number/name and footer doc number (even though Word stores them split across runs)
- 1–8 steps on one page, unused "STEP N" headings blanked
- More than 8 steps → extra pages (STEP 9, 10, …) with header/footer repeating automatically

Run it directly to produce a demo document:

```bash
pip install -r requirements.txt
python fill_sop.py
```

---

## ICL smart dimensioning

The Inspection Check List module measures dimensions off an uploaded STEP model. Picking follows a **smart-dimension** model (like SolidWorks): the geometry you select decides the dimension type — there are no separate tool buttons.

| You pick | You get | Picks |
|---|---|---|
| 1 cylinder face / hole | **Ø** diameter | single click |
| 1 bend cylinder | **R** radius (and bend angle from sweep) | single click |
| 2 planar faces, parallel | **distance** (perpendicular gap) | two clicks |
| 2 planar faces, non-parallel | **angle** (between face normals) | two clicks |
| 2 edges, or face + edge | **distance** | two clicks |
| cylinder/hole + planar face | **centre-to-surface** distance (from the hole axis, not the rim) | two clicks |

Rules:
- **Single-entity dimensions** (Ø, R) commit on the first click — the entity alone defines them.
- **Two-entity dimensions** (distance, angle) need a second pick.
- The instruction strip previews what the current hover will produce.
- Dimensions are drawn **outside the part** in engineering-drawing style: the dimension line is offset along the nearest world axis past the silhouette, with witness (extension) lines and arrowheads.
- Parallel dimensions sharing an axis **stack**: shortest sits closest to the part, each longer one steps further out so they don't overlap.
- Dimensions read square in the **Front / Right / Top** ortho views; the Iso view is for orbiting/context only.

---

## Build order (see CLAUDE.md for detail)

1. **Flask API + DB + engine** on Railway — create an SOP, post steps, download a correct `.docx`
2. **Web app editor** — document list + two-panel SOP editor + download
3. **Phone capture PWA** — photo + annotation + note, installable to home screen

---

## Deploy to Railway

1. Push this repo to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo** → pick this repo.
3. Add the **Postgres** plugin (Railway injects `DATABASE_URL`).
4. Railway reads `railway.json` / `Procfile` and starts `gunicorn app:app`.
5. Health check is `/healthz` (add it when `app.py` is built).

No ports are hardcoded — the app must bind to `$PORT`, which Railway provides.

---

## Brand

IXL palette: red `#CC0000`, black `#1A1A1A`, grey `#6B6B6B`, white. Logo lockup is `IXL | <module>` with a red divider bar. Full tokens in `CLAUDE.md`.
