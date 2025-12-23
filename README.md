# Reserve Study Tool (Flask + SQL)

This is a minimal working Flask app that:
- Lets you create properties
- Run a reserve study for a property (assumptions + components)
- Stores **inputs + computed results** in SQL (SQLite locally, Postgres on Render)
- Lets you **download a CSV report** (download-only; no S3 needed)
- Lets you **clone** a previous study to reuse prior data

## Local run (fastest)
```bash
python -m venv .venv
source .venv/bin/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

python seed.py              # creates fake properties
python app.py               # runs dev server
```

Open: http://127.0.0.1:5000

## Database
- Default: SQLite `app.db` in the project folder (easy for dev).
- Production (Render): set `DATABASE_URL` to your Render Postgres connection string.

## Render deploy
**Build command**
```bash
pip install -r requirements.txt
```

**Start command**
```bash
gunicorn app:app
```

**Environment variables**
- `SECRET_KEY` = some random string
- `DATABASE_URL` = Render Postgres URL (Render provides it)

## Notes
The reserve math in `reserve_math.py` is a placeholder:
- fixed annual contribution
- scheduled replacements when remaining life hits 0
- inflation + interest applied

