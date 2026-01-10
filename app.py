# app.py
import os
import io
import csv
from functools import wraps
from datetime import datetime
from typing import Optional, List, Tuple, Any, Dict

from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
    session,
    jsonify,
    abort,
)

from werkzeug.security import generate_password_hash, check_password_hash

from models import (
    db,
    User,
    Property,
    ReserveStudy,
    ReserveComponent,
    ComponentPhoto,
    TempComponentPhoto,
    ReserveYearResult,
)

from reserve_math import recommend_levelized_full_funding_contribution
from storage import put_object_bytes, delete_object, presign_get_url, make_storage_key

ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
STUDY_PRICE_CENTS = 200000  # $2000 simulated


def _db_uri() -> str:
    uri = os.getenv("DATABASE_URL")
    if not uri:
        raise RuntimeError("DATABASE_URL is not set.")
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    return uri


def _storage_provider() -> str:
    # Keep flexible: could be "s3", "gcs", "azure", etc.
    return (os.getenv("STORAGE_PROVIDER") or "s3").strip()


def _storage_bucket() -> str:
    # You MUST set this if your DB schema requires storage_bucket NOT NULL
    b = (os.getenv("STORAGE_BUCKET") or "").strip()
    if not b:
        raise RuntimeError("STORAGE_BUCKET is not set (required for photo rows).")
    return b


