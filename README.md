# BIXL Studio

Manufacturing documentation platform for IXL Group — a single hosted web app (Flask on Railway) with several shop-floor modules. Responsive: desktop for building, phone for capture/viewing.

> **For Claude Code:** read `CLAUDE.md` first — it is the build spec (stack, template structure, API surface, brand, conventions, gotchas).

---

## Modules

| Module | What it does | Status |
|--------|--------------|--------|
| **Topics** | Team discussion channels — text/photo posts per thread. | ✅ |
| **Tasks** | Kanban board fed from Topics — send messages onto To do / In progress / Done cards, drag to move, tags + filter, comments, archive history. | ✅ |
| **Look Up** | FG ↔ WIP part-number finder. | ✅ |
| **SOP** | Turn shop-floor photos + notes into a formatted IXL Word `.docx` (1–8 steps/page, multi-page). | ✅ |
| **Label** | Three.js STEP viewer → wireframe label JPG at exact angle; history list. | ✅ |
| **ICL** (Inspection Checklist) | Balloon dimensions off a STEP model → export the real IXL inspection `.xlsx`; saved history. | ✅ |
| **PFC** (Process Flow Chart) | BOM transaction export → process flow chart (SVG renderer); saved history. | ✅ |
| **EXP** (Expiry) | BOM Product Detail Report → expiry runway timeline; saved history. | ✅ |
| MLB | Material label batch. | Soon |

Access is gated by a shared **PIN** (`POST /api/login`) with an 8-hour hard session cap; mobile re-locks after 15 min idle.

---

## Repo layout

| Path | What it is |
|------|------------|
| `app.py` | Flask API + DB models (SQLite default, Postgres via `DATABASE_URL`) + routes |
| `modules/sop/fill.py` | SOP `.docx` fill engine — `fill_sop(...)` |
| `modules/label/` | STEP → wireframe / mesh helpers |
| `modules/icl/` | cad-service proxy (`__init__.py`) + Excel export (`export.py`) |
| `modules/pfc/` | BOM transaction parse (`parse.py`) → SVG flow chart (`svg.py`) + export (`export.py`) |
| `docx/sop/` | 12 pre-built `.docx` fill templates (A3/A4 × landscape/portrait × step counts) |
| `docx/icl/Template - ICL.xlsx` | Blank IXL inspection-checklist template (filled per export) |
| `templates/index.html` | Single-file responsive frontend (all screens + PWA) |
| `cad-service/` | FastAPI + OpenCASCADE (`ocp`) service for STEP meshing/measuring (own Docker deploy) |
| `Procfile` / `railway.json` | Railway deploy config (`gunicorn app:app`) |

---

## SOP document engine

`modules/sop/fill.py`:

```python
from modules.sop.fill import fill_sop

docx_bytes = fill_sop(
    steps=[{"image": b"...jpg bytes...", "text": "Place component on fixture"}, ...],
    part_no="36611",
    part_name="Tastic Luminate Heat Module",
    doc_no="A0866",
    format_key="a3-landscape",
    steps_per_page=8,
)
```

Handles: images scaled to fit (any aspect, vertically centred), description text, header part no/name + footer doc no (split-run safe), blanked headings for unused steps, multi-page for >8 steps with header/footer repeating.

---

## ICL — ballooned inspection checklist

Upload a STEP model, then **click surfaces/edges to drop numbered balloons** and build an inspection sheet.

**Smart dimensioning** (geometry decides the type, no tool buttons):

| You pick | You get |
|---|---|
| 1 cylinder face / hole | **Ø** diameter (single click) |
| 1 bend cylinder | **R** radius / bend angle |
| 2 parallel planar faces | **distance** (perpendicular gap) |
| 2 non-parallel faces | **angle** |
| 2 edges, or face + edge | **distance** |
| cylinder/hole + face | **centre-to-surface** distance |

**Workflow:**
1. Each pick adds a numbered balloon + a row to the dimension table. Numbers run sequentially and **carry across screenshots** (never reset); deleting a row re-sequences.
2. Frame the model in the square viewfinder, **Capture** — the balloons bake into the picture (thick lines, white bg) and clear from the viewport.
3. Up to **4 pictures** per sheet (1 ISO overview + measurement views).
4. **Export ICL** fills `docx/icl/Template - ICL.xlsx` (header, NO/balloon column, dimension + tolerance per row, pictures placed by count) and saves the record to history.

Gauges: Vernier / Protractor / Visual (Visual takes an SOP reference → LIMITS column).

**History** (`screen-icl-list`): grouped Today / This week / Earlier, thumbnail, re-download `.xlsx`, delete. On a phone, opening a checklist shows the ballooned pictures + a "what to check" list (no download) — read-only for the operator. The 3D editor is desktop-only.

---

## API surface (selected)

```
POST /api/login | /logout                     PIN auth (8-hour session cap)
GET  /api/session                             current auth state
POST /api/icl/mesh | /edges | /measure      cad-service proxy (STEP geometry)
POST /api/icl/export                          fill template → .xlsx (+ save history)
GET  /api/icls                                list saved checklists
GET  /api/icls/:id  /thumb  /image/:n         fetch record / thumbnail / screenshot
GET  /api/icls/:id/export                     re-generate .xlsx from a saved record
DELETE /api/icls/:id
GET/POST/PUT/DELETE /api/sops ...             SOP CRUD + /generate
GET/POST/DELETE /api/labels ...               Label history
POST /api/pfc/parse                           BOM transaction export → flow chart
GET/POST/DELETE /api/pfcs ... /thumb /export  PFC history + re-export
GET/POST/DELETE /api/channels ...             Topics channels + messages
GET/POST/PATCH/DELETE /api/tasks ...          Tasks board (status/priority/due/tags/add-messages/archive)
POST /api/tasks/:id/comments                  task comments (+ PATCH/DELETE /api/comments/:id)
GET/POST/PATCH/DELETE /api/tags ...           tags/labels (delete scrubs from tasks)
GET/POST/PUT/DELETE /api/mappings ...         Look Up FG↔WIP mappings + /api/lookup
GET  /healthz                                 Railway health check
```

---

## Deploy to Railway

1. Push to GitHub → Railway **New Project → Deploy from GitHub repo**.
2. Add the **Postgres** plugin (injects `DATABASE_URL`).
3. Railway reads `railway.json` / `Procfile` → runs `gunicorn app:app`. Health check `/healthz`.
4. The **cad-service** (`cad-service/`) deploys separately (its own Dockerfile, OpenCASCADE). Set `CAD_API_URL` on the web service to point at it.

No ports hardcoded — the app binds `$PORT`. See `CLAUDE.md` for the cad-service OCC binding gotcha.

---

## Brand

IXL palette: red `#CC0000`, black `#1A1A1A`, grey `#6B6B6B`, light grey `#F2F2F2`, white. Logo lockup `IXL | <module>` with a red divider bar. Full tokens in `CLAUDE.md`.
