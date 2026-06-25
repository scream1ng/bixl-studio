"""app.py — BIXL Studio Flask API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

from modules.sop.fill import fill_sop as _fill_sop
from modules.label import step_to_wireframe, step_to_mesh_and_edges
from modules.icl import icl_mesh, icl_edges, icl_suggest, icl_measure
from modules.icl.export import fill_icl as _fill_icl
from modules.pfc import parse_bom as _parse_bom, model_to_svg as _pfc_svg, fill_pfc as _fill_pfc

app = Flask(__name__)

# ── Auth (single shared PIN gate) ─────────────────────────────────────────────
# Set SECRET_KEY + APP_PIN as env vars in production (Railway). Defaults are for
# local dev only — without a stable SECRET_KEY, sessions reset on every restart.
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
APP_PIN = os.environ.get("APP_PIN", "1858")
# Hard session cap: re-PIN 8h after login (a shift), regardless of activity.
# Mobile additionally locks after 15 min idle (client-side, see index.html).
app.permanent_session_lifetime = timedelta(hours=8)
app.config["SESSION_REFRESH_EACH_REQUEST"] = False  # expire from login, not sliding
# Cookie hardening. SECURE needs HTTPS, so only enforce it off localhost: local dev
# runs over plain HTTP and falls back to the insecure default SECRET_KEY — keying off
# that keeps local login working while production (Railway, real SECRET_KEY) stays Secure.
_is_local_http = os.environ.get("SECRET_KEY") is None
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True   # already Flask's default; explicit for clarity
app.config["SESSION_COOKIE_SECURE"] = not _is_local_http

# Loud warning if running on insecure defaults (don't silently ship them to prod).
if app.secret_key == "dev-insecure-change-me":
    app.logger.warning("SECRET_KEY not set — using insecure dev default. Set SECRET_KEY in production.")
if APP_PIN == "1858":
    app.logger.warning("APP_PIN not set — using default PIN. Set APP_PIN in production.")

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


class ICL(db.Model):
    """One saved inspection checklist — metadata + checks + screenshot refs.

    Stores enough to re-generate the .xlsx (view & re-export). Screenshots live
    in the content-addressed ImageBlob store; this row keeps [{type, hash}] refs.
    """
    __tablename__ = "icls"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    plant = db.Column(db.Text, nullable=False, default="")
    part_no = db.Column(db.Text, nullable=False, default="")
    cust_no = db.Column(db.Text, nullable=False, default="")
    part_desc = db.Column(db.Text, nullable=False, default="")
    checks = db.Column(db.Text, nullable=False, default="[]")        # JSON
    screenshots = db.Column(db.Text, nullable=False, default="[]")   # JSON [{type, hash}]
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def _checks(self) -> list:
        try:
            return json.loads(self.checks or "[]")
        except Exception:
            return []

    def _shots(self) -> list:
        try:
            return json.loads(self.screenshots or "[]")
        except Exception:
            return []

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "plant": self.plant,
            "part_no": self.part_no,
            "cust_no": self.cust_no,
            "part_desc": self.part_desc,
            "check_count": len(self._checks()),
            "screenshot_count": len(self._shots()),
            "created_at": self.created_at.isoformat(),
        }


class Mapping(db.Model):
    """One WIP part → the finished good(s) it rolls up into (Look Up module)."""
    __tablename__ = "mappings"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    wip_number = db.Column(db.Text, nullable=False, default="")
    wip_description = db.Column(db.Text, nullable=False, default="")
    finish_goods = db.Column(db.Text, nullable=False, default="[]")  # JSON: [{fg_number, fg_description}]
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "wip_number": self.wip_number,
            "wip_description": self.wip_description,
            "finish_goods": json.loads(self.finish_goods or "[]"),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class PFC(db.Model):
    """One saved process flow chart — header + parsed model + chart image.

    Stores the full parsed BOM model (JSON) plus the rendered chart PNG (in the
    content-addressed ImageBlob store) so it can be listed, viewed, and
    re-exported to .xlsx.
    """
    __tablename__ = "pfcs"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    part_no = db.Column(db.Text, nullable=False, default="")
    part_name = db.Column(db.Text, nullable=False, default="")
    model = db.Column(db.Text, nullable=False, default="{}")   # JSON parsed BOM model
    image_hash = db.Column(db.String(64), db.ForeignKey("image_blobs.hash"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def _model(self) -> dict:
        try:
            return json.loads(self.model or "{}")
        except Exception:
            return {}

    def to_dict(self) -> dict:
        m = self._model()
        return {
            "id": self.id,
            "part_no": self.part_no,
            "part_name": self.part_name,
            "op_count": len([o for o in m.get("operations", []) if o.get("op_no")]),
            "has_chart": bool(self.image_hash),
            "created_at": self.created_at.isoformat(),
        }


class Channel(db.Model):
    """One Topics thread — an open feed the team posts to."""
    __tablename__ = "channels"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug = db.Column(db.String(64), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    messages = db.relationship(
        "Message", backref="channel", lazy=True,
        order_by="Message.created_at", cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        last = self.messages[-1] if self.messages else None
        return {
            "id": self.id,
            "slug": self.slug,
            "description": self.description,
            "message_count": len(self.messages),
            "last_text": (last.text if last else ""),
            "last_at": (last.created_at.isoformat() if last else None),
            "created_at": self.created_at.isoformat(),
        }


class Message(db.Model):
    """One post in a topic — text and/or photo, with a timestamp."""
    __tablename__ = "messages"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id = db.Column(db.String(36), db.ForeignKey("channels.id"), nullable=False)
    text = db.Column(db.Text, nullable=False, default="")
    image_hash = db.Column(db.String(64), db.ForeignKey("image_blobs.hash"), nullable=True)
    image_hashes = db.Column(db.Text, nullable=True)  # JSON list of blob hashes (multi-photo)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    edited_at = db.Column(db.DateTime, nullable=True)

    def hashes(self) -> list:
        """Ordered list of image blob hashes, folding the legacy single column in."""
        if self.image_hashes:
            try:
                vals = json.loads(self.image_hashes)
                if isinstance(vals, list):
                    return [h for h in vals if h]
            except (ValueError, TypeError):
                pass
        return [self.image_hash] if self.image_hash else []

    def to_dict(self) -> dict:
        hs = self.hashes()
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "text": self.text or "",
            "has_image": bool(hs),
            "image_count": len(hs),
            "images": [f"/api/messages/{self.id}/image/{i}" for i in range(len(hs))],
            "edited": self.edited_at is not None,
            "edited_at": self.edited_at.isoformat() if self.edited_at else None,
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
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_mappings_wip ON mappings (wip_number)",
            "ALTER TABLE messages ADD COLUMN image_hashes TEXT",
            "ALTER TABLE messages ADD COLUMN edited_at TIMESTAMP",
        ):
            try:
                _conn.execute(text(_stmt))
                _conn.commit()
            except Exception:
                # A failed statement (e.g. column already exists) aborts the
                # current transaction on Postgres — roll back so the next
                # statement in the loop isn't skipped with "transaction aborted".
                _conn.rollback()

    # Seed Look Up master data (IXL Search DB) from lookup_seed.json on an empty table.
    if Mapping.query.count() == 0:
        _seed_path = os.path.join(os.path.dirname(__file__), "docx", "look-up", "lookup_seed.json")
        if os.path.exists(_seed_path):
            with open(_seed_path, encoding="utf-8") as _f:
                for _m in json.load(_f):
                    db.session.add(Mapping(
                        wip_number=_m.get("wip_number", ""),
                        wip_description=_m.get("wip_description", ""),
                        finish_goods=json.dumps(_m.get("finish_goods", [])),
                    ))
            db.session.commit()

    # Seed default Topics threads on an empty table.
    if Channel.query.count() == 0:
        for _slug, _desc in (
            ("general", "Plant-wide announcements"),
            ("shift-handoff", "Pass the line between shifts"),
            ("maintenance", "Breakdowns, repairs, spare parts"),
            ("quality", "Scrap, defects, inspection notes"),
            ("line-2-coater", "Watching the edge sensor closely"),
        ):
            db.session.add(Channel(slug=_slug, description=_desc))
        db.session.commit()


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


# ── Auth gate ─────────────────────────────────────────────────────────────────

@app.before_request
def _require_pin():
    # Deny-by-default on the data API: every /api/* route needs an authenticated
    # session except the two login doorways. Non-/api routes (index, static,
    # sw.js, healthz) are public. Gating on the URL keeps this stable across
    # endpoint renames and auto-protects any new /api route.
    p = request.path
    if not p.startswith("/api/") or p in ("/api/login", "/api/session"):
        return None
    if not session.get("authed"):
        return jsonify({"error": "auth required"}), 401
    return None


@app.route("/api/session")
def session_status():
    return jsonify({"authed": bool(session.get("authed"))})


# Login brute-force throttle. In-memory, per client IP. Safe because gunicorn runs
# a single worker (see Procfile); revisit if --workers is ever raised.
_LOGIN_MAX_FAILS = 5      # failed attempts allowed within the window before lockout
_LOGIN_WINDOW = 300       # seconds: rolling window for counting failures
_LOGIN_LOCKOUT = 300      # seconds: how long a tripped IP stays locked out
_login_fails: dict[str, list[float]] = {}   # ip -> recent failure timestamps
_login_locked: dict[str, float] = {}        # ip -> monotonic unlock time


def _client_ip() -> str:
    # Railway terminates TLS at a proxy, so remote_addr is the proxy. Trust the
    # first hop in X-Forwarded-For for per-client throttling.
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or (request.remote_addr or "?")


def _login_lock_remaining(ip: str) -> float:
    """Seconds left on this IP's lockout, or 0 if not locked."""
    remaining = _login_locked.get(ip, 0.0) - time.monotonic()
    return remaining if remaining > 0 else 0.0


