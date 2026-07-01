# BIXL Studio

Manufacturing documentation platform for IXL Group — a single hosted Flask app (Railway) with several shop-floor modules, gated by a shared PIN. The **SOP generator** was the first module and most of the doc detail below is about it; the app has since grown several more (see table). Responsive: desktop for building, phone for capture/viewing.

The core problem the SOP module solves: today, photos are taken on a phone and notes are written separately, so back at the PC nobody can tell which note belongs to which photo. BIXL Studio keeps photo + annotation + note together as one "step" from capture to final document.

### Modules (current)

| Module | What it does | Status |
|---|---|---|
| **Topics** | Team discussion channels, each split into Slack-style **Chats** (threads) — text/photo posts per chat, "Send to ▾" a chat's messages onto a Task. | live |
| **Tasks** | Kanban board fed from Topics — send messages onto To do / In progress / Done cards, drag to move, archive history. Cards show a sequential `TSK-nnn` id. | live |
| **Look Up** | FG ↔ WIP part-number finder (`Mapping` model). | live |
| **SOP** | Photos + notes → formatted IXL Word `.docx` (1–8 steps/page, multi-page). | live |
| **Label** | Three.js STEP viewer → wireframe label JPG at exact angle; history list. | live |
| **ICL** (Inspection Checklist) | Balloon dimensions off a STEP model → real IXL inspection `.xlsx`; history. | live |
| **PFC** (Process Flow Chart) | BOM transaction export → process flow chart (SVG); history. | live |
| Incident, Action Request | Nav stubs only — greyed "Coming soon" entries in the Topics/Tasks workspace group. | soon |
| MLB | Material label batch. | soon |

ICL/Label/PFC geometry runs in a **separate `cad-service/`** (FastAPI + OpenCASCADE `ocp`, own Docker deploy). The web app proxies it via `CAD_API_URL`. See README.md for per-module detail and the cad-service OCC gotcha below.

---

## Product shape

Three pieces, one hosted app on Railway:

```
Phone PWA  ──POST steps──►  Flask API  ──fill_sop──►  .docx
(capture)                   (Railway)                  (download)
Web app    ──list / edit / generate──►  same Flask API
(build)
```

- **Phone PWA** — capture: take photo, draw arrow/circle/label annotations, type a note, one step at a time (up to 8).
- **Web app** — build: two-panel editor (document list on the left, SOP preview/editor on the right), edit text, reorder, set fields, generate and download the `.docx`.
- **Flask API** — fills the real IXL Word template via `python-docx` and returns the file.

The phone PWA and web app are the **same deployment**, responsive — phone screen shows the capture flow, desktop shows the editor. Word is only a viewer/printer at the end; all formatting is done by the API.

---

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Hosting | Railway | Single service, always-on |
| Backend | Python 3.11+, Flask | Wraps `fill_sop.py` |
| DOCX engine | `python-docx`, `Pillow` | Already proven — see below |
| Database | Railway Postgres | SQLite acceptable for first cut |
| Frontend | Single-file HTML + vanilla JS (or React if it grows) | Served by Flask, installable as PWA |
| Output | `.docx` | Filled from `SOP_Template_A3.docx` |

Keep dependencies minimal. Do not introduce a JS build step unless the frontend genuinely outgrows single-file HTML.

---

## The DOCX fill engine (already working — do not rewrite from scratch)

`fill_sop.py` is the proven core. It opens IXL's real template and injects images + text into the correct cells while preserving the company border, logo, PPE icons, header and footer. Key facts the implementation must respect:

### Template structure
`SOP_Template_A3.docx` is **A3 landscape**. The body is a single table with **6 rows**:

| Row index | Contents |
|---|---|
| 0 | STEP 1–4 headings (static) |
| 1 | image cells for steps 1–4 ← inject images here |
| 2 | text cells for steps 1–4 ← inject descriptions here |
| 3 | STEP 5–8 headings (static) |
| 4 | image cells for steps 5–8 ← inject images here |
| 5 | text cells for steps 5–8 ← inject descriptions here |

Each row has 4 columns (one per step). So step N maps to:
- image cell = `table.rows[1 or 4].cells[col]`
- text cell  = `table.rows[2 or 5].cells[col]`
- where the first block is rows 1/2, second block is rows 4/5, `col = 0..3`.

### Image insertion rules (learned the hard way)
- **Scale by the tighter of width OR height**, not width alone. The image rows are a fixed height (~5.84cm). An image scaled to full width overflows the row and pushes content to a second page. Compute `scale = min(max_width_cm / w, max_height_cm / h)` and size by that. Use `max_height_cm ≈ 5.5`.
- This must work for **any aspect ratio** — phone photos are often portrait.
- **Vertically centre** the image in its cell by writing `<w:vAlign w:val="center"/>` into the cell's `<w:tcPr>`. python-docx has no direct API for this; set it via the cell's `_tc.get_or_add_tcPr()`.
- Horizontally centre the paragraph (`WD_ALIGN_PARAGRAPH.CENTER`).

