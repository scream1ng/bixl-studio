"""app.py — BIXL Studio Flask API."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import uuid
from datetime import datetime

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

from modules.sop.fill import fill_sop as _fill_sop
from modules.label import step_to_wireframe, step_to_mesh_and_edges

app = Flask(__name__)

# ── Database ─────────────────────────────────────────────────────────────────
_db_url = os.environ.get("DATABASE_URL", "sqlite:///bixl.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB

db = SQLAlchemy(app)




def _sha256(data_url: str) -> str:
    return hashlib.sha256(data_url.encode("utf-8")).hexdigest()


# ── Models ────────────────────────────────────────────────────────────────────

class ImageBlob(db.Model):
    """Content-addressed image store. One row per unique image, keyed by SHA-256."""
    __tablename__ = "image_blobs"
    hash = db.Column(db.String(64), primary_key=True)
    data = db.Column(db.Text, nullable=False)


class SOP(db.Model):
    __tablename__ = "sops"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    part_no = db.Column(db.Text, nullable=False, default="")
    part_name = db.Column(db.Text, nullable=False, default="")
    doc_no = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(16), nullable=False, default="draft")
    format_key = db.Column(db.String(32), nullable=False, default="a3-landscape")
    steps_per_page = db.Column(db.Integer, nullable=False, default=8)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    steps = db.relationship(
        "Step", backref="sop", lazy=True,
        order_by="Step.index", cascade="all, delete-orphan",
    )

    def to_dict(self, include_steps: bool = False) -> dict:
        d = {
            "id": self.id,
            "part_no": self.part_no,
            "part_name": self.part_name,
            "doc_no": self.doc_no,
            "status": self.status,
            "format_key": self.format_key,
            "steps_per_page": self.steps_per_page,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if include_steps:
            d["steps"] = [s.to_dict() for s in self.steps]
        return d


class Step(db.Model):
    __tablename__ = "steps"

    id = db.Column(db.String(36), primary_key=True)  # client-supplied stable id
    sop_id = db.Column(db.String(36), db.ForeignKey("sops.id"), nullable=False)
    index = db.Column(db.Integer, nullable=False)
    image_hash = db.Column(db.String(64), db.ForeignKey("image_blobs.hash"))
    text = db.Column(db.Text, default="")
    annotations = db.Column(db.Text, default="[]")

    blob = db.relationship("ImageBlob", lazy="joined")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sop_id": self.sop_id,
            "index": self.index,
            "image": self.blob.data if self.blob else None,
            "image_hash": self.image_hash,
            "text": self.text or "",
            "annotations": json.loads(self.annotations or "[]"),
        }


class Label(db.Model):
    """One generated/exported label. Keeps only the export JPG (content-addressed)."""
    __tablename__ = "labels"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.Text, nullable=False, default="")
    part_name = db.Column(db.Text, nullable=False, default="")
    size_mm = db.Column(db.Float, nullable=False, default=26.0)
    dpi = db.Column(db.Integer, nullable=False, default=300)
    image_hash = db.Column(db.String(64), db.ForeignKey("image_blobs.hash"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    blob = db.relationship("ImageBlob", lazy="joined")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "part_name": self.part_name,
            "size_mm": self.size_mm,
            "dpi": self.dpi,
            "created_at": self.created_at.isoformat(),
        }


def _migrate():
    """Migrate steps.image → image_blobs + steps.image_hash if needed."""
    is_sqlite = db.engine.dialect.name == "sqlite"
    with db.engine.connect() as conn:
        # Detect columns on the steps table.
        if is_sqlite:
            rows = conn.execute(text("PRAGMA table_info(steps)")).fetchall()
            cols = {r[1] for r in rows}
        else:
            rows = conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'steps'"
            )).fetchall()
            cols = {r[0] for r in rows}

        if "image" in cols and "image_hash" not in cols:
            conn.execute(text("ALTER TABLE steps ADD COLUMN image_hash VARCHAR(64)"))

            # Migrate existing image blobs.
            existing = conn.execute(
                text("SELECT id, image FROM steps WHERE image IS NOT NULL")
            ).fetchall()
            for row in existing:
                img_hash = hashlib.sha256(row[1].encode("utf-8")).hexdigest()
                if is_sqlite:
                    conn.execute(text(
                        "INSERT OR IGNORE INTO image_blobs (hash, data) VALUES (:h, :d)"
                    ), {"h": img_hash, "d": row[1]})
                else:
                    conn.execute(text(
                        "INSERT INTO image_blobs (hash, data) VALUES (:h, :d) "
                        "ON CONFLICT DO NOTHING"
                    ), {"h": img_hash, "d": row[1]})
                conn.execute(
                    text("UPDATE steps SET image_hash = :h WHERE id = :id"),
                    {"h": img_hash, "id": row[0]},
                )
            conn.commit()


with app.app_context():
    db.create_all()
    _migrate()
    # Add columns introduced after initial schema
    with db.engine.connect() as _conn:
        _is_pg = db.engine.dialect.name == "postgresql"
        for _stmt in (
            "ALTER TABLE sops ADD COLUMN format_key TEXT NOT NULL DEFAULT 'a3-landscape'",
            "ALTER TABLE sops ADD COLUMN steps_per_page INTEGER NOT NULL DEFAULT 8",
        ):
            try:
                _conn.execute(text(_stmt))
                _conn.commit()
            except Exception:
                pass


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js",
                               mimetype="application/javascript")


# ── API: SOPs ─────────────────────────────────────────────────────────────────

@app.route("/api/sops", methods=["GET"])
def list_sops():
    sops = SOP.query.order_by(SOP.updated_at.desc()).all()
    return jsonify([s.to_dict() for s in sops])


@app.route("/api/sops", methods=["POST"])
def create_sop():
    data = request.get_json(silent=True) or {}
    sop = SOP(
        part_no=data.get("part_no", ""),
        part_name=data.get("part_name", ""),
        doc_no=data.get("doc_no", ""),
        format_key=data.get("format_key", "a3-landscape"),
        steps_per_page=int(data.get("steps_per_page", 8)),
    )
    db.session.add(sop)
    db.session.commit()
    return jsonify(sop.to_dict()), 201


@app.route("/api/sops/<sop_id>", methods=["GET"])
def get_sop(sop_id: str):
    sop = db.get_or_404(SOP, sop_id)
    return jsonify(sop.to_dict(include_steps=True))


@app.route("/api/sops/<sop_id>", methods=["PUT"])
def update_sop(sop_id: str):
    sop = db.get_or_404(SOP, sop_id)
    data = request.get_json(silent=True) or {}

    # Optimistic concurrency: reject saves built on a stale base.
    base = data.get("base_updated_at")
    if base is not None and base != sop.updated_at.isoformat():
        return jsonify({
            "error": "stale",
            "server": sop.to_dict(include_steps=True),
        }), 409

    for field in ("part_no", "part_name", "doc_no", "status", "format_key", "steps_per_page"):
        if field in data:
            setattr(sop, field, data[field])

    have_hashes: set[str] = set()

    if "steps" in data:
        existing = {s.id: s for s in sop.steps}
        seen_ids: set[str] = set()

        for i, s in enumerate(data["steps"]):
            sid = s.get("id") or str(uuid.uuid4())
            seen_ids.add(sid)

            img_hash = s.get("image_hash")
            img_data = s.get("image")  # full data URL, only sent when new

            if img_data:
                img_hash = _sha256(img_data)
                if not db.session.get(ImageBlob, img_hash):
                    db.session.add(ImageBlob(hash=img_hash, data=img_data))
            if img_hash:
                have_hashes.add(img_hash)

            step = existing.get(sid)
            if step is None:
                step = Step(id=sid, sop_id=sop.id)
                db.session.add(step)
            step.index = s.get("index", i)
            step.text = s.get("text", "")
            step.annotations = json.dumps(s.get("annotations", []))
            # Only relink the image when the client told us about one —
            # never blank an image we already hold on a partial payload.
            if "image_hash" in s or "image" in s:
                step.image_hash = img_hash if img_hash else None

        for sid, step in existing.items():
            if sid not in seen_ids:
                db.session.delete(step)

        # GC orphaned blobs after step deletions.
        db.session.flush()
        orphans = (ImageBlob.query
                   .outerjoin(Step, Step.image_hash == ImageBlob.hash)
                   .filter(Step.id.is_(None)).all())
        for b in orphans:
            db.session.delete(b)

    sop.updated_at = datetime.utcnow()
    db.session.commit()

    resp = sop.to_dict(include_steps=True)
    resp["have_hashes"] = sorted(have_hashes)
    return jsonify(resp)


@app.route("/api/sops/<sop_id>", methods=["DELETE"])
def delete_sop(sop_id: str):
    sop = db.get_or_404(SOP, sop_id)
    db.session.delete(sop)
    db.session.commit()
    return "", 204


# ── API: Generate .docx ───────────────────────────────────────────────────────

@app.route("/api/sops/<sop_id>/generate", methods=["POST"])
def generate_sop(sop_id: str):
    sop = db.get_or_404(SOP, sop_id)

    steps = []
    for step in sorted(sop.steps, key=lambda s: s.index):
        image_bytes = None
        img_data = step.blob.data if step.blob else None
        if img_data:
            if "," in img_data:
                img_data = img_data.split(",", 1)[1]
            image_bytes = base64.b64decode(img_data)
        steps.append({"image": image_bytes, "text": step.text or ""})

    docx_bytes = _fill_sop(
        steps=steps,
        part_no=sop.part_no or "",
        part_name=sop.part_name or "",
        doc_no=sop.doc_no or "",
        format_key=sop.format_key or "a3-landscape",
        steps_per_page=sop.steps_per_page or 8,
    )

    sop.status = "done"
    sop.updated_at = datetime.utcnow()
    db.session.commit()

    parts = [p for p in [sop.doc_no, sop.part_name] if p]
    raw = " - ".join(parts) if parts else "SOP"
    filename = re.sub(r'[\\/:*?"<>|]', "_", raw) + ".docx"

    return send_file(
        io.BytesIO(docx_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype=(
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ),
    )


# ── Label module ─────────────────────────────────────────────────────────────

@app.route("/api/label/preview", methods=["POST"])
def label_preview():
    """Return STL (base64) + edges JSON for Three.js viewer."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    step_bytes = f.read()
    filename = f.filename or "part.step"
    try:
        stl_bytes, edges = step_to_mesh_and_edges(step_bytes, filename)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({
        "stl": base64.b64encode(stl_bytes).decode(),
        "edges": edges,
    })


