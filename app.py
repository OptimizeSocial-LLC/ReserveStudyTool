# app.py
import os
import io
import csv
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, send_file

from models import db, Property, ReserveStudy, ReserveComponent, ReserveYearResult
from reserve_math import run_simple_reserve_math


def create_app():
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

    # Render: set DATABASE_URL in dashboard (Render Postgres)
    # Local dev fallback: SQLite
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///app.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    # --------------------
    # Home / Properties
    # --------------------

    @app.get("/")
    def home():
        properties = Property.query.order_by(Property.created_at.desc()).all()
        return render_template("home.html", properties=properties)

    @app.post("/properties/create")
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
    def property_page(property_id: int):
        prop = Property.query.get_or_404(property_id)
        studies = (
            ReserveStudy.query.filter_by(property_id=property_id)
            .order_by(ReserveStudy.created_at.desc())
            .all()
        )
        return render_template("property.html", prop=prop, studies=studies)

    # --------------------
    # New / Clone Study (Create flow)
    # --------------------

    @app.get("/studies/new")
    def new_study():
        """
        Query params:
          property_id=123
          clone_from=456 (study_id)
        """
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
            "annual_contribution": 25000,
        }

        components = [
            {"name": "Roof", "useful_life_years": 25, "remaining_life_years": 8, "current_replacement_cost": 180000},
            {"name": "Exterior Paint", "useful_life_years": 10, "remaining_life_years": 3, "current_replacement_cost": 45000},
            {"name": "Paving", "useful_life_years": 20, "remaining_life_years": 12, "current_replacement_cost": 90000},
        ]

        if clone:
            defaults = {
                "start_year": clone.start_year,
                "horizon_years": clone.horizon_years,
                "inflation_rate": clone.inflation_rate,
                "interest_rate": clone.interest_rate,
                "starting_balance": clone.starting_balance,
                "annual_contribution": clone.annual_contribution,
            }
            components = [
                {
                    "name": c.name,
                    "useful_life_years": c.useful_life_years,
                    "remaining_life_years": c.remaining_life_years,
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
    def create_study():
        try:
            property_id = int(request.form["property_id"])
            prop = Property.query.get_or_404(property_id)

            start_year = int(request.form.get("start_year", datetime.utcnow().year))
            horizon_years = int(request.form.get("horizon_years", 30))
            inflation_rate = float(request.form.get("inflation_rate", 0.03))
            interest_rate = float(request.form.get("interest_rate", 0.01))
            starting_balance = float(request.form.get("starting_balance", 0))
            annual_contribution = float(request.form.get("annual_contribution", 25000))

            names = request.form.getlist("component_name[]")
            uls = request.form.getlist("useful_life_years[]")
            rls = request.form.getlist("remaining_life_years[]")
            costs = request.form.getlist("current_replacement_cost[]")

            components = []
            for i in range(len(names)):
                nm = (names[i] or "").strip()
                if not nm:
                    continue

                try:
                    useful_life = int(uls[i])
                    remaining_life = int(rls[i])
                    cost = float(costs[i])
                except (ValueError, IndexError):
                    continue

                components.append(
                    {
                        "name": nm,
                        "useful_life_years": useful_life,
                        "remaining_life_years": remaining_life,
                        "current_replacement_cost": cost,
                    }
                )

            if not components:
                flash("Please add at least one valid component.")
                return redirect(url_for("property_page", property_id=prop.id))

            study = ReserveStudy(
                property_id=prop.id,
                start_year=start_year,
                horizon_years=horizon_years,
                inflation_rate=inflation_rate,
                interest_rate=interest_rate,
                starting_balance=starting_balance,
                annual_contribution=annual_contribution,
            )
            db.session.add(study)
            db.session.flush()

            for c in components:
                db.session.add(
                    ReserveComponent(
                        study_id=study.id,
                        name=c["name"],
                        useful_life_years=c["useful_life_years"],
                        remaining_life_years=c["remaining_life_years"],
                        current_replacement_cost=c["current_replacement_cost"],
                    )
                )

            yearly = run_simple_reserve_math(
                start_year=start_year,
                horizon_years=horizon_years,
                inflation_rate=inflation_rate,
                interest_rate=interest_rate,
                components=components,
                starting_balance=starting_balance,
                annual_contribution=annual_contribution,
            )

            for row in yearly:
                db.session.add(
                    ReserveYearResult(
                        study_id=study.id,
                        year=row["year"],
                        starting_balance=row["starting_balance"],
                        contributions=row["contributions"],
                        expenses=row["expenses"],
                        interest_earned=row["interest_earned"],
                        ending_balance=row["ending_balance"],
                    )
                )

            db.session.commit()
            return redirect(url_for("study_detail", study_id=study.id))

        except Exception as e:
            db.session.rollback()
            flash(f"Error creating study: {e}")
            return redirect(url_for("home"))

    # --------------------
    # Edit Study (loads the same form, but edit_mode=True)
    # --------------------

    @app.get("/studies/<int:study_id>/edit")
    def edit_study(study_id: int):
        study = ReserveStudy.query.get_or_404(study_id)
        prop = study.property

        defaults = {
            "start_year": study.start_year,
            "horizon_years": study.horizon_years,
            "inflation_rate": study.inflation_rate,
            "interest_rate": study.interest_rate,
            "starting_balance": study.starting_balance,
            "annual_contribution": study.annual_contribution,
        }

        components = [
            {
                "name": c.name,
                "useful_life_years": c.useful_life_years,
                "remaining_life_years": c.remaining_life_years,
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
    # Update Study (replaces components + recomputes results)
    # --------------------

    @app.post("/studies/<int:study_id>/update")
    def update_study(study_id: int):
        study = ReserveStudy.query.get_or_404(study_id)

        try:
            start_year = int(request.form.get("start_year", datetime.utcnow().year))
            horizon_years = int(request.form.get("horizon_years", 30))
            inflation_rate = float(request.form.get("inflation_rate", 0.03))
            interest_rate = float(request.form.get("interest_rate", 0.01))
            starting_balance = float(request.form.get("starting_balance", 0))
            annual_contribution = float(request.form.get("annual_contribution", 25000))

            names = request.form.getlist("component_name[]")
            uls = request.form.getlist("useful_life_years[]")
            rls = request.form.getlist("remaining_life_years[]")
            costs = request.form.getlist("current_replacement_cost[]")

            components = []
            for i in range(len(names)):
                nm = (names[i] or "").strip()
                if not nm:
                    continue

                try:
                    useful_life = int(uls[i])
                    remaining_life = int(rls[i])
                    cost = float(costs[i])
                except (ValueError, IndexError):
                    continue

                components.append(
                    {
                        "name": nm,
                        "useful_life_years": useful_life,
                        "remaining_life_years": remaining_life,
                        "current_replacement_cost": cost,
                    }
                )

            if not components:
                flash("Please add at least one valid component.")
                return redirect(url_for("edit_study", study_id=study.id))

            # Update study inputs
            study.start_year = start_year
            study.horizon_years = horizon_years
            study.inflation_rate = inflation_rate
            study.interest_rate = interest_rate
            study.starting_balance = starting_balance
            study.annual_contribution = annual_contribution

            # Remove old components + results (then replace)
            ReserveComponent.query.filter_by(study_id=study.id).delete(synchronize_session=False)
            ReserveYearResult.query.filter_by(study_id=study.id).delete(synchronize_session=False)

            # Insert new components
            for c in components:
                db.session.add(
                    ReserveComponent(
                        study_id=study.id,
                        name=c["name"],
                        useful_life_years=c["useful_life_years"],
                        remaining_life_years=c["remaining_life_years"],
                        current_replacement_cost=c["current_replacement_cost"],
                    )
                )

            # Recompute results
            yearly = run_simple_reserve_math(
                start_year=start_year,
                horizon_years=horizon_years,
                inflation_rate=inflation_rate,
                interest_rate=interest_rate,
                components=components,
                starting_balance=starting_balance,
                annual_contribution=annual_contribution,
            )

            for row in yearly:
                db.session.add(
                    ReserveYearResult(
                        study_id=study.id,
                        year=row["year"],
                        starting_balance=row["starting_balance"],
                        contributions=row["contributions"],
                        expenses=row["expenses"],
                        interest_earned=row["interest_earned"],
                        ending_balance=row["ending_balance"],
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
        return render_template("study_detail.html", study=study, results=results, components=components)

    # --------------------
    # Download CSV
    # --------------------

    @app.get("/studies/<int:study_id>/download.csv")
    def download_study_csv(study_id: int):
        study = ReserveStudy.query.get_or_404(study_id)
        results = (
            ReserveYearResult.query.filter_by(study_id=study_id)
            .order_by(ReserveYearResult.year.asc())
            .all()
        )

        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow(
            [
                "property",
                "study_id",
                "start_year",
                "horizon_years",
                "inflation_rate",
                "interest_rate",
                "starting_balance",
                "annual_contribution",
            ]
        )
        writer.writerow(
            [
                study.property.name,
                study.id,
                study.start_year,
                study.horizon_years,
                study.inflation_rate,
                study.interest_rate,
                f"{study.starting_balance:.2f}",
                f"{study.annual_contribution:.2f}",
            ]
        )
        writer.writerow([])
        writer.writerow(["year", "starting_balance", "contributions", "expenses", "interest_earned", "ending_balance"])

        for r in results:
            writer.writerow(
                [
                    r.year,
                    f"{r.starting_balance:.2f}",
                    f"{r.contributions:.2f}",
                    f"{r.expenses:.2f}",
                    f"{r.interest_earned:.2f}",
                    f"{r.ending_balance:.2f}",
                ]
            )

        mem = io.BytesIO(output.getvalue().encode("utf-8"))
        filename = f"reserve_study_{study.id}_{study.property.name.replace(' ', '_')}.csv"

        return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)

    # --------------------
    # Clone Study
    # --------------------

    @app.get("/studies/<int:study_id>/clone")
    def clone_study(study_id: int):
        study = ReserveStudy.query.get_or_404(study_id)
        return redirect(url_for("new_study", property_id=study.property_id, clone_from=study.id))

    return app


app = create_app()

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="127.0.0.1", port=5050, debug=True)