### Text insertion rules
- Clear the placeholder paragraph, write one run, font **Century Gothic**, ~11pt to match the template.

### Header / footer fields
These are NOT in the document body — they live in the header and footer parts, and the values are **split across multiple runs** (the classic Word problem that breaks naive find-and-replace):
- `36611 - Tastic Luminate Heat Module` (part no + name) is in the **header table, row 1, cell 2**, split across ~5 runs.
- `A0866` (doc number) is in the **footer table, row 0, cell 0**, split across 3 runs (`A0` + `8` + `66`).
- Fix by collecting all runs in the target paragraph, copying the first run's `<w:rPr>` formatting, removing every run, then writing a single clean run with the new text.
- Footer **Revision Date** is a Word `DATE` field — auto-updates, leave alone.
- Footer **Authorised by** is a `LASTSAVEDBY` field — pulls from whoever saved the file. Only override if explicitly required.

### Validation
After generating, validate the `.docx` before returning it. Catch the "image overflows to page 2" regression specifically — a single-page SOP (≤8 steps) must stay one page.

### Variable step count and multi-page

An SOP can have any number of steps. The template page holds **8 steps** (two blocks of four). Handle counts as follows:

**1–8 steps (single page)**
- Fill the cells you have (image + text per step).
- Leave unused image/text cells **blank** — do not delete rows or cells.
- **Remove the "STEP N" heading text** from every empty step so blank cells don't look like unfinished placeholders. A cell is "empty" if its step has neither image nor text.
- Headings live in **row 0** (STEP 1–4) and **row 3** (STEP 5–8). Each heading is a single run holding `STEP N`. To clear it, blank that run's text (keep the cell and its formatting; just empty the string).
- Example: a 6-step SOP keeps STEP 1–6 headings, blanks STEP 7 and STEP 8 headings, and leaves cells 7–8 empty.

**More than 8 steps (multi-page)**
- Steps 1–8 go on page 1 as normal.
- For each additional block of 8, **duplicate the entire 6-row table** onto a **new page** (insert a page break before it) and continue numbering: page 2 is STEP 9–16, page 3 is STEP 17–24, etc.
- Re-run the same fill logic on the duplicated table with a step-index offset (page 2 → offset 8, page 3 → offset 16). Update its headings to STEP 9–16 (etc.).
- On the **last** page, apply the same empty-heading rule to any trailing unused cells (e.g. 11 steps → page 2 shows STEP 9–11, blanks STEP 12–16 headings).
- **Header and footer repeat automatically.** They are defined at the section level, so every page inherits the logo, PPE icons, "STANDARD OPERATING PROCEDURE", part name, and footer. The footer's `PAGE` / `NUMPAGES` fields update on their own ("Page 1 of 2", "Page 2 of 2") — do not hand-build page numbers. Only the **table** needs duplicating, not the header/footer.
- When duplicating the table, deep-copy the table element (with its borders, column widths, row heights, and cell styling intact) so page 2 is visually identical to page 1.

Implementation note: expose this as e.g. `fill_sop(steps, part_no, part_name, doc_no)` where `steps` is a list of any length; the function decides single vs. multi-page internally and applies the empty-heading rule per page.

---

## Data model

One record per SOP:

```
SOP {
  id           uuid
  part_no      text     # e.g. "36611"
  part_name    text     # e.g. "Tastic Luminate Heat Module"
  doc_no       text     # e.g. "A0866"
  status       text     # "draft" | "done"
  created_at   timestamp
  updated_at   timestamp
  steps        Step[]   # up to 8, ordered
}

Step {
  index        int      # 0-based, any number; 8 steps per page
  image        bytes/base64   # the photo with annotations baked in OR raw + annotation list
  text         text     # the description
  annotations  json     # optional: [{type:"arrow"|"circle"|"label", coords as 0..1 fractions, label?}]
}
```

Annotations are stored as **fractional coordinates (0.0–1.0)** relative to image size so they scale correctly. Decision still open (see Open questions): bake annotations into the JPG on capture vs. re-render as native Word shapes on generate. Default to **baked-in** unless asked to do native shapes.

---

## API surface (Flask)

```
GET    /api/sops               list SOPs (grouped by date on the client)
POST   /api/sops               create a draft
GET    /api/sops/:id           fetch one SOP with steps
PUT    /api/sops/:id           update fields / steps / order
DELETE /api/sops/:id           delete
POST   /api/sops/:id/generate  run fill_sop → return .docx (attachment)
GET    /healthz                Railway health check
```