def _safe_model_kwargs(model_cls, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Only pass kwargs that exist as attributes on the SQLAlchemy model class.
    Prevents crashes if one model has a column and another doesn't.
    """
    out = {}
    for k, v in data.items():
        if hasattr(model_cls, k):
            out[k] = v
    return out


def _session_user() -> Optional[User]:
    uid = session.get("user_id")
    if not uid:
        return None
    try:
        user = db.session.get(User, int(uid))
    except Exception:
        user = None
    if not user:
        session.clear()
        return None
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _session_user()
        if not user:
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def current_user() -> Optional[User]:
    return _session_user()


def _require_owner_property(u: User, prop_id: int) -> Property:
    prop = Property.query.filter_by(id=prop_id, user_id=u.id).first()
    if not prop:
        abort(404)
    return prop


def _require_owner_study(u: User, study_id: int) -> ReserveStudy:
    study = ReserveStudy.query.get_or_404(study_id)
    if not study.property or study.property.user_id != u.id:
        abort(403)
    return study


def _study_is_locked(study: ReserveStudy) -> bool:
    return (study.paid_status or "").lower() == "paid"


def _validate_image_file(f) -> Tuple[Optional[Tuple[bytes, str]], Optional[str]]:
    if not f or not getattr(f, "filename", None):
        return None, "No file uploaded."
    mime = (f.mimetype or "").lower()
    if mime not in ALLOWED_IMAGE_MIMES:
        return None, f"Unsupported file type: {mime}"
    raw = f.read()
    if not raw:
        return None, "Empty file."
    return (raw, mime), None


def _parse_float(form, key, default=None):
    v = (form.get(key) or "").strip()
    if v == "":
        return default
    return float(v)


def _parse_int(form, key, default=None):
    v = (form.get(key) or "").strip()
    if v == "":
        return default
    return int(v)


def _form_first(*keys: str) -> str:
    for k in keys:
        v = (request.form.get(k) or "").strip()
        if v != "":
            return v
    return ""


def create_app():
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

    app.config["SQLALCHEMY_DATABASE_URI"] = _db_uri().replace(
        "postgresql://", "postgresql+psycopg://", 1
        )

    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Cookie hardening (Render is HTTPS)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = True  # keep True on Render/HTTPS

    # Photo storage metadata (required)
    app.config["STORAGE_PROVIDER"] = _storage_provider()
    app.config["STORAGE_BUCKET"] = _storage_bucket()

    db.init_app(app)

    with app.app_context():
        db.create_all()

    @app.context_processor
    def inject_user():
        return {"current_user": current_user()}

    @app.after_request
    def add_no_cache_headers(resp):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    # --------------------
    # Auth
    # --------------------
    @app.get("/signup")
    def signup():
        if session.get("user_id"):
            session.clear()
            flash("You're being signed out so you can create a new account.")
        return render_template("signup.html")

    @app.post("/signup")
    def signup_post():
        if session.get("user_id"):
            session.clear()

        email = _form_first("signup_email", "email").lower()
        password = _form_first("signup_password", "password")
        password2 = _form_first("signup_password2", "password2")

        if not email or not password:
            flash("Email and password are required.")
            return redirect(url_for("signup"))
        if password != password2:
            flash("Passwords do not match.")
            return redirect(url_for("signup"))
        if len(password) < 8:
            flash("Password must be at least 8 characters.")
            return redirect(url_for("signup"))

        existing = User.query.filter_by(email=email).first()
        if existing:
            flash("That email is already registered. Try logging in.")
            return redirect(url_for("login"))

        u = User(email=email, password_hash=generate_password_hash(password), is_admin=False)
        db.session.add(u)
        db.session.commit()

        session.clear()
        session["user_id"] = u.id
        flash("Account created. You’re logged in!")
        return redirect(url_for("home"))

    @app.get("/login")
    def login():
        if session.get("user_id") and _session_user():
            return redirect(url_for("home"))
        next_url = request.args.get("next") or url_for("home")
        return render_template("login.html", next_url=next_url)

    @app.post("/login")
    def login_post():
        email = (request.form.get("username") or "").strip().lower()
        password = (request.form.get("password") or "").strip()
        next_url = request.form.get("next_url") or url_for("home")

        u = User.query.filter_by(email=email).first()
        if u and check_password_hash(u.password_hash, password):
            session.clear()
            session["user_id"] = u.id
            return redirect(next_url)

        flash("Invalid email or password.")
        return redirect(url_for("login", next=next_url))

    @app.get("/logout")
    def logout():
        session.clear()
        flash("You’ve been logged out.")
        return redirect(url_for("login"))

    # --------------------
    # Home / Properties
    # --------------------
    @app.get("/")
    @login_required
    def home():
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))

        props = (
            Property.query.filter_by(user_id=u.id)
            .order_by(Property.created_at.desc())
            .all()
        )
        return render_template("home.html", properties=props)

    @app.post("/properties/create")
    @login_required
    def create_property():
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))

        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Property name is required.")
            return redirect(url_for("home"))

        prop = Property(
            user_id=u.id,
            name=name,
            address=(request.form.get("address") or "").strip() or None,
            city=(request.form.get("city") or "").strip() or None,
            state=(request.form.get("state") or "").strip() or None,
        )
        db.session.add(prop)
        db.session.commit()
        return redirect(url_for("property_page", property_id=prop.id))

    @app.get("/properties/<int:property_id>")
    @login_required
    def property_page(property_id: int):
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))

        prop = Property.query.filter_by(id=property_id, user_id=u.id).first()
        if not prop:
            abort(404)

        studies = (
            ReserveStudy.query.filter_by(property_id=prop.id)
            .order_by(ReserveStudy.created_at.desc())
            .all()
        )

        # ✅ NEW: split into completed vs drafts
        completed_studies = []
        draft_studies = []
        for s in studies:
            if (s.paid_status or "").lower() == "paid":
                completed_studies.append(s)
            else:
                draft_studies.append(s)

        return render_template(
            "property.html",
            prop=prop,
            completed_studies=completed_studies,
            draft_studies=draft_studies,
        )

    # --------------------
    # Study routes: new, edit, clone
    # --------------------
    @app.get("/studies/new")
    @login_required
    def new_study():
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))

        property_id = request.args.get("property_id", type=int)
        clone_from = request.args.get("clone_from", type=int)

        prop = Property.query.filter_by(id=property_id, user_id=u.id).first() if property_id else None
        if not prop:
            flash("Choose a property first.")
            return redirect(url_for("home"))

        clone = None
        if clone_from:
            clone = ReserveStudy.query.get_or_404(clone_from)
            if clone.property.user_id != u.id:
                abort(403)

        defaults = {
            "start_year": "",
            "horizon_years": "",
            "inflation_rate": "",
            "interest_rate": "",
            "starting_balance": "",
            "min_balance": "",
            "funding_method": "full",
            "contribution_mode": "levelized",
        }

        components = [
            {"id": "", "row_key": "", "name": "", "quantity": "", "useful_life_years": "", "remaining_life_years": "", "cycle_years": "", "current_replacement_cost": "", "photos": []},
        ]

        if clone:
            defaults = {
                "start_year": clone.start_year,
                "horizon_years": clone.horizon_years,
                "inflation_rate": clone.inflation_rate,
                "interest_rate": clone.interest_rate,
                "starting_balance": clone.starting_balance,
                "min_balance": clone.min_balance or 0,
                "funding_method": clone.funding_method or "full",
                "contribution_mode": clone.contribution_mode or "levelized",
            }
            components = []
            for c in clone.components:
                components.append({
                    "id": "",
                    "row_key": "",
                    "name": c.name,
                    "quantity": c.quantity or 1,
                    "useful_life_years": c.useful_life_years,
                    "remaining_life_years": c.remaining_life_years,
                    "cycle_years": (c.cycle_years or 0) or c.useful_life_years,
                    "current_replacement_cost": c.current_replacement_cost,
                    "photos": [],
                })

        return render_template("study_form.html", prop=prop, defaults=defaults, components=components, edit_mode=False, study=None)

    @app.get("/studies/<int:study_id>/edit")
    @login_required
    def edit_study(study_id: int):
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))

        study = _require_owner_study(u, study_id)
        prop = study.property

        components = ReserveComponent.query.filter_by(study_id=study.id).order_by(ReserveComponent.id.asc()).all()

        comp_photos = {}
        for c in components:
            photos = ComponentPhoto.query.filter_by(component_id=c.id).order_by(ComponentPhoto.created_at.asc()).all()
            comp_photos[c.id] = [{"id": p.id, "url": presign_get_url(p.storage_key, expires_seconds=900), "name": (getattr(p, "filename", None) or getattr(p, "original_filename", None) or "photo")} for p in photos]

        defaults = {
            "start_year": study.start_year,
            "horizon_years": study.horizon_years,
            "inflation_rate": study.inflation_rate,
            "interest_rate": study.interest_rate,
            "starting_balance": study.starting_balance,
            "min_balance": study.min_balance,
            "funding_method": study.funding_method,
            "contribution_mode": study.contribution_mode,
        }

        component_rows = []
        for c in components:
            component_rows.append({
                "id": c.id,
                "row_key": "",
                "name": c.name,
                "quantity": c.quantity,
                "useful_life_years": c.useful_life_years,
                "remaining_life_years": c.remaining_life_years,
                "cycle_years": c.cycle_years or c.useful_life_years,
                "current_replacement_cost": c.current_replacement_cost,
                "photos": comp_photos.get(c.id, []),
            })

        return render_template("study_form.html", prop=prop, defaults=defaults, components=component_rows, edit_mode=True, study=study)

    @app.get("/studies/<int:study_id>/clone")
    @login_required
    def clone_study(study_id: int):
        u = current_user()
        study = _require_owner_study(u, study_id)
        return redirect(url_for("new_study", property_id=study.property_id, clone_from=study.id))

    # --------------------
    # Temp photo upload (BEFORE SAVE)
    # --------------------
    @app.get("/temp/component-photos")
    @login_required
    def list_temp_component_photos():
        u = current_user()
        prop_id = request.args.get("property_id", type=int)
        row_key = (request.args.get("row_key") or "").strip()

        if not prop_id or not row_key:
            return jsonify({"ok": False, "error": "Missing property_id or row_key"}), 400

        _require_owner_property(u, prop_id)

        rows = TempComponentPhoto.query.filter_by(user_id=u.id, property_id=prop_id, row_key=row_key).order_by(TempComponentPhoto.created_at.asc()).all()
        return jsonify({
            "ok": True,
            "photos": [{"id": r.id, "name": (getattr(r, "filename", None) or getattr(r, "original_filename", None) or "photo"), "url": presign_get_url(r.storage_key, expires_seconds=900)} for r in rows],
        })

    @app.post("/temp/component-photo")
    @login_required
    def upload_temp_component_photo():
        u = current_user()
        prop_id = request.form.get("property_id", type=int)
        row_key = (request.form.get("row_key") or "").strip()

        if not prop_id or not row_key:
            return jsonify({"ok": False, "error": "Missing property_id or row_key"}), 400

        prop = _require_owner_property(u, prop_id)

        files: List[Any] = []
        if request.files.getlist("photos"):
            files = request.files.getlist("photos")
        elif request.files.get("photo"):
            files = [request.files.get("photo")]

        if not files:
            return jsonify({"ok": False, "error": "No file uploaded (use field 'photo' or 'photos')"}), 400

        provider = app.config["STORAGE_PROVIDER"]
        bucket = app.config["STORAGE_BUCKET"]

        created = []
        for f in files:
            validated, err = _validate_image_file(f)
            if err:
                return jsonify({"ok": False, "error": err}), 400
            raw, mime = validated

            storage_key = make_storage_key(
                f"users/{u.id}",
                f"properties/{prop.id}",
                "temp/components",
                row_key,
                filename=f.filename,
            )
            put_object_bytes(storage_key, raw, mime)

            data = {
                "user_id": u.id,
                "property_id": prop.id,
                "row_key": row_key,
                "storage_provider": provider,
                "storage_bucket": bucket,
                "storage_key": storage_key,
                "original_filename": f.filename,
                "filename": f.filename,
                "content_type": mime,
                "size_bytes": len(raw),
                "created_at": datetime.utcnow(),
            }
            row = TempComponentPhoto(**_safe_model_kwargs(TempComponentPhoto, data))
            db.session.add(row)
            db.session.flush()

            created.append({"id": row.id, "name": f.filename or "photo", "url": presign_get_url(storage_key, expires_seconds=900)})

        db.session.commit()
        return jsonify({"ok": True, "created": created})

    @app.delete("/temp/component-photo/<int:temp_photo_id>")
    @login_required
    def delete_temp_component_photo(temp_photo_id: int):
        u = current_user()
        row = TempComponentPhoto.query.get_or_404(temp_photo_id)
        if row.user_id != u.id:
            abort(403)

        try:
            delete_object(row.storage_key)
        except Exception:
            pass

        db.session.delete(row)
        db.session.commit()
        return jsonify({"ok": True})

    # --------------------
    # Component photos (AFTER SAVE)
    # --------------------
    @app.get("/components/<int:component_id>/photos")
    @login_required
    def list_component_photos(component_id: int):
        u = current_user()
        comp = ReserveComponent.query.get_or_404(component_id)
        if comp.study.property.user_id != u.id:
            abort(403)

        photos = ComponentPhoto.query.filter_by(component_id=component_id).order_by(ComponentPhoto.created_at.asc()).all()
        return jsonify({
            "ok": True,
            "photos": [{"id": p.id, "name": (getattr(p, "filename", None) or getattr(p, "original_filename", None) or "photo"), "url": presign_get_url(p.storage_key, expires_seconds=900)} for p in photos],
        })

    @app.post("/components/<int:component_id>/photos")
    @login_required
    def upload_component_photo(component_id: int):
        u = current_user()
        comp = ReserveComponent.query.get_or_404(component_id)
        if comp.study.property.user_id != u.id:
            abort(403)
        if _study_is_locked(comp.study):
            return jsonify({"ok": False, "error": "Study is locked after payment."}), 403

        files: List[Any] = []
        if request.files.getlist("photos"):
            files = request.files.getlist("photos")
        elif request.files.get("photo"):
            files = [request.files.get("photo")]

        if not files:
            return jsonify({"ok": False, "error": "No file uploaded (use field 'photo' or 'photos')"}), 400

        provider = app.config["STORAGE_PROVIDER"]
        bucket = app.config["STORAGE_BUCKET"]

        created = []
        for f in files:
            validated, err = _validate_image_file(f)
            if err:
                return jsonify({"ok": False, "error": err}), 400
            raw, mime = validated

            storage_key = make_storage_key(
                f"users/{u.id}",
                f"properties/{comp.study.property_id}",
                f"studies/{comp.study_id}",
                f"components/{comp.id}",
                filename=f.filename,
            )
            put_object_bytes(storage_key, raw, mime)

            data = {
                "component_id": comp.id,
                "storage_provider": provider,
                "storage_bucket": bucket,
                "storage_key": storage_key,
                "original_filename": f.filename,
                "filename": f.filename,
                "content_type": mime,
                "size_bytes": len(raw),
                "created_at": datetime.utcnow(),
            }
            photo = ComponentPhoto(**_safe_model_kwargs(ComponentPhoto, data))
            db.session.add(photo)
            db.session.flush()

            created.append({"id": photo.id, "name": f.filename or "photo", "url": presign_get_url(storage_key, expires_seconds=900)})

        db.session.commit()
        return jsonify({"ok": True, "created": created})

    @app.delete("/components/photos/<int:photo_id>")
    @login_required
    def delete_component_photo(photo_id: int):
        u = current_user()
        photo = ComponentPhoto.query.get_or_404(photo_id)
        comp = ReserveComponent.query.get_or_404(photo.component_id)

        if comp.study.property.user_id != u.id:
            abort(403)
        if _study_is_locked(comp.study):
            return jsonify({"ok": False, "error": "Study is locked after payment."}), 403

        try:
            delete_object(photo.storage_key)
        except Exception:
            pass

        db.session.delete(photo)
        db.session.commit()
        return jsonify({"ok": True})

    # --------------------
    # Create Study (draft)
    # --------------------
    @app.post("/studies/create")
    @login_required
    def create_study():
        u = current_user()

        try:
            prop_id = int(request.form["property_id"])
            prop = Property.query.filter_by(id=prop_id, user_id=u.id).first()
            if not prop:
                abort(403)

            start_year = _parse_int(request.form, "start_year", datetime.utcnow().year)
            horizon_years = _parse_int(request.form, "horizon_years", 30)
            inflation_rate = _parse_float(request.form, "inflation_rate", 0.03)
            interest_rate = _parse_float(request.form, "interest_rate", 0.01)
            starting_balance = _parse_float(request.form, "starting_balance", 0.0)
            min_balance = _parse_float(request.form, "min_balance", 0.0)
            funding_method = (request.form.get("funding_method") or "full").strip()
            contribution_mode = (request.form.get("contribution_mode") or "levelized").strip()

            names = request.form.getlist("component_name[]")
            uls = request.form.getlist("useful_life_years[]")
            rls = request.form.getlist("remaining_life_years[]")
            costs = request.form.getlist("current_replacement_cost[]")
            qtys = request.form.getlist("quantity[]")
            cycles = request.form.getlist("cycle_years[]")
            row_keys = request.form.getlist("row_key[]")

            payload = []
            for i in range(len(names)):
                nm = (names[i] or "").strip()
                if not nm:
                    continue

                ul = int(uls[i]) if str(uls[i]).strip() else 1
                rl = int(rls[i]) if str(rls[i]).strip() else 0
                cost = float(costs[i]) if str(costs[i]).strip() else 0.0
                qty = int(qtys[i]) if i < len(qtys) and str(qtys[i]).strip() else 1
                cyc = int(cycles[i]) if i < len(cycles) and str(cycles[i]).strip() else ul
                rk = (row_keys[i] if i < len(row_keys) else "").strip()

                payload.append({
                    "name": nm,
                    "quantity": max(1, qty),
                    "useful_life_years": max(1, ul),
                    "remaining_life_years": max(0, rl),
                    "cycle_years": max(1, cyc),
                    "current_replacement_cost": max(0.0, cost),
                    "row_key": rk,
                })

            if not payload:
                flash("Please add at least one component.")
                return redirect(url_for("new_study", property_id=prop.id))

            study = ReserveStudy(
                property_id=prop.id,
                start_year=int(start_year),
                horizon_years=int(horizon_years),
                inflation_rate=float(inflation_rate),
                interest_rate=float(interest_rate),
                starting_balance=float(starting_balance),
                min_balance=float(min_balance),
                funding_method=funding_method,
                contribution_mode=contribution_mode,
                recommended_annual_contribution=None,
                paid_status="draft",
            )
            db.session.add(study)
            db.session.flush()

            provider = app.config["STORAGE_PROVIDER"]
            bucket = app.config["STORAGE_BUCKET"]

            for c in payload:
                comp = ReserveComponent(
                    study_id=study.id,
                    name=c["name"],
                    quantity=c["quantity"],
                    useful_life_years=c["useful_life_years"],
                    remaining_life_years=c["remaining_life_years"],
                    cycle_years=c["cycle_years"],
                    current_replacement_cost=c["current_replacement_cost"],
                )
                db.session.add(comp)
                db.session.flush()

                # move temp photos for this row_key → component photos
                if c["row_key"]:
                    temps = TempComponentPhoto.query.filter_by(
                        user_id=u.id,
                        property_id=prop.id,
                        row_key=c["row_key"],
                    ).all()

                    for tp in temps:
                        data = {
                            "component_id": comp.id,
                            "storage_provider": getattr(tp, "storage_provider", None) or provider,
                            "storage_bucket": getattr(tp, "storage_bucket", None) or bucket,
                            "storage_key": tp.storage_key,
                            "original_filename": getattr(tp, "original_filename", None) or getattr(tp, "filename", None),
                            "filename": getattr(tp, "filename", None) or getattr(tp, "original_filename", None),
                            "content_type": getattr(tp, "content_type", None),
                            "size_bytes": getattr(tp, "size_bytes", None),
                            "created_at": datetime.utcnow(),
                        }
                        db.session.add(ComponentPhoto(**_safe_model_kwargs(ComponentPhoto, data)))
                        db.session.delete(tp)

            db.session.commit()
            flash("Draft saved.")
            return redirect(url_for("edit_study", study_id=study.id))

        except Exception as e:
            db.session.rollback()
            flash(f"Error saving draft: {e}")
            return redirect(url_for("home"))

    # --------------------
    # Update draft (edit mode)
    # --------------------
    @app.post("/studies/<int:study_id>/update")
    @login_required
    def update_study(study_id: int):
        u = current_user()
        study = _require_owner_study(u, study_id)
        if _study_is_locked(study):
            flash("This study is locked after payment.")
            return redirect(url_for("study_detail", study_id=study.id))

        try:
            study.start_year = _parse_int(request.form, "start_year", study.start_year)
            study.horizon_years = _parse_int(request.form, "horizon_years", study.horizon_years)
            study.inflation_rate = _parse_float(request.form, "inflation_rate", study.inflation_rate)
            study.interest_rate = _parse_float(request.form, "interest_rate", study.interest_rate)
            study.starting_balance = _parse_float(request.form, "starting_balance", study.starting_balance)
            study.min_balance = _parse_float(request.form, "min_balance", study.min_balance)
            study.funding_method = (request.form.get("funding_method") or study.funding_method or "full").strip()
            study.contribution_mode = (request.form.get("contribution_mode") or study.contribution_mode or "levelized").strip()

            names = request.form.getlist("component_name[]")
            uls = request.form.getlist("useful_life_years[]")
            rls = request.form.getlist("remaining_life_years[]")
            costs = request.form.getlist("current_replacement_cost[]")
            qtys = request.form.getlist("quantity[]")
            cycles = request.form.getlist("cycle_years[]")
            component_ids = request.form.getlist("component_id[]")
            row_keys = request.form.getlist("row_key[]")

            incoming = []
            for i in range(len(names)):
                nm = (names[i] or "").strip()
                if not nm:
                    continue

                ul = int(uls[i]) if str(uls[i]).strip() else 1
                rl = int(rls[i]) if str(rls[i]).strip() else 0
                cost = float(costs[i]) if str(costs[i]).strip() else 0.0
                qty = int(qtys[i]) if i < len(qtys) and str(qtys[i]).strip() else 1
                cyc = int(cycles[i]) if i < len(cycles) and str(cycles[i]).strip() else ul

                cid = (component_ids[i] if i < len(component_ids) else "").strip()
                rk = (row_keys[i] if i < len(row_keys) else "").strip()

                incoming.append({
                    "component_id": int(cid) if cid else None,
                    "row_key": rk,
                    "name": nm,
                    "quantity": max(1, qty),
                    "useful_life_years": max(1, ul),
                    "remaining_life_years": max(0, rl),
                    "cycle_years": max(1, cyc),
                    "current_replacement_cost": max(0.0, cost),
                })

            if not incoming:
                flash("Please keep at least one component.")
                return redirect(url_for("edit_study", study_id=study.id))

            existing = ReserveComponent.query.filter_by(study_id=study.id).all()
            existing_by_id = {c.id: c for c in existing}
            keep_ids = set()

            provider = app.config["STORAGE_PROVIDER"]
            bucket = app.config["STORAGE_BUCKET"]

            for row in incoming:
                if row["component_id"] and row["component_id"] in existing_by_id:
                    c = existing_by_id[row["component_id"]]
                    c.name = row["name"]
                    c.quantity = row["quantity"]
                    c.useful_life_years = row["useful_life_years"]
                    c.remaining_life_years = row["remaining_life_years"]
                    c.cycle_years = row["cycle_years"]
                    c.current_replacement_cost = row["current_replacement_cost"]
                    keep_ids.add(c.id)
                else:
                    c = ReserveComponent(
                        study_id=study.id,
                        name=row["name"],
                        quantity=row["quantity"],
                        useful_life_years=row["useful_life_years"],
                        remaining_life_years=row["remaining_life_years"],
                        cycle_years=row["cycle_years"],
                        current_replacement_cost=row["current_replacement_cost"],
                    )
                    db.session.add(c)
                    db.session.flush()
                    keep_ids.add(c.id)

                    # attach any temp photos from row_key (new rows created during edit)
                    if row["row_key"]:
                        temps = TempComponentPhoto.query.filter_by(
                            user_id=u.id,
                            property_id=study.property_id,
                            row_key=row["row_key"],
                        ).all()
                        for tp in temps:
                            data = {
                                "component_id": c.id,
                                "storage_provider": getattr(tp, "storage_provider", None) or provider,
                                "storage_bucket": getattr(tp, "storage_bucket", None) or bucket,
                                "storage_key": tp.storage_key,
                                "original_filename": getattr(tp, "original_filename", None) or getattr(tp, "filename", None),
                                "filename": getattr(tp, "filename", None) or getattr(tp, "original_filename", None),
                                "content_type": getattr(tp, "content_type", None),
                                "size_bytes": getattr(tp, "size_bytes", None),
                                "created_at": datetime.utcnow(),
                            }
                            db.session.add(ComponentPhoto(**_safe_model_kwargs(ComponentPhoto, data)))
                            db.session.delete(tp)

            for c in existing:
                if c.id not in keep_ids:
                    db.session.delete(c)

            db.session.commit()
            flash("Draft saved.")
            return redirect(url_for("edit_study", study_id=study.id))

        except Exception as e:
            db.session.rollback()
            flash(f"Error saving changes: {e}")
            return redirect(url_for("edit_study", study_id=study.id))

    # --------------------
    # Checkout
    # --------------------
    @app.get("/studies/<int:study_id>/checkout")
    @login_required
    def checkout(study_id: int):
        u = current_user()
        study = _require_owner_study(u, study_id)
        if _study_is_locked(study):
            return redirect(url_for("study_detail", study_id=study.id))
        return render_template("checkout.html", study=study, price_cents=STUDY_PRICE_CENTS)

    @app.post("/studies/<int:study_id>/checkout/simulate-success")
    @login_required
    def checkout_simulate_success(study_id: int):
        u = current_user()
        study = _require_owner_study(u, study_id)
        if _study_is_locked(study):
            return redirect(url_for("study_detail", study_id=study.id))

        components = [{
            "name": c.name,
            "quantity": c.quantity,
            "useful_life_years": c.useful_life_years,
            "remaining_life_years": c.remaining_life_years,
            "cycle_years": c.cycle_years or c.useful_life_years,
            "current_replacement_cost": c.current_replacement_cost,
        } for c in study.components]

        recommended_contrib, yearly = recommend_levelized_full_funding_contribution(
            start_year=study.start_year,
            horizon_years=study.horizon_years,
            inflation_rate=study.inflation_rate,
            interest_rate=study.interest_rate,
            starting_balance=study.starting_balance,
            components=components,
            min_balance=study.min_balance,
        )

        ReserveYearResult.query.filter_by(study_id=study.id).delete()

        for row in yearly:
            kwargs = dict(
                study_id=study.id,
                year=row["year"],
                starting_balance=row["starting_balance"],
                contributions=row["contributions"],
                expenses=row["expenses"],
                interest_earned=row["interest_earned"],
                ending_balance=row["ending_balance"],
                fully_funded_balance=row["fully_funded_balance"],
                percent_funded=row["percent_funded"],
            )
            if hasattr(ReserveYearResult, "recommended_contribution"):
                kwargs["recommended_contribution"] = float(recommended_contrib)

            db.session.add(ReserveYearResult(**kwargs))

        study.recommended_annual_contribution = float(recommended_contrib)
        study.paid_status = "paid"
        db.session.commit()

        flash("Payment successful (simulated). Study is now locked.")
        return redirect(url_for("study_detail", study_id=study.id))

    # --------------------
    # Study Detail
    # --------------------
    @app.get("/studies/<int:study_id>")
    @login_required
    def study_detail(study_id: int):
        u = current_user()
        study = _require_owner_study(u, study_id)

        results = ReserveYearResult.query.filter_by(study_id=study_id).order_by(ReserveYearResult.year.asc()).all()
        components = ReserveComponent.query.filter_by(study_id=study_id).order_by(ReserveComponent.name.asc()).all()

        comp_photos = {}
        for c in components:
            photos = ComponentPhoto.query.filter_by(component_id=c.id).order_by(ComponentPhoto.created_at.asc()).all()
            comp_photos[c.id] = [{"id": p.id, "url": presign_get_url(p.storage_key, expires_seconds=900), "name": (getattr(p, "filename", None) or getattr(p, "original_filename", None) or "photo")} for p in photos]

        return render_template("study_detail.html", study=study, results=results, components=components, comp_photos=comp_photos)

    # --------------------
    # Download CSV (only after paid)
    # --------------------
    @app.get("/studies/<int:study_id>/download.csv")
    @login_required
    def download_study_csv(study_id: int):
        u = current_user()
        study = _require_owner_study(u, study_id)

        if not _study_is_locked(study):
            flash("Please complete checkout before downloading a report.")
            return redirect(url_for("checkout", study_id=study.id))

        results = ReserveYearResult.query.filter_by(study_id=study_id).order_by(ReserveYearResult.year.asc()).all()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["property", study.property.name])
        writer.writerow(["study_id", study.id])
        writer.writerow(["start_year", study.start_year])
        writer.writerow(["horizon_years", study.horizon_years])
        writer.writerow(["inflation_rate", study.inflation_rate])
        writer.writerow(["interest_rate", study.interest_rate])
        writer.writerow(["starting_balance", f"{study.starting_balance:.2f}"])
        writer.writerow(["min_balance", f"{study.min_balance:.2f}"])
        writer.writerow(["funding_method", study.funding_method])
        writer.writerow(["contribution_mode", study.contribution_mode])
        writer.writerow(["recommended_annual_contribution", f"{(study.recommended_annual_contribution or 0.0):.2f}"])
        writer.writerow([])

        writer.writerow(["Components"])
        writer.writerow(["name", "qty", "useful_life_years", "remaining_life_years", "cycle_years", "replacement_cost_today"])
        for c in study.components:
            writer.writerow([c.name, c.quantity, c.useful_life_years, c.remaining_life_years, c.cycle_years, f"{c.current_replacement_cost:.2f}"])

        writer.writerow([])
        writer.writerow(["Year-by-year results"])
        writer.writerow(["year", "start", "contrib", "expenses", "interest", "end", "ffb", "percent_funded"])

        for r in results:
            writer.writerow([
                r.year,
                f"{r.starting_balance:.2f}",
                f"{r.contributions:.2f}",
                f"{r.expenses:.2f}",
                f"{r.interest_earned:.2f}",
                f"{r.ending_balance:.2f}",
                f"{r.fully_funded_balance:.2f}",
                f"{r.percent_funded:.6f}",
            ])

        mem = io.BytesIO(output.getvalue().encode("utf-8"))
        filename = f"reserve_study_{study.id}_{study.property.name.replace(' ', '_')}.csv"
        return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)

    return app


app = create_app()

if __name__ == "__main__":
    app.config["SESSION_COOKIE_SECURE"] = False  # local only
    app.run(host="127.0.0.1", port=5050, debug=True)































