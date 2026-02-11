# app.py
import os
import io
import csv
from functools import wraps
from datetime import datetime, timedelta
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
    AuditLog,
    User,
    Property,
    ReserveStudy,
    ReserveComponent,
    ComponentPhoto,
    TempComponentPhoto,
    ReserveYearResult,
    PremiumRequest,
)

from reserve_math import recommend_levelized_full_funding_contribution
from storage import put_object_bytes, delete_object, presign_get_url, make_storage_key

ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}

TIER_PRICES_CENTS = {
    "essentials": 50000,
    "plus": 170000,
    "premium": 300000,
}


# --------------------
# Helpers
# --------------------
def _db_uri() -> str:
    uri = os.getenv("DATABASE_URL")
    if not uri:
        raise RuntimeError("DATABASE_URL is not set.")
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    return uri


def _storage_provider() -> str:
    return (os.getenv("STORAGE_PROVIDER") or "s3").strip()


def _storage_bucket() -> str:
    b = (os.getenv("STORAGE_BUCKET") or "").strip()
    if not b:
        raise RuntimeError("STORAGE_BUCKET is not set (required for photo rows).")
    return b


def _safe_model_kwargs(model_cls, data: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in data.items():
        if hasattr(model_cls, k):
            out[k] = v
    return out


def _admin_emails_set() -> set:
    raw = (os.getenv("ADMIN_EMAILS") or "").strip()
    if not raw:
        return set()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


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


def current_user() -> Optional[User]:
    return _session_user()


def is_admin(u: Optional[User]) -> bool:
    return bool(u and getattr(u, "is_admin", False))


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _session_user()
        if not user:
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def _require_property_access(u: User, prop_id: int) -> Property:
    if is_admin(u):
        return Property.query.get_or_404(prop_id)
    prop = Property.query.filter_by(id=prop_id, user_id=u.id).first()
    if not prop:
        abort(404)
    return prop


def _require_study_access(u: User, study_id: int) -> ReserveStudy:
    study = ReserveStudy.query.get_or_404(study_id)
    if is_admin(u):
        return study
    if not study.property or study.property.user_id != u.id:
        abort(403)
    return study


def _study_locked_for_user(u: User, study: ReserveStudy) -> bool:
    # Admins can always edit anything.
    if is_admin(u):
        return False
    # Users locked once workflow is not draft
    ws = (study.workflow_status or "").lower()
    if ws and ws != "draft":
        return True
    # fallback legacy
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


def _audit_enabled_for_study_actor(study: Optional[ReserveStudy], actor: Optional[User]) -> bool:
    # Log customer edits for Essentials/Plus, skip premium; also skip admin edits
    if not study or not actor:
        return False
    if is_admin(actor):
        return False
    tier = (study.tier or "").lower()
    return tier in ("essentials", "plus")


def log_audit(actor: Optional[User], action: str, entity_type: str, entity_id: Optional[int], meta: Optional[dict] = None):
    try:
        al = AuditLog(
            actor_user_id=int(actor.id) if actor else None,
            actor_email=(actor.email if actor else None),
            action=action,
            entity_type=entity_type,
            entity_id=int(entity_id) if entity_id is not None else None,
            meta=meta or None,
        )
        db.session.add(al)
    except Exception:
        pass


def _tier_price_cents(tier: str) -> int:
    t = (tier or "").strip().lower()
    return int(TIER_PRICES_CENTS.get(t, 0))


def _tier_label(tier: str) -> str:
    t = (tier or "").strip().lower()
    if t == "plus":
        return "Plus"
    if t == "premium":
        return "Premium"
    return "Essentials"


def _pretty_money(cents: int) -> str:
    return f"${cents/100:,.0f}"


def _load_components_and_photos(study_id: int):
    components = ReserveComponent.query.filter_by(study_id=study_id).order_by(ReserveComponent.name.asc()).all()
    comp_photos = {}
    for c in components:
        photos = ComponentPhoto.query.filter_by(component_id=c.id).order_by(ComponentPhoto.created_at.asc()).all()
        comp_photos[c.id] = [{
            "id": p.id,
            "url": presign_get_url(p.storage_key, expires_seconds=900),
            "name": (getattr(p, "filename", None) or getattr(p, "original_filename", None) or "photo")
        } for p in photos]
    return components, comp_photos


# NEW: statuses that indicate an admin needs to do something
ADMIN_NEEDS_REVIEW_STATUSES = {
    "paid_awaiting_review",        # Plus paid, waiting for expert/admin review/approve
    "paid_pending_admin_build",    # Premium scheduled/paid, waiting for admin build + approve
}


# --------------------
# App factory
# --------------------
def create_app():
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

    app.config["SQLALCHEMY_DATABASE_URI"] = _db_uri().replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = True  # keep True on Render/HTTPS

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
    # Public marketing home (NO LOGIN)
    # --------------------
    @app.get("/")
    def kdogs_home():
        if session.get("user_id") and _session_user():
            return redirect(url_for("home"))
        return render_template("KDogsHomePage.html")

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

        admin_emails = _admin_emails_set()
        make_admin = email in admin_emails

        u = User(email=email, password_hash=generate_password_hash(password), is_admin=make_admin)
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
        next_url = (request.form.get("next_url") or "").strip() or url_for("home")

        u = User.query.filter_by(email=email).first()
        if u and check_password_hash(u.password_hash, password):
            admin_emails = _admin_emails_set()
            if email in admin_emails and not u.is_admin:
                u.is_admin = True
                db.session.commit()

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
    # Home / Properties (LOGGED IN)
    # --------------------
    @app.get("/home")
    @login_required
    def home():
        """
        FIX:
        - home.html loops `properties_view`.
        - Your non-admin path used to pass `properties=props`, so the template rendered nothing.
        - Now both admin and non-admin always pass `properties_view`.
        """
        u = current_user()

        if is_admin(u):
            props = Property.query.order_by(Property.created_at.desc()).all()

            # Count "needs review" studies by property so we can show a column + float to top.
            rows = (
                db.session.query(ReserveStudy.property_id, db.func.count(ReserveStudy.id))
                .filter(ReserveStudy.workflow_status.in_(list(ADMIN_NEEDS_REVIEW_STATUSES)))
                .group_by(ReserveStudy.property_id)
                .all()
            )
            pending_map = {int(pid): int(cnt) for pid, cnt in rows}

            properties_view = []
            for p in props:
                cnt = pending_map.get(int(p.id), 0)
                properties_view.append({
                    "prop": p,
                    "needs_review": cnt > 0,
                    "pending_count": cnt,
                })

            # float needs_review to the top, then newest
            properties_view.sort(
                key=lambda x: (
                    0 if x["needs_review"] else 1,
                    -(x["prop"].created_at.timestamp() if x["prop"].created_at else 0),
                )
            )

            return render_template("home.html", properties_view=properties_view, is_admin=True)

        # Non-admin: build the same properties_view structure so template works
        props = Property.query.filter_by(user_id=u.id).order_by(Property.created_at.desc()).all()
        properties_view = [{
            "prop": p,
            "needs_review": False,
            "pending_count": 0,
        } for p in props]

        return render_template("home.html", properties_view=properties_view, is_admin=False)

    @app.post("/properties/create")
    @login_required
    def create_property():
        u = current_user()
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
        prop = _require_property_access(u, property_id)

        studies = (
            ReserveStudy.query.filter_by(property_id=prop.id)
            .order_by(ReserveStudy.created_at.desc())
            .all()
        )

        completed_studies = []
        draft_studies = []
        pending_review = []
        premium_pending = []

        for s in studies:
            ws = (s.workflow_status or "").lower()
            if ws == "draft":
                draft_studies.append(s)
            elif ws == "paid_awaiting_review":
                pending_review.append(s)
            elif ws == "paid_pending_admin_build":
                premium_pending.append(s)
            else:
                completed_studies.append(s)

        # premium requests (for UI messaging)
        if is_admin(u):
            premium_reqs = PremiumRequest.query.filter_by(property_id=prop.id).order_by(PremiumRequest.created_at.desc()).all()
        else:
            premium_reqs = PremiumRequest.query.filter_by(property_id=prop.id, user_id=u.id).order_by(PremiumRequest.created_at.desc()).all()

        return render_template(
            "property.html",
            prop=prop,
            completed_studies=completed_studies,
            draft_studies=draft_studies,
            pending_review=pending_review,
            premium_pending=premium_pending,
            premium_reqs=premium_reqs,
            is_admin=is_admin(u),  # IMPORTANT: template needs this for Admin tools
            tier_prices={
                "essentials": _pretty_money(_tier_price_cents("essentials")),
                "plus": _pretty_money(_tier_price_cents("plus")),
                "premium": _pretty_money(_tier_price_cents("premium")),
            }
        )

    # --------------------
    # Admin: create PREMIUM shell study (from scratch) ONLY
    # --------------------
    @app.post("/admin/studies/create-shell")
    @login_required
    def admin_create_shell_study():
        u = current_user()
        if not is_admin(u):
            abort(403)

        prop_id = request.form.get("property_id", type=int)
        tier = (request.form.get("tier") or "premium").strip().lower()

        if not prop_id:
            abort(400)

        # PREMIUM ONLY — admin "from scratch" is not allowed for Essentials/Plus
        if tier != "premium":
            flash("Only Premium is created from scratch by admins.")
            return redirect(url_for("property_page", property_id=prop_id))

        prop = _require_property_access(u, prop_id)

        now_year = datetime.utcnow().year

        # Premium shell: pending admin build (customer sees pending page until approved)
        study = ReserveStudy(
            property_id=prop.id,
            tier="premium",
            workflow_status="paid_pending_admin_build",
            paid_status="paid",
            start_year=now_year,
            horizon_years=30,
            inflation_rate=0.03,
            interest_rate=0.01,
            starting_balance=0.0,
            min_balance=0.0,
            funding_method="full",
            contribution_mode="levelized",
            recommended_annual_contribution=None,
        )
        db.session.add(study)
        db.session.commit()

        flash("Created Premium study shell. Fill it out and approve when ready.")
        return redirect(url_for("edit_study", study_id=study.id))

    # --------------------
    # Study routes: new, edit, clone
    # --------------------
    @app.get("/studies/new")
    @login_required
    def new_study():
        u = current_user()
        property_id = request.args.get("property_id", type=int)
        tier = (request.args.get("tier") or "essentials").strip().lower()

        if tier not in ("essentials", "plus"):
            flash("Premium requests use the Premium button on the property page.")
            return redirect(url_for("property_page", property_id=property_id or 0))

        prop = _require_property_access(u, property_id) if property_id else None
        if not prop:
            flash("Choose a property first.")
            return redirect(url_for("home"))

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

        components = [{
            "id": "",
            "row_key": "",
            "name": "",
            "quantity": "",
            "useful_life_years": "",
            "remaining_life_years": "",
            "cycle_years": "",
            "current_replacement_cost": "",
            "photos": []
        }]

        return render_template(
            "study_form.html",
            prop=prop,
            defaults=defaults,
            components=components,
            edit_mode=False,
            study=None,
            tier=tier,
            is_locked=False,
            tier_price=_pretty_money(_tier_price_cents(tier)),
            # New: templates can use this to decide whether to show approve controls
            admin_can_approve=False,
            admin_approve_url=None,
        )

    @app.get("/studies/<int:study_id>/edit")
    @login_required
    def edit_study(study_id: int):
        u = current_user()
        study = _require_study_access(u, study_id)

        if _study_locked_for_user(u, study):
            flash("This study is locked for editing.")
            return redirect(url_for("study_detail", study_id=study.id))

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

        if not component_rows:
            component_rows = [{
                "id": "",
                "row_key": "",
                "name": "",
                "quantity": "",
                "useful_life_years": "",
                "remaining_life_years": "",
                "cycle_years": "",
                "current_replacement_cost": "",
                "photos": []
            }]

        ws = (study.workflow_status or "").lower()
        tier = (study.tier or "essentials").lower()

        # New: if admin editing a paid Plus/Premium that isn't approved, allow "Complete & Approve"
        admin_can_approve = bool(
            is_admin(u)
            and tier in ("plus", "premium")
            and ws in ("paid_awaiting_review", "paid_pending_admin_build")
        )

        return render_template(
            "study_form.html",
            prop=prop,
            defaults=defaults,
            components=component_rows,
            edit_mode=True,
            study=study,
            tier=(study.tier or "essentials"),
            is_locked=_study_locked_for_user(u, study),
            tier_price=_pretty_money(_tier_price_cents(study.tier or "essentials")),
            admin_can_approve=admin_can_approve,
            admin_approve_url=(url_for("admin_approve", study_id=study.id) if admin_can_approve else None),
        )

    @app.get("/studies/<int:study_id>/clone")
    @login_required
    def clone_study(study_id: int):
        u = current_user()
        study = _require_study_access(u, study_id)
        if not study.property:
            abort(404)
        return redirect(url_for("new_study", property_id=study.property_id, tier=(study.tier or "essentials")))

    # --------------------
    # Premium: pay -> schedule -> shell study created
    # --------------------
    @app.post("/premium/start")
    @login_required
    def premium_start():
        u = current_user()
        prop_id = request.form.get("property_id", type=int)
        if not prop_id:
            abort(400)
        prop = _require_property_access(u, prop_id)

        pr = PremiumRequest(
            user_id=u.id,
            property_id=prop.id,
            paid=False,
            paid_amount_cents=_tier_price_cents("premium"),
            status="created",
            meta=None,
        )
        db.session.add(pr)
        db.session.commit()

        return redirect(url_for("premium_checkout", premium_request_id=pr.id))

    @app.get("/premium/<int:premium_request_id>/checkout")
    @login_required
    def premium_checkout(premium_request_id: int):
        u = current_user()
        pr = PremiumRequest.query.get_or_404(premium_request_id)
        if not is_admin(u) and pr.user_id != u.id:
            abort(403)

        return render_template(
            "premium_checkout.html",
            pr=pr,
            price_cents=_tier_price_cents("premium"),
            price_label=_pretty_money(_tier_price_cents("premium")),
        )

    @app.post("/premium/<int:premium_request_id>/checkout/simulate-success")
    @login_required
    def premium_checkout_simulate_success(premium_request_id: int):
        u = current_user()
        pr = PremiumRequest.query.get_or_404(premium_request_id)
        if not is_admin(u) and pr.user_id != u.id:
            abort(403)

        pr.paid = True
        pr.status = "paid_pending_schedule"
        pr.paid_amount_cents = _tier_price_cents("premium")
        db.session.commit()

        flash("Payment successful (simulated). Please choose an on-site inspection time.")
        return redirect(url_for("premium_schedule", premium_request_id=pr.id))

    @app.get("/premium/<int:premium_request_id>/schedule")
    @login_required
    def premium_schedule(premium_request_id: int):
        u = current_user()
        pr = PremiumRequest.query.get_or_404(premium_request_id)
        if not is_admin(u) and pr.user_id != u.id:
            abort(403)
        if not pr.paid:
            flash("Please complete payment first.")
            return redirect(url_for("premium_checkout", premium_request_id=pr.id))

        options = []
        base = datetime.utcnow()
        d = base
        while len(options) < 10:
            d += timedelta(days=1)
            if d.weekday() in (5, 6):  # skip Sat/Sun
                continue
            opt = datetime(d.year, d.month, d.day, 17, 0)  # 5pm UTC-ish
            options.append(opt)
        return render_template("premium_schedule.html", pr=pr, options=options)

    @app.post("/premium/<int:premium_request_id>/schedule")
    @login_required
    def premium_schedule_post(premium_request_id: int):
        u = current_user()
        pr = PremiumRequest.query.get_or_404(premium_request_id)
        if not is_admin(u) and pr.user_id != u.id:
            abort(403)
        if not pr.paid:
            flash("Please complete payment first.")
            return redirect(url_for("premium_checkout", premium_request_id=pr.id))

        when_raw = (request.form.get("scheduled_at") or "").strip()
        if not when_raw:
            flash("Please select a time.")
            return redirect(url_for("premium_schedule", premium_request_id=pr.id))

        try:
            scheduled_at = datetime.fromisoformat(when_raw.replace("Z", "+00:00"))
        except Exception:
            flash("Invalid date format.")
            return redirect(url_for("premium_schedule", premium_request_id=pr.id))

        pr.scheduled_at = scheduled_at
        pr.status = "scheduled"

        # Create shell study for admins to build
        now_year = datetime.utcnow().year
        study = ReserveStudy(
            property_id=pr.property_id,
            tier="premium",
            workflow_status="paid_pending_admin_build",
            paid_status="paid",
            start_year=now_year,
            horizon_years=30,
            inflation_rate=0.03,
            interest_rate=0.01,
            starting_balance=0.0,
            min_balance=0.0,
            funding_method="full",
            contribution_mode="levelized",
            recommended_annual_contribution=None,
        )
        db.session.add(study)
        db.session.flush()

        pr.meta = {"study_id": int(study.id)}
        db.session.commit()

        flash("Premium request scheduled. Our team will build your study and it will appear in your account when ready.")
        return redirect(url_for("property_page", property_id=pr.property_id))

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

        _require_property_access(u, prop_id)

        rows = TempComponentPhoto.query.filter_by(user_id=u.id, property_id=prop_id, row_key=row_key).order_by(TempComponentPhoto.created_at.asc()).all()
        return jsonify({
            "ok": True,
            "photos": [{"id": r.id, "name": (getattr(r, "filename", None) or "photo"), "url": presign_get_url(r.storage_key, expires_seconds=900)} for r in rows],
        })

    @app.post("/temp/component-photo")
    @login_required
    def upload_temp_component_photo():
        u = current_user()
        prop_id = request.form.get("property_id", type=int)
        row_key = (request.form.get("row_key") or "").strip()

        if not prop_id or not row_key:
            return jsonify({"ok": False, "error": "Missing property_id or row_key"}), 400

        prop = _require_property_access(u, prop_id)

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
                "filename": f.filename,
                "content_type": mime,
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
        if row.user_id != u.id and not is_admin(u):
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
        study = comp.study
        if not study or not study.property:
            abort(404)

        if not is_admin(u) and study.property.user_id != u.id:
            abort(403)

        photos = ComponentPhoto.query.filter_by(component_id=component_id).order_by(ComponentPhoto.created_at.asc()).all()
        return jsonify({
            "ok": True,
            "photos": [{"id": p.id, "name": (getattr(p, "filename", None) or "photo"), "url": presign_get_url(p.storage_key, expires_seconds=900)} for p in photos],
        })

    @app.post("/components/<int:component_id>/photos")
    @login_required
    def upload_component_photo(component_id: int):
        u = current_user()
        comp = ReserveComponent.query.get_or_404(component_id)
        study = comp.study
        if not study or not study.property:
            abort(404)

        if not is_admin(u) and study.property.user_id != u.id:
            abort(403)

        if _study_locked_for_user(u, study):
            return jsonify({"ok": False, "error": "Study is locked for editing."}), 403

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
                f"users/{study.property.user_id}",
                f"properties/{study.property_id}",
                f"studies/{study.id}",
                f"components/{comp.id}",
                filename=f.filename,
            )
            put_object_bytes(storage_key, raw, mime)

            data = {
                "component_id": comp.id,
                "storage_provider": provider,
                "storage_bucket": bucket,
                "storage_key": storage_key,
                "filename": f.filename,
                "content_type": mime,
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
        study = comp.study
        if not study or not study.property:
            abort(404)

        if not is_admin(u) and study.property.user_id != u.id:
            abort(403)

        if _study_locked_for_user(u, study):
            return jsonify({"ok": False, "error": "Study is locked for editing."}), 403

        try:
            delete_object(photo.storage_key)
        except Exception:
            pass

        db.session.delete(photo)
        db.session.commit()
        return jsonify({"ok": True})

    # --------------------
    # Create Study (draft) - Essentials/Plus only
    # --------------------
    @app.post("/studies/create")
    @login_required
    def create_study():
        u = current_user()

        try:
            prop_id = int(request.form["property_id"])
            prop = _require_property_access(u, prop_id)

            tier = (request.form.get("tier") or "essentials").strip().lower()
            if tier not in ("essentials", "plus"):
                flash("Invalid tier for self-service study.")
                return redirect(url_for("property_page", property_id=prop.id))

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
                return redirect(url_for("new_study", property_id=prop.id, tier=tier))

            study = ReserveStudy(
                property_id=prop.id,
                tier=tier,
                workflow_status="draft",
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
                            "filename": getattr(tp, "filename", None),
                            "content_type": getattr(tp, "content_type", None),
                            "created_at": datetime.utcnow(),
                        }
                        db.session.add(ComponentPhoto(**_safe_model_kwargs(ComponentPhoto, data)))
                        db.session.delete(tp)

            if _audit_enabled_for_study_actor(study, u):
                log_audit(u, "user_save_draft", "reserve_study", study.id, meta={
                    "tier": tier,
                    "study_fields": {
                        "start_year": start_year,
                        "horizon_years": horizon_years,
                        "inflation_rate": inflation_rate,
                        "interest_rate": interest_rate,
                        "starting_balance": starting_balance,
                        "min_balance": min_balance,
                        "funding_method": funding_method,
                        "contribution_mode": contribution_mode,
                    },
                    "components": payload,
                })

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
        study = _require_study_access(u, study_id)

        if _study_locked_for_user(u, study):
            flash("This study is locked for editing.")
            return redirect(url_for("study_detail", study_id=study.id))

        try:
            before = {
                "start_year": study.start_year,
                "horizon_years": study.horizon_years,
                "inflation_rate": study.inflation_rate,
                "interest_rate": study.interest_rate,
                "starting_balance": study.starting_balance,
                "min_balance": study.min_balance,
                "funding_method": study.funding_method,
                "contribution_mode": study.contribution_mode,
            }

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
                cyc = int(cycles[i]) if str(cycles[i]).strip() else ul

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
                                "filename": getattr(tp, "filename", None),
                                "content_type": getattr(tp, "content_type", None),
                                "created_at": datetime.utcnow(),
                            }
                            db.session.add(ComponentPhoto(**_safe_model_kwargs(ComponentPhoto, data)))
                            db.session.delete(tp)

            for c in existing:
                if c.id not in keep_ids:
                    db.session.delete(c)

            if _audit_enabled_for_study_actor(study, u):
                log_audit(u, "user_update_draft", "reserve_study", study.id, meta={
                    "tier": (study.tier or "").lower(),
                    "before": before,
                    "after": {
                        "start_year": study.start_year,
                        "horizon_years": study.horizon_years,
                        "inflation_rate": study.inflation_rate,
                        "interest_rate": study.interest_rate,
                        "starting_balance": study.starting_balance,
                        "min_balance": study.min_balance,
                        "funding_method": study.funding_method,
                        "contribution_mode": study.contribution_mode,
                    },
                    "components": incoming,
                })

            db.session.commit()
            flash("Draft saved.")
            return redirect(url_for("edit_study", study_id=study.id))

        except Exception as e:
            db.session.rollback()
            flash(f"Error saving changes: {e}")
            return redirect(url_for("edit_study", study_id=study.id))

    # --------------------
    # Checkout (Essentials/Plus)
    # --------------------
    @app.get("/studies/<int:study_id>/checkout")
    @login_required
    def checkout(study_id: int):
        u = current_user()
        study = _require_study_access(u, study_id)

        if _study_locked_for_user(u, study):
            return redirect(url_for("study_detail", study_id=study.id))

        tier = (study.tier or "").lower()
        if tier not in ("essentials", "plus"):
            flash("Premium requests are handled separately.")
            return redirect(url_for("property_page", property_id=study.property_id))

        price_cents = _tier_price_cents(tier)
        return render_template(
            "checkout.html",
            study=study,
            price_cents=price_cents,
            price_label=_pretty_money(price_cents),
            tier_label=_tier_label(tier),
        )

    @app.post("/studies/<int:study_id>/checkout/simulate-success")
    @login_required
    def checkout_simulate_success(study_id: int):
        u = current_user()
        study = _require_study_access(u, study_id)

        if _study_locked_for_user(u, study):
            return redirect(url_for("study_detail", study_id=study.id))

        tier = (study.tier or "").lower()
        if tier not in ("essentials", "plus"):
            flash("Premium requests are handled separately.")
            return redirect(url_for("property_page", property_id=study.property_id))

        components = [{
            "name": c.name,
            "quantity": c.quantity,
            "useful_life_years": c.useful_life_years,
            "remaining_life_years": c.remaining_life_years,
            "cycle_years": c.cycle_years or c.useful_life_years,
            "current_replacement_cost": c.current_replacement_cost,
        } for c in study.components]

        recommended_contrib = None
        yearly = []

        if components:
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
                db.session.add(ReserveYearResult(**kwargs))

            study.recommended_annual_contribution = float(recommended_contrib)

        study.paid_status = "paid"
        if tier == "essentials":
            study.workflow_status = "paid_final"
        else:
            study.workflow_status = "paid_awaiting_review"

        if _audit_enabled_for_study_actor(study, u):
            log_audit(u, "user_checkout_paid", "reserve_study", study.id, meta={
                "tier": tier,
                "price_cents": _tier_price_cents(tier),
                "workflow_status": study.workflow_status,
            })

        db.session.commit()

        if tier == "plus":
            flash("Payment successful (simulated). You can review your inputs while our team completes the report.")
        else:
            flash("Payment successful (simulated). Study is now locked.")

        return redirect(url_for("study_detail", study_id=study.id))

    # --------------------
    # Admin approve (Plus + Premium when done)
    # --------------------
    @app.post("/studies/<int:study_id>/admin/approve")
    @login_required
    def admin_approve(study_id: int):
        u = current_user()
        if not is_admin(u):
            abort(403)

        study = _require_study_access(u, study_id)
        study.workflow_status = "approved_final"
        study.paid_status = "paid"
        db.session.commit()

        flash("Study approved and published.")
        return redirect(url_for("study_detail", study_id=study.id))

    # --------------------
    # Study Detail
    # --------------------
    @app.get("/studies/<int:study_id>")
    @login_required
    def study_detail(study_id: int):
        u = current_user()
        study = _require_study_access(u, study_id)

        tier = (study.tier or "").lower()
        ws = (study.workflow_status or "").lower()

        # Always load submitted inputs for snapshot views (Plus/Premium pending)
        components, comp_photos = _load_components_and_photos(study_id)

        # PLUS: customers see pending-only until approved (and see their submitted inputs below)
        if tier == "plus" and (not is_admin(u)) and ws != "approved_final":
            return render_template(
                "study_pending_review.html",
                study=study,
                components=components,
                comp_photos=comp_photos,
                pending_title="Waiting on Expert Approval",
                pending_body="Your Plus request has been submitted. Our expert is reviewing it now. You’ll see the final report here once it’s approved.",
            )

        # PREMIUM: customers see pending-only until approved
        if tier == "premium" and (not is_admin(u)) and ws != "approved_final":
            return render_template(
                "study_premium_pending.html",
                study=study,
                components=components,
                comp_photos=comp_photos,
                pending_title="We’re Building Your Premium Report",
                pending_body="Your Premium request is in progress. Our team will build and approve your report, then it will appear here automatically.",
            )

        results = ReserveYearResult.query.filter_by(study_id=study_id).order_by(ReserveYearResult.year.asc()).all()

        # New: admin CTA availability (templates can show a "Complete & Approve" button)
        admin_can_approve = bool(
            is_admin(u)
            and tier in ("plus", "premium")
            and ws in ("paid_awaiting_review", "paid_pending_admin_build")
        )

        return render_template(
            "study_detail.html",
            study=study,
            results=results,
            components=components,
            comp_photos=comp_photos,
            admin_can_approve=admin_can_approve,
            admin_approve_url=(url_for("admin_approve", study_id=study.id) if admin_can_approve else None),
        )

    # --------------------
    # Download CSV
    # --------------------
    @app.get("/studies/<int:study_id>/download.csv")
    @login_required
    def download_study_csv(study_id: int):
        u = current_user()
        study = _require_study_access(u, study_id)

        tier = (study.tier or "").lower()
        ws = (study.workflow_status or "").lower()

        if tier in ("plus", "premium") and (not is_admin(u)) and ws != "approved_final":
            flash("Your report will be available after our team finishes and approves it.")
            return redirect(url_for("study_detail", study_id=study.id))

        if not study.is_paid:
            flash("Please complete checkout before downloading a report.")
            return redirect(url_for("checkout", study_id=study.id))

        results = ReserveYearResult.query.filter_by(study_id=study_id).order_by(ReserveYearResult.year.asc()).all()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["property", study.property.name])
        writer.writerow(["study_id", study.id])
        writer.writerow(["tier", study.tier])
        writer.writerow(["workflow_status", study.workflow_status])
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











































