# models.py
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Property(db.Model):
    __tablename__ = "properties"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(220), nullable=False)
    address = db.Column(db.String(320), nullable=True)
    city = db.Column(db.String(120), nullable=True)
    state = db.Column(db.String(30), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    studies = db.relationship("ReserveStudy", backref="property", lazy=True, cascade="all, delete-orphan")

class ReserveStudy(db.Model):
    __tablename__ = "reserve_studies"
    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)

    start_year = db.Column(db.Integer, nullable=False)
    horizon_years = db.Column(db.Integer, nullable=False)
    inflation_rate = db.Column(db.Float, nullable=False, default=0.03)
    interest_rate = db.Column(db.Float, nullable=False, default=0.01)
    starting_balance = db.Column(db.Float, nullable=False, default=0.0)
    annual_contribution = db.Column(db.Float, nullable=False, default=25000.0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    components = db.relationship("ReserveComponent", backref="study", lazy=True, cascade="all, delete-orphan")
    results = db.relationship("ReserveYearResult", backref="study", lazy=True, cascade="all, delete-orphan")

class ReserveComponent(db.Model):
    __tablename__ = "reserve_components"
    id = db.Column(db.Integer, primary_key=True)
    study_id = db.Column(db.Integer, db.ForeignKey("reserve_studies.id"), nullable=False)

    name = db.Column(db.String(220), nullable=False)
    useful_life_years = db.Column(db.Integer, nullable=False)
    remaining_life_years = db.Column(db.Integer, nullable=False)
    current_replacement_cost = db.Column(db.Float, nullable=False)

class ReserveYearResult(db.Model):
    __tablename__ = "reserve_year_results"
    id = db.Column(db.Integer, primary_key=True)
    study_id = db.Column(db.Integer, db.ForeignKey("reserve_studies.id"), nullable=False)

    year = db.Column(db.Integer, nullable=False)
    starting_balance = db.Column(db.Float, nullable=False)
    contributions = db.Column(db.Float, nullable=False)
    expenses = db.Column(db.Float, nullable=False)
    interest_earned = db.Column(db.Float, nullable=False)
    ending_balance = db.Column(db.Float, nullable=False)

    __table_args__ = (db.UniqueConstraint("study_id", "year", name="uq_study_year"),)
