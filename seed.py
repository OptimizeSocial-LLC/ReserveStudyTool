# seed.py
"""
Generates fake properties in your database so you can demo the flow quickly.

Usage:
  python seed.py
"""
from faker import Faker
from app import create_app
from models import db, Property

fake = Faker()

def run():
    app = create_app()
    with app.app_context():
        db.create_all()

        if Property.query.count() > 0:
            print("DB already has properties. Skipping seed.")
            return

        for _ in range(6):
            p = Property(
                name=f"{fake.street_name()} Apartments",
                address=fake.street_address(),
                city=fake.city(),
                state=fake.state_abbr(),
            )
            db.session.add(p)

        db.session.commit()
        print("Seed complete. Created 6 fake properties.")

if __name__ == "__main__":
    run()