`generate` payload assembles the `SOP` record into the `fill_sop` inputs (8 image+text pairs, plus part_no, part_name, doc_no) and streams back the `.docx`.

---

## Frontend

### Screens
1. **Home** — IXL | Studio logo, module selector (SOP active; ICL / PFC / MLB shown but disabled / "Soon"), recent documents, "New SOP".
2. **Capture** (phone) — step dots for the current page of steps, photo area with annotation tools (arrow / circle / label / undo), note field, back / next. Steps are not capped at 8 in the data — the document layout paginates at 8 per page, but a user can keep adding steps.
3. **Editor** (desktop) — two panels: left = document list grouped by Today / This week with status dots; right = live SOP preview matching the template layout (header, 8 step cells, footer) with inline-editable fields, click a cell to replace photo or edit text. Top bar has module tabs and a red "Download .docx" button.

### Brand (strict — match IXL logo)
| Token | Value |
|---|---|
| Red | `#CC0000` |
| Black | `#1A1A1A` |
| Dark grey | `#2C2C2C` |
| Grey | `#6B6B6B` |
| Light grey | `#F2F2F2` |
| Page background | `#F5F4F1` |
| Recessed panel background | `#E7E5E0` |
| White | `#FFFFFF` |

- Logo lockup: **IXL** bold + red vertical divider bar + module name in regular weight (e.g. `IXL | Studio`, `IXL | SOP`, `IXL | ICL`). "since 1858" sits under the IXL mark.
- Black headers/top bar with a **red bottom accent line**. Red for primary/active actions and the current step. Black for secondary primary buttons. Grey for muted/secondary. White surfaces, light-grey page background.
- Sentence case, no ALL CAPS except short labels. Minimal borders, no gradients/shadows.

The web editor does **not** need to be a pixel-perfect replica of the Word output — it is the editor, Word/the generated `.docx` is the formatted result. Set that expectation in the UI; don't waste effort trying to make HTML and python-docx render identically.

---

## Build order

Build and verify in this order. Each phase should be runnable before moving on.

1. **Flask API + DB + fill engine.** Wire `fill_sop.py` behind `POST /api/sops/:id/generate`. Get to: create a SOP via API, post steps with images, download a correct one-page `.docx`. Deploy to Railway, confirm `/healthz`.
2. **Web app editor.** Document list + two-panel SOP editor + working Download button against the live API.
3. **Phone capture PWA.** Photo + annotation + note flow, installable to home screen, posts steps to the API. Make the same deployment installable as a PWA (manifest + service worker).

Phases 1–2 alone give a working desktop tool. Phase 3 adds mobile capture.

---

## Railway specifics

- Single web service. Expose Flask on `$PORT` (Railway injects it) — do not hardcode a port.
- `Procfile` (or `railway.json` start command): `gunicorn app:app` (use gunicorn, not the Flask dev server, in production).
- `requirements.txt` pins: `flask`, `gunicorn`, `python-docx`, `Pillow`, `psycopg2-binary` (if Postgres).
- Put the template at a known path (e.g. `app/templates_docx/SOP_Template_A3.docx`) and read-copy it per request — never mutate the original on disk.
- `DATABASE_URL` comes from Railway's Postgres plugin as an env var.
- Generated files are transient: stream the `.docx` in the response, or write to `/tmp` and delete after sending. Do not accumulate files on the service disk.

### cad-service OCC gotcha (learned the hard way)

The CAD service (`cad-service/`) runs OpenCASCADE via the `ocp` conda package in Docker. **Railway's `ocp` build can be older than your local one**, and the two bindings name static methods differently: newer exposes `TopoDS.Face_s(x)` / `BRep_Tool.Triangulation_s(...)`, older exposes them **without the `_s` suffix** (`TopoDS.Face(x)`). Code that works locally then 500s on Railway with `AttributeError: ... has no attribute 'Face_s'`.

- Never call OCC statics directly by the `_s` name. Resolve per-binding — see the `_static()` / `_face()` / `_map_shapes()` helpers in `cad/icl.py` and `as_edge()` in `cad/loader.py`.
- Same for version-shifted APIs: `Poly_Triangulation.Node(i)` (newer) vs `.Nodes().Value(i)` (older); prefer the 4-arg `BRepMesh_IncrementalMesh(shape, lin, rel, ang)` (the 5-arg parallel overload is missing on some builds).
- The Docker conda layer is cached by build hash; changing the Dockerfile text does **not** reliably re-resolve `ocp`. Don't rely on a cache-bust to "get a newer version" — write binding-agnostic code instead.
- Endpoints swallow exceptions into `HTTPException(500, ...)`; to debug Railway, `traceback.print_exc()` in the handler and surface the response `detail` through the Flask proxy (`modules/icl`), then read `railway logs -p <cad-project-id> -e production -d`.