def _record_login_fail(ip: str) -> None:
    now = time.monotonic()
    fails = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW]
    fails.append(now)
    _login_fails[ip] = fails
    if len(fails) >= _LOGIN_MAX_FAILS:
        _login_locked[ip] = now + _LOGIN_LOCKOUT
        _login_fails.pop(ip, None)


@app.route("/api/login", methods=["POST"])
def login():
    ip = _client_ip()
    locked = _login_lock_remaining(ip)
    if locked:
        retry = int(locked) + 1
        resp = jsonify({"error": "too many attempts", "retry_after": retry})
        resp.headers["Retry-After"] = str(retry)
        return resp, 429
    pin = (request.get_json(silent=True) or {}).get("pin", "")
    # Constant-time compare so response timing can't leak PIN digits.
    if hmac.compare_digest(str(pin), APP_PIN):
        _login_fails.pop(ip, None)
        _login_locked.pop(ip, None)
        session.permanent = True   # apply the 8h lifetime
        session["authed"] = True
        return jsonify({"ok": True})
    _record_login_fail(ip)
    return jsonify({"error": "invalid pin"}), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


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
        # Hashes this SOP referenced before the update — GC the ones it drops.
        prior_hashes = {st.image_hash for st in existing.values() if st.image_hash}

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

        # GC only the blobs this SOP dropped, and only if nothing else uses them.
        db.session.flush()
        _gc_blobs(prior_hashes)

    sop.updated_at = datetime.utcnow()
    db.session.commit()

    resp = sop.to_dict(include_steps=True)
    resp["have_hashes"] = sorted(have_hashes)
    return jsonify(resp)


