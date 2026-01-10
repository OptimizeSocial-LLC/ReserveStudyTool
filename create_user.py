import os
from werkzeug.security import generate_password_hash
from app import create_app
from models import db, User

email = os.environ.get("NEW_USER_EMAIL")
password = os.environ.get("NEW_USER_PASSWORD")

if not email or not password:
    raise RuntimeError("Set NEW_USER_EMAIL and NEW_USER_PASSWORD env vars")

app = create_app()

with app.app_context():
    email_norm = email.strip().lower()
    if User.query.filter_by(email=email_norm).first():
        print("User already exists:", email_norm)
    else:
        u = User(email=email_norm, password_hash=generate_password_hash(password))
        db.session.add(u)
        db.session.commit()
        print("Created user:", email_norm)