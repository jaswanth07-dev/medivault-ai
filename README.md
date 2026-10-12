# MediVault AI

MediVault AI is an educational healthcare-document organization website built with Python, FastAPI, SQLite, HTML, CSS, and vanilla JavaScript. It includes a public landing page, SQLite-backed accounts, protected account workspaces, document upload/review, cautious summaries, and a chronological timeline.

## Features

- PDF, JPG, and PNG upload support
- PDF text extraction with PyMuPDF
- OCR for scanned images and PDFs when Tesseract is installed
- Field extraction for dates, document type, medications, dosages, diagnoses, and lab values
- Editable review form before saving
- Deterministic summary generation with no API keys required
- SQLite timeline dashboard
- Five synthetic sample PDFs with a demo-loading flow
- Medical disclaimers and “synthetic data” labeling
- Password-hashed accounts with expiring, HttpOnly session cookies
- Account-scoped uploaded originals, saved records, and timelines
- Password change, profile, and logout pages
- Feedback saved locally (not emailed)
- Integration tests for authentication, upload, record ownership, and timeline

## Windows setup

1. Install Python 3.11+ from python.org or winget:
   `winget install Python.Python.3.13`
2. Open PowerShell in this project folder.
3. Create a virtual environment:
   `py -3.13 -m venv .venv`
4. Activate it:
   `.\.venv\Scripts\Activate.ps1`
5. Install dependencies:
   `python -m pip install --upgrade pip`
   `python -m pip install -r requirements.txt`
6. Optionally install Tesseract OCR for scanned image support:
   - Download and install Tesseract from: https://github.com/UB-Mannheim/tesseract/wiki
   - Ensure `tesseract` is on your PATH.
   - If it is installed but not on PATH, set `TESSERACT_CMD` in `.env` to the full executable path, for example `C:\Program Files\Tesseract-OCR\tesseract.exe`.
   - Without a runnable Tesseract executable, scanned image uploads return an actionable OCR-unavailable error; the uploaded original is retained and empty extraction is not reported as success.
7. Create local environment values:
   `Copy-Item .env.example .env`
   For a local HTTP development server, `COOKIE_SECURE=false` is used. Set `COOKIE_SECURE=true` only when serving over HTTPS.
8. Start the app:
   `uvicorn app:app --reload --host 0.0.0.0 --port 8000`
9. Open http://localhost:8000

## Testing

Run the backend and workflow regression tests with:

`python -m unittest discover -s tests -v`

The tests use a temporary SQLite database and temporary upload directories; they do not add test accounts or records to the configured database.

## Important safety notes

- This application is for documentation review and summarization support only.
- It does not diagnose patients or recommend medication changes.
- All extracted content should be reviewed by a qualified clinician before any care decisions.
- Synthetic sample data is clearly labeled as fictional.
- Accounts and records are stored locally. This project is not a production identity or healthcare-record platform and has not undergone security/compliance certification.
- Feedback submissions are stored locally in SQLite; the form does not send email.

## Project structure

- `app.py` — FastAPI app, extraction logic, SQLite DB, sample generation
- `templates/index.html` — dashboard UI
- `static/styles.css` — responsive healthcare styling
- `static/app.js` — upload and verification flow
- `storage/originals/` — preserved uploaded file copies
- `storage/sample_docs/` — generated fictional PDFs
- `medivault.db` — SQLite records database
