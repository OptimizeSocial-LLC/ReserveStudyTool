# create_user.py
import os
from werkzeug.security import generate_password_hash
from app import create_app
from models import db, User

def _admin_emails() -> set[str]:
    raw = (os.getenv("ADMIN_EMAILS") or "").strip()
    if not raw:
        return set()
    # split on comma, normalize lowercase
    parts = [p.strip().lower() for p in raw.replace(";", ",").split(",")]
    return {p for p in parts if p}

email = os.environ.get("NEW_USER_EMAIL")
password = os.environ.get("NEW_USER_PASSWORD")

if not email or not password:
    raise RuntimeError("Set NEW_USER_EMAIL and NEW_USER_PASSWORD env vars")

app = create_app()

with app.app_context():
    email_norm = email.strip().lower()
    admins = _admin_emails()

    existing = User.query.filter_by(email=email_norm).first()
    if existing:
        # keep admin status synced with env
        should_admin = email_norm in admins
        if hasattr(existing, "is_admin") and bool(getattr(existing, "is_admin", False)) != should_admin:
            existing.is_admin = should_admin
            db.session.commit()
            print("Updated admin flag for:", email_norm, "->", should_admin)
        else:
            print("User already exists:", email_norm)
    else:
        u = User(
            email=email_norm,
            password_hash=generate_password_hash(password),
            is_admin=(email_norm in admins),
        )
        db.session.add(u)
        db.session.commit()
        print("Created user:", email_norm, "admin=" + str(u.is_admin))