---

## Conventions

- Python: type hints, small functions, no premature abstraction. Keep the DOCX logic in one module (`fill_sop.py`) with clear `add_image_to_cell` / `add_text_to_cell` / `set_part_title` / `set_doc_number` helpers.
- Never edit the original template file in place — always copy to a working buffer/path first.
- Every generate path must validate the output is **one page** before returning.
- Secrets via Railway env vars only — nothing committed.
- Commit messages: short imperative ("add generate endpoint", "fix image overflow on portrait photos").

---

## Status

- [x] `modules/sop/fill.py` — dynamic fill engine, 12 pre-built templates (A3/A4 × landscape/portrait × 1/2/4/6/8 steps)
- [x] Image scaling constrained by height — output stays one page, any aspect ratio
- [x] Vertical centre of images in cells (`w:vAlign`)
- [x] Header part no/name + footer doc no — split-run replacement working
- [x] Flask API + DB (SQLite default, Postgres via `DATABASE_URL`)
- [x] Railway deploy (Procfile, requirements.txt, gunicorn, `/healthz`)
- [x] Web app editor — document list, SOP editor, format/steps-per-page picker, Download .docx
- [x] Phone capture PWA — installable, capture flow (photo + annotation + note), step reorder, manifest + service worker
- [x] Annotation strategy — baked-in (arrows/circles drawn on canvas, exported as JPEG)
- [x] Label module — Three.js STEP viewer, wireframe JPG export (canvas render, exact angle match)
- [x] Label three-column editor UI — preview panel + settings panel + recent labels sidebar
- [x] Label history page (`screen-label-list`) — grouped list (Today/This week/Earlier), thumbnail, delete, same format as SOP home
- [x] Label mobile layout — 44vh preview cap, settings panel full-width below, Browse file + greyed Take Photo buttons, mobile header with hamburger
- [x] Auth — shared PIN gate, 8h session cap, deny-by-default `/api/*`, hardened session cookies
- [x] Topics module — channels + text/photo messages (edit/delete, multi-photo, lightbox)
- [x] Look Up module — FG ↔ WIP part-number mapping + lookup
- [x] Tasks module — Kanban board fed from Topics (live message refs), drag between columns, card detail (editable priority/due, status switch), send-from-topic modal, archive/restore/delete
- [x] Tags — GitHub-style labels on tasks (`Tag` model + `tasks.tag_ids` JSON refs), seeded preset (Quality/Safety/Improvement/Document), editable manager (create/rename/recolor/delete), board tag-filter
- [x] ICL module — STEP balloon dimensioning, smart dimension type, `.xlsx` export, history (cad-service backed)
- [x] PFC module — BOM transaction parse → SVG flow chart, `.xlsx` export, history
- [x] cad-service — FastAPI + OpenCASCADE STEP mesh/measure, separate Docker deploy, binding-agnostic OCC
- [x] Chats — `Topic → Chats → Messages` (`Chat` model), chat-scoped messages API (hard cutover from channel-scoped), topic-home screen (search/filter/archive), expandable topic-tree, "Send to ▾" dropdown (Task wired, Incident/Action Request disabled stubs), Tasks board `TSK-nnn` display id + chat provenance, nav rail split into WORKSPACE/TOOLS groups
- [ ] Empty-cell heading removal (blank "STEP N" for unused steps, ≤8)
- [ ] Multi-page support (>8 steps → duplicate table on new page, STEP 9+)
- [ ] Label: Take Photo mode (mobile capture → label, currently greyed out)

---

## Open questions (decide before or during the relevant phase)

1. **Annotations**: bake arrows/circles into the photo on capture (simple, not editable in Word) vs. store as data and render native Word shapes on generate (editable, more complex). Default: baked-in.
2. **Auth**: done — shared **PIN gate** (`POST /api/login`, `APP_PIN` env var) with an 8h hard session cap; `_require_pin` deny-by-default `before_request` on all `/api/*` except `/api/login` + `/api/session`. Mobile re-locks after 15 min idle (client-side). Per-user auth not yet needed.
3. **"Note-only" SOPs**: some SOPs are one photo + lots of text, no 8-cell grid. May need a second template/layout later — out of scope for phase 1.

---

## Reference assets

- `modules/sop/fill.py` — the fill engine. Entry point: `fill_sop(steps, part_no, part_name, doc_no, format_key, steps_per_page)`.
- `docx/sop/` — 12 pre-built `.docx` fill templates (source of truth for layout).
