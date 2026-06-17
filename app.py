"""app.py — BIXL Studio Flask API."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import uuid
from datetime import datetime

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from flask_sqlalchemy import SQLAlchemy

from fill_sop import fill_sop as _fill_sop

app = Flask(__name__)

# ── Database ─────────────────────────────────────────────────────────────────
_db_url = os.environ.get("DATABASE_URL", "sqlite:///bixl.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB

db = SQLAlchemy(app)

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "SOP_Template_A3.docx")


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


with app.app_context():
    db.create_all()


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

    for field in ("part_no", "part_name", "doc_no", "status"):
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
        template_path=TEMPLATE_PATH,
    )

    sop.status = "done"
    sop.updated_at = datetime.utcnow()
    db.session.commit()

    safe_part = (sop.part_no or "draft").replace(" ", "_")
    safe_doc = (sop.doc_no or "v1").replace(" ", "_")
    filename = f"SOP_{safe_part}_{safe_doc}.docx"

    return send_file(
        io.BytesIO(docx_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype=(
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
