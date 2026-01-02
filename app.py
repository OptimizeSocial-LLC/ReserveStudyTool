# app.py
import os
import io
import csv
from functools import wraps
from datetime import datetime

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
    session,
)

from models import db, Property, ReserveStudy, ReserveComponent, ReserveYearResult
from reserve_math import recommend_levelized_full_funding_contribution

from dotenv import load_dotenv
load_dotenv()


def _sqlite_uri(app: Flask) -> str:
    """
    Uses persistent disk on Render if available:
      - Render persistent disk typically mounted at /var/data
      - Local dev uses Flask instance folder (absolute path)
    You can override with SQLITE_PATH env var.
    """
    explicit = os.getenv("SQLITE_PATH")
    if explicit:
        # absolute path recommended
        if explicit.startswith("/"):
            return f"sqlite:///{explicit}"
        # relative path if you insist
        return f"sqlite:///{explicit}"

    # Render persistent disk
    if os.path.isdir("/var/data"):
        return "sqlite:////var/data/app.db"

    # Local: Flask instance path is the correct writable place.
    os.makedirs(app.instance_path, exist_ok=True)
    db_path = os.path.join(app.instance_path, "app.db")
    return f"sqlite:///{os.path.abspath(db_path)}"


# --------------------
# Simple password auth (company-wide)
# --------------------
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("logged_in") is True:
            return fn(*args, **kwargs)
        return redirect(url_for("login", next=request.path))

    return wrapper