@app.route("/api/label/generate", methods=["POST"])
def label_generate():
    """Generate wireframe JPG from STEP + camera params, return as image/jpeg."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    step_bytes = f.read()
    filename = f.filename or "part.step"

    def _float(key):
        v = request.form.get(key)
        return float(v) if v is not None else None

    def _vec(prefix):
        x, y, z = _float(f"{prefix}_x"), _float(f"{prefix}_y"), _float(f"{prefix}_z")
        return (x, y, z) if None not in (x, y, z) else None

    try:
        jpg = step_to_wireframe(
            step_bytes,
            filename=filename,
            view=request.form.get("view", "isometric"),
            line_px=int(request.form.get("line_px", 3)),
            label_cm=float(request.form.get("label_cm", 26.0)),
            dpi=int(request.form.get("dpi", 300)),
            eye=_vec("eye"),
            right=_vec("right"),
            target=_vec("target"),
            fov_deg=_float("fov_deg"),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    return send_file(
        io.BytesIO(jpg),
        mimetype="image/jpeg",
        as_attachment=True,
        download_name=f"{filename.rsplit('.', 1)[0]}_label.jpg",
    )


# ── Label history (exported JPGs) ─────────────────────────────────────────────

@app.route("/api/labels", methods=["GET"])
def list_labels():
    rows = Label.query.order_by(Label.created_at.desc()).all()
    return jsonify([l.to_dict() for l in rows])


@app.route("/api/labels", methods=["POST"])
def create_label():
    d = request.get_json(silent=True) or {}
    image = d.get("image")  # full export JPG as a data URL
    if not image:
        return jsonify({"error": "no image"}), 400
    img_hash = hashlib.sha256(image.encode("utf-8")).hexdigest()
    if not db.session.get(ImageBlob, img_hash):
        db.session.add(ImageBlob(hash=img_hash, data=image))
    label = Label(
        name=(d.get("name") or "").strip(),
        part_name=(d.get("part_name") or "").strip(),
        size_mm=float(d.get("size_mm") or 26.0),
        dpi=int(d.get("dpi") or 300),
        image_hash=img_hash,
    )
    db.session.add(label)
    db.session.commit()
    return jsonify(label.to_dict()), 201


@app.route("/api/labels/<label_id>/image", methods=["GET"])
def label_image(label_id):
    label = db.session.get(Label, label_id)
    if not label or not label.blob:
        return jsonify({"error": "not found"}), 404
    data = label.blob.data
    b64 = data.split(",", 1)[1] if "," in data else data
    raw = base64.b64decode(b64)
    return send_file(
        io.BytesIO(raw),
        mimetype="image/jpeg",
        as_attachment=bool(request.args.get("download")),
        download_name=(label.name or "label.jpg"),
    )


@app.route("/api/labels/<label_id>", methods=["DELETE"])
def delete_label(label_id):
    label = db.session.get(Label, label_id)
    if label:
        db.session.delete(label)
        db.session.commit()
    return "", 204


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
