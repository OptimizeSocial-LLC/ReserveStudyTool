# models.py
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.BigInteger, primary_key=True)

    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

    actor_user_id = db.Column(db.BigInteger, nullable=True, index=True)
    actor_email = db.Column(db.Text, nullable=True)

    action = db.Column(db.Text, nullable=False)
    entity_type = db.Column(db.Text, nullable=False)
    entity_id = db.Column(db.BigInteger, nullable=True)

    meta = db.Column(db.JSON, nullable=True)

    __table_args__ = (
        db.Index("idx_audit_logs_created_at", db.text("created_at DESC")),
        db.Index("idx_audit_logs_entity", "entity_type", "entity_id"),
    )


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    properties = db.relationship(
        "Property", backref="user", lazy=True, cascade="all, delete-orphan"
    )


class Property(db.Model):
    __tablename__ = "properties"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )

    name = db.Column(db.String(220), nullable=False)
    address = db.Column(db.String(320), nullable=True)
    city = db.Column(db.String(120), nullable=True)
    state = db.Column(db.String(80), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    studies = db.relationship(
        "ReserveStudy", backref="property", lazy=True, cascade="all, delete-orphan"
    )


class ReserveStudy(db.Model):
    __tablename__ = "reserve_studies"

    id = db.Column(db.Integer, primary_key=True)

    property_id = db.Column(
        db.Integer, db.ForeignKey("properties.id"), nullable=False, index=True
    )

    # tier + workflow
    tier = db.Column(db.String(40), nullable=False, default="essentials", index=True)

    # workflow_status values:
    # - draft
    # - paid_final                 (essentials after payment)
    # - paid_awaiting_review       (plus after payment)
    # - approved_final             (plus after admin approves; also legacy paid)
    # - paid_pending_admin_build   (premium after payment; admins build)
    workflow_status = db.Column(db.String(60), nullable=False, default="draft", index=True)

    start_year = db.Column(db.Integer, nullable=False)
    horizon_years = db.Column(db.Integer, nullable=False)

    inflation_rate = db.Column(db.Float, nullable=False)
    interest_rate = db.Column(db.Float, nullable=False)

    starting_balance = db.Column(db.Float, nullable=False)
    min_balance = db.Column(db.Float, nullable=False, default=0.0)

    funding_method = db.Column(db.String(50), nullable=False, default="full")
    contribution_mode = db.Column(db.String(50), nullable=False, default="levelized")

    recommended_annual_contribution = db.Column(db.Float, nullable=True)

    # legacy: keep for compatibility
    paid_status = db.Column(db.String(30), nullable=False, default="draft", index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    components = db.relationship(
        "ReserveComponent", backref="study", lazy=True, cascade="all, delete-orphan"
    )
    results = db.relationship(
        "ReserveYearResult", backref="study", lazy=True, cascade="all, delete-orphan"
    )

    @property
    def is_paid(self) -> bool:
        ws = (self.workflow_status or "").lower()
        if ws in ("paid_final", "paid_awaiting_review", "approved_final", "paid_pending_admin_build"):
            return True
        return (self.paid_status or "").lower() == "paid"

    @property
    def is_approved(self) -> bool:
        return (self.workflow_status or "").lower() == "approved_final"


class ReserveComponent(db.Model):
    __tablename__ = "reserve_components"

    id = db.Column(db.Integer, primary_key=True)

    study_id = db.Column(
        db.Integer, db.ForeignKey("reserve_studies.id"), nullable=False, index=True
    )

    name = db.Column(db.String(200), nullable=False)
    current_replacement_cost = db.Column(db.Float, nullable=False)

    quantity = db.Column(db.Integer, nullable=False, default=1)
    useful_life_years = db.Column(db.Integer, nullable=False)
    remaining_life_years = db.Column(db.Integer, nullable=False)
    cycle_years = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    photos = db.relationship(
        "ComponentPhoto", backref="component", lazy=True, cascade="all, delete-orphan"
    )


class ReserveYearResult(db.Model):
    __tablename__ = "reserve_year_results"

    id = db.Column(db.Integer, primary_key=True)

    study_id = db.Column(
        db.Integer, db.ForeignKey("reserve_studies.id"), nullable=False, index=True
    )

    year = db.Column(db.Integer, nullable=False)

    starting_balance = db.Column(db.Float, nullable=False)
    contributions = db.Column(db.Float, nullable=False)
    expenses = db.Column(db.Float, nullable=False)
    interest_earned = db.Column(db.Float, nullable=False)
    ending_balance = db.Column(db.Float, nullable=False)

    fully_funded_balance = db.Column(db.Float, nullable=False)
    percent_funded = db.Column(db.Float, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ComponentPhoto(db.Model):
    __tablename__ = "component_photos"

    id = db.Column(db.Integer, primary_key=True)

    component_id = db.Column(
        db.Integer,
        db.ForeignKey("reserve_components.id"),
        nullable=False,
        index=True,
    )

    storage_provider = db.Column(db.String(50), nullable=True)
    storage_bucket = db.Column(db.String(255), nullable=True)
    storage_key = db.Column(db.String(600), nullable=False, index=True)

    original_filename = db.Column(db.String(255), nullable=True)
    filename = db.Column(db.String(255), nullable=True)

    content_type = db.Column(db.String(120), nullable=True)
    size_bytes = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class TempComponentPhoto(db.Model):
    __tablename__ = "temp_component_photos"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False, index=True)

    row_key = db.Column(db.String(80), nullable=False, index=True)

    storage_key = db.Column(db.String(600), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=True)
    content_type = db.Column(db.String(120), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.Index("ix_temp_photo_user_prop_row", "user_id", "property_id", "row_key"),
    )


class PremiumRequest(db.Model):
    __tablename__ = "premium_requests"

    id = db.Column(db.BigInteger, primary_key=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    property_id = db.Column(db.BigInteger, db.ForeignKey("properties.id"), nullable=False, index=True)

    paid = db.Column(db.Boolean, nullable=False, default=False)
    paid_amount_cents = db.Column(db.Integer, nullable=False, default=0)

    scheduled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.Text, nullable=False, default="created")  # created | paid_pending_schedule | scheduled | completed
    meta = db.Column(db.JSON, nullable=True)

