def create_app():
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

    # Always use SQLite (Render disk if present, otherwise instance/app.db absolute)
    app.config["SQLALCHEMY_DATABASE_URI"] = _sqlite_uri(app)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # ✅ Read the SAME env var names you keep in .env
    # (Your .env uses SITE_USERNAME / SITE_PASSWORD)
    app.config["APP_USERNAME"] = os.getenv("SITE_USERNAME", "admin")
    app.config["APP_PASSWORD"] = os.getenv("SITE_PASSWORD", "change-me")

    db.init_app(app)

    # Ensure tables exist
    with app.app_context():
        db.create_all()
        print("DB URI:", app.config["SQLALCHEMY_DATABASE_URI"])
        print("Auth username loaded as:", app.config["APP_USERNAME"])  # quick sanity check

    # --------------------
    # Auth routes
    # --------------------
    @app.get("/login")
    def login():
        if session.get("logged_in"):
            return redirect(url_for("home"))
        next_url = request.args.get("next") or url_for("home")
        return render_template("login.html", next_url=next_url)

    @app.post("/login")
    def login_post():
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        next_url = request.form.get("next_url") or url_for("home")

        if username == app.config["APP_USERNAME"] and password == app.config["APP_PASSWORD"]:
            session["logged_in"] = True
            return redirect(next_url)

        flash("Invalid username or password.")
        return redirect(url_for("login", next=next_url))

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # --------------------
    # Home / Properties
    # --------------------
    @app.get("/")
    @login_required
    def home():
        properties = Property.query.order_by(Property.created_at.desc()).all()
        return render_template("home.html", properties=properties)

    @app.post("/properties/create")
    @login_required
    def create_property():
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Property name is required.")
            return redirect(url_for("home"))

        prop = Property(
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
        prop = Property.query.get_or_404(property_id)
        studies = (
            ReserveStudy.query.filter_by(property_id=property_id)
            .order_by(ReserveStudy.created_at.desc())
            .all()
        )
        return render_template("property.html", prop=prop, studies=studies)

    # --------------------
    # New / Clone Study
    # --------------------
    @app.get("/studies/new")
    @login_required
    def new_study():
        property_id = request.args.get("property_id", type=int)
        clone_from = request.args.get("clone_from", type=int)

        prop = Property.query.get_or_404(property_id) if property_id else None
        clone = ReserveStudy.query.get(clone_from) if clone_from else None

        defaults = {
            "start_year": datetime.utcnow().year,
            "horizon_years": 30,
            "inflation_rate": 0.03,
            "interest_rate": 0.01,
            "starting_balance": 50000,
            "min_balance": 0,
            "funding_method": "full",
            "contribution_mode": "levelized",
        }

        components = [
            {
                "name": "Roof",
                "quantity": 1,
                "useful_life_years": 25,
                "remaining_life_years": 8,
                "cycle_years": 25,
                "current_replacement_cost": 180000,
            },
            {
                "name": "Exterior Paint",
                "quantity": 1,
                "useful_life_years": 10,
                "remaining_life_years": 3,
                "cycle_years": 10,
                "current_replacement_cost": 45000,
            },
            {
                "name": "Paving",
                "quantity": 1,
                "useful_life_years": 20,
                "remaining_life_years": 12,
                "cycle_years": 20,
                "current_replacement_cost": 90000,
            },
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
            components = [
                {
                    "name": c.name,
                    "quantity": c.quantity or 1,
                    "useful_life_years": c.useful_life_years,
                    "remaining_life_years": c.remaining_life_years,
                    "cycle_years": (c.cycle_years or 0) or c.useful_life_years,
                    "current_replacement_cost": c.current_replacement_cost,
                }
                for c in clone.components
            ]
            if not prop:
                prop = clone.property

        if not prop:
            flash("Choose a property first.")
            return redirect(url_for("home"))

        return render_template(
            "study_form.html",
            prop=prop,
            defaults=defaults,
            components=components,
            clone=clone,
            edit_mode=False,
            study=None,
        )

    # --------------------
    # Create Study
    # --------------------
    @app.post("/studies/create")
    @login_required
    def create_study():
        try:
            property_id = int(request.form["property_id"])
            prop = Property.query.get_or_404(property_id)

            start_year = int(request.form.get("start_year", datetime.utcnow().year))
            horizon_years = int(request.form.get("horizon_years", 30))
            inflation_rate = float(request.form.get("inflation_rate", 0.03))
            interest_rate = float(request.form.get("interest_rate", 0.01))
            starting_balance = float(request.form.get("starting_balance", 0))
            min_balance = float(request.form.get("min_balance", 0))

            funding_method = (request.form.get("funding_method") or "full").strip()
            contribution_mode = (request.form.get("contribution_mode") or "levelized").strip()

            names = request.form.getlist("component_name[]")
            uls = request.form.getlist("useful_life_years[]")
            rls = request.form.getlist("remaining_life_years[]")
            costs = request.form.getlist("current_replacement_cost[]")
            qtys = request.form.getlist("quantity[]")
            cycles = request.form.getlist("cycle_years[]")

            components = []
            for i in range(len(names)):
                nm = (names[i] or "").strip()
                if not nm:
                    continue

                useful_life = int(uls[i])
                remaining_life = int(rls[i])
                cost = float(costs[i])
                qty = int(qtys[i]) if i < len(qtys) and str(qtys[i]).strip() else 1
                cyc = int(cycles[i]) if i < len(cycles) and str(cycles[i]).strip() else useful_life

                components.append(
                    {
                        "name": nm,
                        "quantity": max(1, qty),
                        "useful_life_years": useful_life,
                        "remaining_life_years": remaining_life,
                        "cycle_years": max(1, cyc),
                        "current_replacement_cost": cost,
                    }
                )

            if not components:
                flash("Please add at least one valid component.")
                return redirect(url_for("property_page", property_id=prop.id))

            recommended_contrib, yearly = recommend_levelized_full_funding_contribution(
                start_year=start_year,
                horizon_years=horizon_years,
                inflation_rate=inflation_rate,
                interest_rate=interest_rate,
                starting_balance=starting_balance,
                components=components,
                min_balance=min_balance,
            )

            study = ReserveStudy(
                property_id=prop.id,
                start_year=start_year,
                horizon_years=horizon_years,
                inflation_rate=inflation_rate,
                interest_rate=interest_rate,
                starting_balance=starting_balance,
                funding_method=funding_method,
                contribution_mode=contribution_mode,
                min_balance=min_balance,
                recommended_annual_contribution=recommended_contrib,
            )
            db.session.add(study)
            db.session.flush()

            for c in components:
                db.session.add(
                    ReserveComponent(
                        study_id=study.id,
                        name=c["name"],
                        quantity=c["quantity"],
                        useful_life_years=c["useful_life_years"],
                        remaining_life_years=c["remaining_life_years"],
                        cycle_years=c["cycle_years"],
                        current_replacement_cost=c["current_replacement_cost"],
                    )
                )

            for row in yearly:
                db.session.add(
                    ReserveYearResult(
                        study_id=study.id,
                        year=row["year"],
                        starting_balance=row["starting_balance"],
                        recommended_contribution=row["recommended_contribution"],
                        contributions=row["contributions"],
                        expenses=row["expenses"],
                        interest_earned=row["interest_earned"],
                        ending_balance=row["ending_balance"],
                        fully_funded_balance=row["fully_funded_balance"],
                        percent_funded=row["percent_funded"],
                    )
                )

            db.session.commit()
            return redirect(url_for("study_detail", study_id=study.id))

        except Exception as e:
            db.session.rollback()
            flash(f"Error creating study: {e}")
            return redirect(url_for("home"))

    # --------------------
    # Edit Study (used by study_detail.html)
    # --------------------
    @app.get("/studies/<int:study_id>/edit")
    @login_required
    def edit_study(study_id: int):
        study = ReserveStudy.query.get_or_404(study_id)
        prop = study.property

        defaults = {
            "start_year": study.start_year,
            "horizon_years": study.horizon_years,
            "inflation_rate": study.inflation_rate,
            "interest_rate": study.interest_rate,
            "starting_balance": study.starting_balance,
            "min_balance": study.min_balance or 0,
            "funding_method": study.funding_method or "full",
            "contribution_mode": study.contribution_mode or "levelized",
        }

        components = [
            {
                "name": c.name,
                "quantity": c.quantity or 1,
                "useful_life_years": c.useful_life_years,
                "remaining_life_years": c.remaining_life_years,
                "cycle_years": (c.cycle_years or 0) or c.useful_life_years,
                "current_replacement_cost": c.current_replacement_cost,
            }
            for c in study.components
        ]

        return render_template(
            "study_form.html",
            prop=prop,
            defaults=defaults,
            components=components,
            clone=None,
            edit_mode=True,
            study=study,
        )

    # --------------------
    # Update Study (used by study_form.html when edit_mode=True)
    # --------------------
    @app.post("/studies/<int:study_id>/update")
    @login_required
    def update_study(study_id: int):
        study = ReserveStudy.query.get_or_404(study_id)

        try:
            start_year = int(request.form.get("start_year", study.start_year))
            horizon_years = int(request.form.get("horizon_years", study.horizon_years))
            inflation_rate = float(request.form.get("inflation_rate", study.inflation_rate))
            interest_rate = float(request.form.get("interest_rate", study.interest_rate))
            starting_balance = float(request.form.get("starting_balance", study.starting_balance))
            min_balance = float(request.form.get("min_balance", study.min_balance or 0))

            funding_method = (request.form.get("funding_method") or "full").strip()
            contribution_mode = (request.form.get("contribution_mode") or "levelized").strip()

            names = request.form.getlist("component_name[]")
            uls = request.form.getlist("useful_life_years[]")
            rls = request.form.getlist("remaining_life_years[]")
            costs = request.form.getlist("current_replacement_cost[]")
            qtys = request.form.getlist("quantity[]")
            cycles = request.form.getlist("cycle_years[]")

            components = []
            for i in range(len(names)):
                nm = (names[i] or "").strip()
                if not nm:
                    continue

                useful_life = int(uls[i])
                remaining_life = int(rls[i])
                cost = float(costs[i])

                qty = int(qtys[i]) if i < len(qtys) and str(qtys[i]).strip() else 1
                cyc = int(cycles[i]) if i < len(cycles) and str(cycles[i]).strip() else useful_life

                components.append(
                    {
                        "name": nm,
                        "quantity": max(1, qty),
                        "useful_life_years": useful_life,
                        "remaining_life_years": remaining_life,
                        "cycle_years": max(1, cyc),
                        "current_replacement_cost": cost,
                    }
                )

            if not components:
                flash("Please add at least one valid component.")
                return redirect(url_for("edit_study", study_id=study.id))

            recommended_contrib, yearly = recommend_levelized_full_funding_contribution(
                start_year=start_year,
                horizon_years=horizon_years,
                inflation_rate=inflation_rate,
                interest_rate=interest_rate,
                starting_balance=starting_balance,
                components=components,
                min_balance=min_balance,
            )

            # Update study fields
            study.start_year = start_year
            study.horizon_years = horizon_years
            study.inflation_rate = inflation_rate
            study.interest_rate = interest_rate
            study.starting_balance = starting_balance
            study.min_balance = min_balance
            study.funding_method = funding_method
            study.contribution_mode = contribution_mode
            study.recommended_annual_contribution = recommended_contrib

            # Clear old child rows
            ReserveYearResult.query.filter_by(study_id=study.id).delete()
            ReserveComponent.query.filter_by(study_id=study.id).delete()

            # Insert updated components
            for c in components:
                db.session.add(
                    ReserveComponent(
                        study_id=study.id,
                        name=c["name"],
                        quantity=c["quantity"],
                        useful_life_years=c["useful_life_years"],
                        remaining_life_years=c["remaining_life_years"],
                        cycle_years=c["cycle_years"],
                        current_replacement_cost=c["current_replacement_cost"],
                    )
                )

            # Insert updated yearly rows
            for row in yearly:
                db.session.add(
                    ReserveYearResult(
                        study_id=study.id,
                        year=row["year"],
                        starting_balance=row["starting_balance"],
                        recommended_contribution=row["recommended_contribution"],
                        contributions=row["contributions"],
                        expenses=row["expenses"],
                        interest_earned=row["interest_earned"],
                        ending_balance=row["ending_balance"],
                        fully_funded_balance=row["fully_funded_balance"],
                        percent_funded=row["percent_funded"],
                    )
                )

            db.session.commit()
            flash("Study updated.")
            return redirect(url_for("study_detail", study_id=study.id))

        except Exception as e:
            db.session.rollback()
            flash(f"Error updating study: {e}")
            return redirect(url_for("edit_study", study_id=study.id))

    # --------------------
    # Study Detail
    # --------------------
    @app.get("/studies/<int:study_id>")
    @login_required
    def study_detail(study_id: int):
        study = ReserveStudy.query.get_or_404(study_id)
        results = (
            ReserveYearResult.query.filter_by(study_id=study_id)
            .order_by(ReserveYearResult.year.asc())
            .all()
        )
        components = (
            ReserveComponent.query.filter_by(study_id=study_id)
            .order_by(ReserveComponent.name.asc())
            .all()
        )
        return render_template(
            "study_detail.html",
            study=study,
            results=results,
            components=components,
        )

    # --------------------
    # Download CSV
    # --------------------
    @app.get("/studies/<int:study_id>/download.csv")
    @login_required
    def download_study_csv(study_id: int):
        study = ReserveStudy.query.get_or_404(study_id)
        results = (
            ReserveYearResult.query.filter_by(study_id=study_id)
            .order_by(ReserveYearResult.year.asc())
            .all()
        )

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
        writer.writerow(
            ["recommended_annual_contribution", f"{(study.recommended_annual_contribution or 0.0):.2f}"]
        )
        writer.writerow([])

        writer.writerow(["Components"])
        writer.writerow(
            [
                "name",
                "qty",
                "useful_life_years",
                "remaining_life_years",
                "cycle_years",
                "replacement_cost_today",
            ]
        )
        for c in study.components:
            writer.writerow(
                [
                    c.name,
                    c.quantity,
                    c.useful_life_years,
                    c.remaining_life_years,
                    c.cycle_years,
                    f"{c.current_replacement_cost:.2f}",
                ]
            )

        writer.writerow([])
        writer.writerow(["Year-by-year results"])
        writer.writerow(["year", "start", "contrib", "expenses", "interest", "end", "ffb", "percent_funded"])

        for r in results:
            writer.writerow(
                [
                    r.year,
                    f"{r.starting_balance:.2f}",
                    f"{r.contributions:.2f}",
                    f"{r.expenses:.2f}",
                    f"{r.interest_earned:.2f}",
                    f"{r.ending_balance:.2f}",
                    f"{r.fully_funded_balance:.2f}",
                    f"{r.percent_funded:.6f}",
                ]
            )

        mem = io.BytesIO(output.getvalue().encode("utf-8"))
        filename = f"reserve_study_{study.id}_{study.property.name.replace(' ', '_')}.csv"
        return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)

    # --------------------
    # Clone Study
    # --------------------
    @app.get("/studies/<int:study_id>/clone")
    @login_required
    def clone_study(study_id: int):
        study = ReserveStudy.query.get_or_404(study_id)
        return redirect(url_for("new_study", property_id=study.property_id, clone_from=study.id))

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=5050, debug=True)