@app.route("/api/sops/<sop_id>", methods=["DELETE"])
def delete_sop(sop_id: str):
    sop = db.get_or_404(SOP, sop_id)
    hashes = {s.image_hash for s in sop.steps if s.image_hash}
    db.session.delete(sop)
    db.session.flush()
    _gc_blobs(hashes)
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


# ── ICL module (inspection measuring) ─────────────────────────────────────────

@app.route("/api/icl/mesh", methods=["POST"])
def icl_mesh_route():
    """Face-tagged mesh + per-face metadata + indexed edges for the viewer."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    step_bytes = f.read()
    filename = f.filename or "part.step"
    try:
        mesh = icl_mesh(step_bytes, filename)
        edges = icl_edges(step_bytes, filename)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"mesh": mesh, "edges": edges["edges"]})


@app.route("/api/icl/suggest", methods=["POST"])
def icl_suggest_route():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    try:
        result = icl_suggest(f.read(), f.filename or "part.step")
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(result)


@app.route("/api/icl/measure", methods=["POST"])
def icl_measure_route():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    k2 = request.form.get("kind2")
    i2 = request.form.get("id2")
    try:
        result = icl_measure(
            f.read(),
            kind1=request.form.get("kind1", "face"),
            id1=int(request.form.get("id1")),
            kind2=k2 if k2 else None,
            id2=int(i2) if i2 else None,
            filename=f.filename or "part.step",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(result)


def _decode_data_url(data_url: str) -> bytes:
    """Decode a (possibly data-URL-prefixed) base64 string to raw bytes."""
    b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
    return base64.b64decode(b64)


def _gc_blobs(hashes) -> None:
    """Delete each given image blob iff nothing references it anymore.

    Call after the owning rows are deleted/flushed. Reclaims Postgres storage
    without touching blobs still shared by another module.
    """
    cands = {x for x in (hashes or []) if x}
    if not cands:
        return
    # ICL keeps screenshot hashes in a JSON text column — scan it once, not per hash.
    icl_hashes = {s.get("hash") for icl in ICL.query.all() for s in icl._shots()}
    # Messages may keep several hashes in a JSON column — scan them once too.
    msg_hashes = {h for m in Message.query.all() for h in m.hashes()}
    for h in cands:
        if h in icl_hashes or h in msg_hashes:
            continue
        if any(db.session.query(m.id).filter_by(image_hash=h).first()
               for m in (Step, Label, PFC)):
            continue
        b = db.session.get(ImageBlob, h)
        if b:
            db.session.delete(b)


def _send_jpeg_blob(blob):
    return send_file(io.BytesIO(_decode_data_url(blob.data)), mimetype="image/jpeg")


def _send_icl_xlsx(part_no: str, xlsx: bytes):
    raw = f"{part_no} - ICL" if part_no else "ICL"
    filename = re.sub(r'[\\/:*?"<>|]', "_", raw) + ".xlsx"
    return send_file(
        io.BytesIO(xlsx),
        as_attachment=True,
        download_name=filename,
        mimetype=(
            "application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet"
        ),
    )


def _store_icl(payload: dict) -> "ICL":
    """Persist an ICL record; screenshots go to the content-addressed blob store."""
    refs = []
    for s in (payload.get("screenshots") or []):
        img = s.get("image")
        if not img:
            continue
        h = hashlib.sha256(img.encode("utf-8")).hexdigest()
        if not db.session.get(ImageBlob, h):
            db.session.add(ImageBlob(hash=h, data=img))
        refs.append({"type": s.get("type") or "view", "hash": h})
    icl = ICL(
        plant=(payload.get("plant") or "").strip(),
        part_no=(payload.get("part_no") or "").strip(),
        cust_no=(payload.get("cust_no") or "").strip(),
        part_desc=(payload.get("part_desc") or "").strip(),
        checks=json.dumps(payload.get("checks") or []),
        screenshots=json.dumps(refs),
    )
    db.session.add(icl)
    db.session.commit()
    return icl


def _icl_shots_for_fill(icl: "ICL") -> list:
    out = []
    for ref in icl._shots():
        blob = db.session.get(ImageBlob, ref.get("hash"))
        if blob:
            out.append({"type": ref.get("type") or "view", "image": blob.data})
    return out


@app.route("/api/icl/export", methods=["POST"])
def icl_export_route():
    """Fill the IXL Inspection Check List template → .xlsx attachment (+ save to history)."""
    payload = request.get_json(silent=True) or {}
    part_no = payload.get("part_no") or ""
    try:
        xlsx = _fill_icl(
            part_no=part_no,
            cust_no=payload.get("cust_no") or "",
            part_desc=payload.get("part_desc") or "",
            plant=payload.get("plant") or "",
            checks=payload.get("checks") or [],
            screenshots=payload.get("screenshots") or [],
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    try:
        _store_icl(payload)   # best-effort history save
    except Exception:
        db.session.rollback()

    return _send_icl_xlsx(part_no, xlsx)


# ── ICL history (saved checklists) ────────────────────────────────────────────

@app.route("/api/icls", methods=["GET"])
def list_icls():
    rows = ICL.query.order_by(ICL.created_at.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@app.route("/api/icls", methods=["POST"])
def create_icl():
    payload = request.get_json(silent=True) or {}
    icl = _store_icl(payload)
    return jsonify(icl.to_dict()), 201


@app.route("/api/icls/<icl_id>", methods=["GET"])
def get_icl(icl_id):
    icl = db.session.get(ICL, icl_id)
    if not icl:
        return jsonify({"error": "not found"}), 404
    shots = icl._shots()
    d = icl.to_dict()
    d["checks"] = icl._checks()
    d["screenshots"] = [
        {"type": r.get("type"), "url": f"/api/icls/{icl.id}/image/{i}"}
        for i, r in enumerate(shots)
    ]
    return jsonify(d)


@app.route("/api/icls/<icl_id>/image/<int:idx>", methods=["GET"])
def icl_image(icl_id, idx):
    icl = db.session.get(ICL, icl_id)
    shots = icl._shots() if icl else []
    if not icl or idx < 0 or idx >= len(shots):
        return jsonify({"error": "not found"}), 404
    blob = db.session.get(ImageBlob, shots[idx].get("hash"))
    if not blob:
        return jsonify({"error": "not found"}), 404
    return _send_jpeg_blob(blob)


@app.route("/api/icls/<icl_id>/thumb", methods=["GET"])
def icl_thumb(icl_id):
    """First ISO screenshot (or first available) as the list thumbnail."""
    icl = db.session.get(ICL, icl_id)
    shots = icl._shots() if icl else []
    if not icl or not shots:
        return jsonify({"error": "not found"}), 404
    pick = next((s for s in shots if s.get("type") == "iso"), shots[0])
    blob = db.session.get(ImageBlob, pick.get("hash"))
    if not blob:
        return jsonify({"error": "not found"}), 404
    return _send_jpeg_blob(blob)


@app.route("/api/icls/<icl_id>/export", methods=["GET", "POST"])
def icl_reexport(icl_id):
    """Re-generate the .xlsx from a saved record."""
    icl = db.session.get(ICL, icl_id)
    if not icl:
        return jsonify({"error": "not found"}), 404
    try:
        xlsx = _fill_icl(
            part_no=icl.part_no, cust_no=icl.cust_no, part_desc=icl.part_desc,
            plant=icl.plant, checks=icl._checks(),
            screenshots=_icl_shots_for_fill(icl),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return _send_icl_xlsx(icl.part_no, xlsx)


@app.route("/api/icls/<icl_id>", methods=["DELETE"])
def delete_icl(icl_id):
    icl = db.session.get(ICL, icl_id)
    if icl:
        hashes = {s.get("hash") for s in icl._shots()}
        db.session.delete(icl)
        db.session.flush()
        _gc_blobs(hashes)
        db.session.commit()
    return "", 204


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
    return send_file(
        io.BytesIO(_decode_data_url(label.blob.data)),
        mimetype="image/jpeg",
        as_attachment=bool(request.args.get("download")),
        download_name=(label.name or "label.jpg"),
    )


@app.route("/api/labels/<label_id>", methods=["DELETE"])
def delete_label(label_id):
    label = db.session.get(Label, label_id)
    if label:
        h = label.image_hash
        db.session.delete(label)
        db.session.flush()
        _gc_blobs([h])
        db.session.commit()
    return "", 204


# ── API: Look Up (WIP → Finish Good mappings) ─────────────────────────────────

def _clean_fgs(raw) -> list:
    """Normalize an incoming finish_goods list, dropping empty rows."""
    out = []
    for fg in raw or []:
        num = (fg.get("fg_number") or "").strip()
        desc = (fg.get("fg_description") or "").strip()
        if num or desc:
            out.append({"fg_number": num, "fg_description": desc})
    return out


def _find_mapping_by_wip(wip: str, exclude_id: str | None = None):
    """Return the mapping whose WIP number matches (case-insensitive), or None."""
    key = (wip or "").strip().upper()
    if not key:
        return None
    for m in Mapping.query.all():
        if m.id == exclude_id:
            continue
        if (m.wip_number or "").strip().upper() == key:
            return m
    return None


@app.route("/api/mappings", methods=["GET"])
def list_mappings():
    rows = Mapping.query.order_by(Mapping.updated_at.desc()).all()
    return jsonify([m.to_dict() for m in rows])


@app.route("/api/mappings", methods=["POST"])
def create_mapping():
    d = request.get_json(silent=True) or {}
    wip = (d.get("wip_number") or "").strip()
    desc = (d.get("wip_description") or "").strip()
    new_fgs = _clean_fgs(d.get("finish_goods"))

    # If this WIP already exists, merge the new finished goods into it
    # (dedupe by FG number) instead of creating a duplicate row.
    existing = _find_mapping_by_wip(wip)
    if existing:
        fgs = json.loads(existing.finish_goods or "[]")
        have = {(f.get("fg_number") or "").strip().upper() for f in fgs}
        for fg in new_fgs:
            if fg["fg_number"].upper() not in have:
                fgs.append(fg)
                have.add(fg["fg_number"].upper())
        existing.finish_goods = json.dumps(fgs)
        if desc:
            existing.wip_description = desc
        existing.updated_at = datetime.utcnow()
        db.session.commit()
        resp = existing.to_dict()
        resp["merged"] = True
        return jsonify(resp), 200

    m = Mapping(wip_number=wip, wip_description=desc,
                finish_goods=json.dumps(new_fgs))
    db.session.add(m)
    db.session.commit()
    return jsonify(m.to_dict()), 201


@app.route("/api/mappings/<mapping_id>", methods=["GET"])
def get_mapping(mapping_id: str):
    m = db.get_or_404(Mapping, mapping_id)
    return jsonify(m.to_dict())


@app.route("/api/mappings/<mapping_id>", methods=["PUT"])
def update_mapping(mapping_id: str):
    m = db.get_or_404(Mapping, mapping_id)
    d = request.get_json(silent=True) or {}
    if "wip_number" in d:
        new_wip = (d.get("wip_number") or "").strip()
        clash = _find_mapping_by_wip(new_wip, exclude_id=m.id)
        if clash:
            return jsonify({
                "error": f"WIP {new_wip} is already mapped. Edit that mapping instead.",
                "existing_id": clash.id,
            }), 409
        m.wip_number = new_wip
    if "wip_description" in d:
        m.wip_description = (d.get("wip_description") or "").strip()
    if "finish_goods" in d:
        m.finish_goods = json.dumps(_clean_fgs(d.get("finish_goods")))
    m.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(m.to_dict())


@app.route("/api/mappings/<mapping_id>", methods=["DELETE"])
def delete_mapping(mapping_id: str):
    m = db.session.get(Mapping, mapping_id)
    if m:
        db.session.delete(m)
        db.session.commit()
    return "", 204


@app.route("/api/lookup", methods=["GET"])
def lookup_wip():
    """Operator lookup: exact (normalized) WIP number → mapping, or 404."""
    q = (request.args.get("wip") or "").strip()
    if not q:
        return jsonify({"error": "no query"}), 400
    m = _find_mapping_by_wip(q)
    if m:
        return jsonify(m.to_dict())
    return jsonify({"error": "not found", "query": q.upper()}), 404


# ── API: PFC (BOM transaction export → process flow chart) ────────────────────

def _send_pfc_xlsx(part_no: str, xlsx: bytes):
    raw = f"{part_no} - PFC" if part_no else "PFC"
    filename = re.sub(r'[\\/:*?"<>|]', "_", raw) + ".xlsx"
    return send_file(
        io.BytesIO(xlsx),
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _store_pfc(payload: dict) -> "PFC":
    """Persist a PFC record; the chart PNG goes to the content-addressed store."""
    model = payload.get("model") or {}
    header = model.get("header") or {}
    img_hash = None
    chart = payload.get("chart_png")
    if chart:
        img_hash = hashlib.sha256(chart.encode("utf-8")).hexdigest()
        if not db.session.get(ImageBlob, img_hash):
            db.session.add(ImageBlob(hash=img_hash, data=chart))
    pfc = PFC(
        part_no=(header.get("part_no") or "").strip(),
        part_name=(header.get("part_name") or "").strip(),
        model=json.dumps(model),
        image_hash=img_hash,
    )
    db.session.add(pfc)
    db.session.commit()
    return pfc


@app.route("/api/pfc/parse", methods=["POST"])
def pfc_parse_route():
    """Parse an uploaded BOM transaction export into a flow model + SVG chart."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    try:
        model = _parse_bom(f.read())
    except Exception as e:
        return jsonify({"error": str(e)}), 422
    return jsonify({"model": model, "svg": _pfc_svg(model)})


@app.route("/api/pfcs", methods=["GET"])
def list_pfcs():
    rows = PFC.query.order_by(PFC.created_at.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@app.route("/api/pfcs", methods=["POST"])
def create_pfc():
    """Save a PFC (model + chart PNG) to history. Returns the new record."""
    payload = request.get_json(silent=True) or {}
    if not (payload.get("model") or {}).get("operations"):
        return jsonify({"error": "no model"}), 400
    pfc = _store_pfc(payload)
    return jsonify(pfc.to_dict()), 201


@app.route("/api/pfcs/<pfc_id>", methods=["GET"])
def get_pfc(pfc_id):
    pfc = db.session.get(PFC, pfc_id)
    if not pfc:
        return jsonify({"error": "not found"}), 404
    d = pfc.to_dict()
    d["model"] = pfc._model()
    d["chart_url"] = f"/api/pfcs/{pfc.id}/thumb" if pfc.image_hash else None
    return jsonify(d)


@app.route("/api/pfcs/<pfc_id>/thumb", methods=["GET"])
def pfc_thumb(pfc_id):
    pfc = db.session.get(PFC, pfc_id)
    if not pfc or not pfc.image_hash:
        return jsonify({"error": "not found"}), 404
    blob = db.session.get(ImageBlob, pfc.image_hash)
    if not blob:
        return jsonify({"error": "not found"}), 404
    return send_file(io.BytesIO(_decode_data_url(blob.data)), mimetype="image/png")


@app.route("/api/pfcs/<pfc_id>/export", methods=["GET", "POST"])
def pfc_reexport(pfc_id):
    """Re-generate the .xlsx from a saved PFC record."""
    pfc = db.session.get(PFC, pfc_id)
    if not pfc:
        return jsonify({"error": "not found"}), 404
    png = ""
    if pfc.image_hash:
        blob = db.session.get(ImageBlob, pfc.image_hash)
        if blob:
            png = blob.data
    try:
        xlsx = _fill_pfc(pfc._model(), png)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return _send_pfc_xlsx(pfc.part_no, xlsx)


@app.route("/api/pfcs/<pfc_id>", methods=["DELETE"])
def delete_pfc(pfc_id):
    pfc = db.session.get(PFC, pfc_id)
    if pfc:
        h = pfc.image_hash
        db.session.delete(pfc)
        db.session.flush()
        _gc_blobs([h])
        db.session.commit()
    return "", 204


# ── API: Topics (team discussion threads) ────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


@app.route("/api/channels", methods=["GET"])
def list_channels():
    rows = Channel.query.order_by(Channel.created_at).all()
    return jsonify([c.to_dict() for c in rows])


@app.route("/api/channels", methods=["POST"])
def create_channel():
    d = request.get_json(silent=True) or {}
    slug = _SLUG_RE.sub("-", (d.get("slug") or "").strip().lower()).strip("-")
    if not slug:
        return jsonify({"error": "invalid slug"}), 400
    if Channel.query.filter_by(slug=slug).first():
        return jsonify({"error": "slug exists"}), 409
    ch = Channel(slug=slug, description=(d.get("description") or "").strip())
    db.session.add(ch)
    db.session.commit()
    return jsonify(ch.to_dict()), 201


@app.route("/api/channels/<channel_id>", methods=["DELETE"])
def delete_channel(channel_id):
    ch = db.session.get(Channel, channel_id)
    if ch:
        hashes = {h for m in ch.messages for h in m.hashes()}
        db.session.delete(ch)  # cascades to its messages
        db.session.flush()
        _gc_blobs(hashes)
        db.session.commit()
    return "", 204


@app.route("/api/channels/<channel_id>/messages", methods=["GET"])
def list_messages(channel_id):
    if not db.session.get(Channel, channel_id):
        return jsonify({"error": "not found"}), 404
    q = Message.query.filter_by(channel_id=channel_id)
    after = request.args.get("after")
    if after:
        q = q.filter(Message.created_at > datetime.fromisoformat(after))
    rows = q.order_by(Message.created_at).all()
    return jsonify([m.to_dict() for m in rows])


@app.route("/api/channels/<channel_id>/messages", methods=["POST"])
def create_message(channel_id):
    if not db.session.get(Channel, channel_id):
        return jsonify({"error": "not found"}), 404
    d = request.get_json(silent=True) or {}
    text_val = (d.get("text") or "").strip()
    # Accept a list of photos (data URLs); still accept the legacy single `image`.
    images = d.get("images")
    if not isinstance(images, list):
        images = [d.get("image")] if d.get("image") else []
    images = [im for im in images if im]
    if not text_val and not images:
        return jsonify({"error": "empty message"}), 400
    hashes = []
    for im in images:
        h = _sha256(im)
        if not db.session.get(ImageBlob, h):
            db.session.add(ImageBlob(hash=h, data=im))
        hashes.append(h)
    msg = Message(
        channel_id=channel_id,
        text=text_val,
        image_hash=hashes[0] if hashes else None,  # keep legacy column populated
        image_hashes=json.dumps(hashes) if hashes else None,
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify(msg.to_dict()), 201


@app.route("/api/messages/<message_id>", methods=["PATCH"])
def edit_message(message_id):
    msg = db.session.get(Message, message_id)
    if not msg:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(silent=True) or {}
    text_val = (d.get("text") or "").strip()
    if not text_val and not msg.hashes():
        return jsonify({"error": "empty message"}), 400
    msg.text = text_val
    msg.edited_at = datetime.utcnow()
    db.session.commit()
    return jsonify(msg.to_dict())


@app.route("/api/messages/<message_id>", methods=["DELETE"])
def delete_message(message_id):
    msg = db.session.get(Message, message_id)
    if msg:
        hashes = set(msg.hashes())
        db.session.delete(msg)
        db.session.flush()
        _gc_blobs(hashes)
        db.session.commit()
    return "", 204


@app.route("/api/messages/<message_id>/image", methods=["GET"])
@app.route("/api/messages/<message_id>/image/<int:idx>", methods=["GET"])
def message_image(message_id, idx=0):
    msg = db.session.get(Message, message_id)
    if not msg:
        return jsonify({"error": "not found"}), 404
    hashes = msg.hashes()
    if idx < 0 or idx >= len(hashes):
        return jsonify({"error": "not found"}), 404
    blob = db.session.get(ImageBlob, hashes[idx])
    if not blob:
        return jsonify({"error": "not found"}), 404
    return _send_jpeg_blob(blob)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
