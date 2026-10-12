from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import secrets
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import fitz
import pytesseract
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

APP_NAME = os.getenv("APP_NAME", "MediVault AI")
DB_PATH = Path(os.getenv("MEDIVAULT_DB_PATH", str(BASE_DIR / "medivault.db")))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "storage" / "originals")))
SAMPLE_DIR = Path(os.getenv("SAMPLE_DIR", str(BASE_DIR / "storage" / "sample_docs")))

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_TESSERACT_CMD = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
KNOWN_DRUGS = [
    "acetaminophen",
    "amoxicillin",
    "aspirin",
    "atorvastatin",
    "cetirizine",
    "albuterol",
    "bupropion",
    "furosemide",
    "ibuprofen",
    "insulin",
    "levothyroxine",
    "lisinopril",
    "metformin",
    "omeprazole",
    "paracetamol",
    "prednisone",
    "sertraline",
    "simvastatin",
    "vitamin d",
    "iron supplement",
    "oral rehydration salts",
]

MEDICINE_ALIASES = {
    "oral rehydration salts": ("oral rehydration salts", "ors"),
}

LAB_TEST_NAMES = {
    "hemoglobin",
    "platelets",
    "sodium",
    "potassium",
    "creatinine",
    "ferritin",
    "glucose",
    "wbc",
    "rbc",
    "cholesterol",
    "hdl",
    "ldl",
    "triglycerides",
    "albumin",
    "bmi",
    "blood pressure",
    "temperature",
}

NOISE_LABELS = {
    "date",
    "document type",
    "report",
    "note",
    "summary",
    "patient",
    "time",
    "mrn",
    "visit",
    "consult",
    "medication",
    "medications",
    "diagnosis",
    "diagnoses",
    "assessment",
    "impression",
    "review",
    "lab values",
    "progress",
    "specialist",
    "follow-up",
    "synthetic sample",
}


class OCRUnavailableError(RuntimeError):
    """Raised when scanned content requires OCR but no usable Tesseract is installed."""


class OCRProcessingError(RuntimeError):
    """Raised when OCR is installed but cannot extract readable text from an image."""


class DocumentReadError(RuntimeError):
    """Raised when an uploaded document cannot be decoded or read."""

app = FastAPI(title=APP_NAME)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=select_autoescape(("html", "xml")),
)


def ensure_directories() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SESSION_COOKIE = "medivault_session"
SESSION_LIFETIME = timedelta(days=14)


def password_digest(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def password_matches(password: str, stored_digest: str) -> bool:
    try:
        salt_hex, digest_hex = stored_digest.split("$", 1)
        candidate = password_digest(password, bytes.fromhex(salt_hex)).split("$", 1)[1]
        return hmac.compare_digest(candidate, digest_hex)
    except (ValueError, TypeError):
        return False


def current_user(request: Request) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = get_db_connection()
    row = conn.execute(
        """
        SELECT users.id, users.email, users.display_name, users.created_at
        FROM sessions JOIN users ON users.id = sessions.user_id
        WHERE sessions.token_hash = ? AND sessions.expires_at > ?
        """,
        (token_hash, datetime.utcnow().isoformat(timespec="seconds")),
    ).fetchone()
    conn.close()
    return row


def require_user(request: Request) -> sqlite3.Row:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Please log in to continue.")
    return user


def set_session_cookie(response: JSONResponse, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            user_id,
            (now + SESSION_LIFETIME).isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    conn.close()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        samesite="lax",
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
    )


def render_page(request: Request, template_name: str, **context: Any) -> HTMLResponse:
    user = current_user(request)
    template_context = {
        "request": request,
        "app_name": APP_NAME,
        "current_user": user,
        **context,
    }
    return HTMLResponse(templates.get_template(template_name).render(**template_context))


def init_db() -> None:
    ensure_directories()
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            stored_path TEXT,
            document_date TEXT,
            document_type TEXT,
            medicines TEXT,
            dosages TEXT,
            diagnoses TEXT,
            lab_values TEXT,
            summary TEXT,
            raw_text TEXT,
            source_type TEXT,
            is_synthetic INTEGER DEFAULT 0,
            created_at TEXT,
            status TEXT,
            owner_id INTEGER REFERENCES users(id)
        )
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
    if "owner_id" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN owner_id INTEGER REFERENCES users(id)")
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "display_name" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def safe_filename(name: str) -> str:
    clean = name or "document"
    clean = clean.replace("\\", "/")
    clean = clean.rsplit("/", 1)[-1]
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", clean)
    return clean or "document"


def read_text_file(path: Path) -> str:
    text = ""
    if path.suffix.lower() == ".pdf":
        try:
            with fitz.open(path) as doc:
                for page in doc:
                    page_text = page.get_text("text")
                    if page_text and page_text.strip():
                        text += page_text + "\n"
                    else:
                        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                        image_path = BASE_DIR / f"tmp_ocr_{uuid.uuid4().hex}.png"
                        pix.save(image_path)
                        try:
                            ocr_text = ocr_image_file(image_path)
                            if ocr_text:
                                text += ocr_text + "\n"
                        finally:
                            if image_path.exists():
                                image_path.unlink(missing_ok=True)
        except (OCRUnavailableError, OCRProcessingError):
            raise
        except Exception as error:
            raise DocumentReadError("The PDF could not be read. Check that the file is a valid, unencrypted PDF.") from error
    else:
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            raise DocumentReadError("This document type is not supported for text extraction.")
        text = ocr_image_file(path)

    text = text.strip()
    if not text:
        raise OCRProcessingError("No readable text was extracted. Check the document quality and try again.")
    return text


def configure_tesseract() -> str:
    configured_path = os.getenv("TESSERACT_CMD", "").strip().strip('"')
    executable = None
    if configured_path:
        configured_file = Path(configured_path)
        if configured_file.is_file():
            executable = str(configured_file.resolve())
        else:
            executable = shutil.which(configured_path)
    else:
        executable = shutil.which("tesseract")
        if not executable and DEFAULT_TESSERACT_CMD.is_file():
            executable = str(DEFAULT_TESSERACT_CMD.resolve())

    if not executable:
        raise OCRUnavailableError(
            "Scanned-document OCR is unavailable. Install Tesseract OCR and add it to PATH, "
            "or set TESSERACT_CMD in .env to the full path of tesseract.exe. "
            "The uploaded original has been retained."
        )

    pytesseract.pytesseract.tesseract_cmd = executable
    try:
        pytesseract.get_tesseract_version()
    except Exception as error:
        raise OCRUnavailableError(
            "Tesseract was found but could not be started. Check the Tesseract installation "
            "or TESSERACT_CMD setting. The uploaded original has been retained."
        ) from error
    return executable


def tesseract_is_available() -> bool:
    try:
        configure_tesseract()
        return True
    except OCRUnavailableError:
        return False


def ocr_image_file(path: Path) -> str:
    configure_tesseract()
    try:
        with Image.open(path) as image:
            text = pytesseract.image_to_string(image, config="--psm 6").strip()
    except pytesseract.TesseractNotFoundError as error:
        raise OCRUnavailableError(
            "Tesseract could not be started. Check TESSERACT_CMD or add Tesseract to PATH. "
            "The uploaded original has been retained."
        ) from error
    except Exception as error:
        raise OCRProcessingError("OCR could not process this image. Check that it is a valid JPG or PNG and try again.") from error
    if not text:
        raise OCRProcessingError("OCR ran but found no readable text. Check the image quality and try again.")
    return text


def detect_date(value: str) -> str:
    patterns = [
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return ""


def detect_document_type(filename: str, text: str) -> str:
    combined = f"{filename} {text}".lower()
    rules = [
        ("Lab report", ["lab report", "laboratory report", "cbc", "bmp", "cmp", "hematology", "chemistry"]),
        ("Progress note", ["progress note", "visit note", "clinic note", "follow-up note", "consult note"]),
        ("Discharge summary", ["discharge summary", "discharge note", "hospital discharge"]),
        ("Medication list", ["medication list", "prescription", "meds", "rx", "medication refill"]),
        ("Imaging report", ["x-ray", "mri", "ct", "ultrasound", "imaging report", "radiology"]),
        ("Specialist consult", ["specialist consult", "cardiology", "neurology", "endocrinology"]),
    ]
    for label, keywords in rules:
        if any(keyword in combined for keyword in keywords):
            return label
    return "Medical document"


def is_noise_label(label: str) -> bool:
    cleaned = re.sub(r"[^A-Za-z0-9 /-]+", "", label or "").strip().lower()
    if not cleaned:
        return True
    if cleaned.startswith("synthetic sample") or "synthetic" in cleaned or "sample" in cleaned:
        return True
    if cleaned in NOISE_LABELS:
        return True
    if cleaned.startswith("date") or cleaned.startswith("document type"):
        return True
    if cleaned.startswith("diagnosis") or cleaned.startswith("medication"):
        return True
    if cleaned.startswith("lab values") or cleaned.startswith("assessment"):
        return True
    return False


def medicine_aliases(medicine: str) -> tuple[str, ...]:
    return MEDICINE_ALIASES.get(medicine, (medicine,))


def find_medicine_name(text: str) -> str | None:
    for medicine in sorted(KNOWN_DRUGS, key=len, reverse=True):
        if any(re.search(rf"(?i)\b{re.escape(alias)}\b", text) for alias in medicine_aliases(medicine)):
            return medicine.title()
    return None


def extract_medicines(text: str) -> list[str]:
    matches = {
        medicine.title()
        for medicine in KNOWN_DRUGS
        if any(re.search(rf"(?i)\b{re.escape(alias)}\b", text) for alias in medicine_aliases(medicine))
    }
    return sorted(matches)


def is_strength_value(value: str) -> bool:
    cleaned = value.strip()
    return cleaned.casefold() == "as packaged" or bool(
        re.fullmatch(
            r"\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units?|tablets?|capsules?)",
            cleaned,
            flags=re.IGNORECASE,
        )
    )


def parse_medicine_table_row(line: str) -> tuple[str, str, str] | None:
    cells = [cell.strip() for cell in line.split("|")]
    medicine = find_medicine_name(cells[0] if cells else line)
    if not medicine:
        return None
    if len(cells) < 2 or not is_strength_value(cells[1]):
        return None
    strength = cells[1]
    instructions = " | ".join(cells[2:]).strip()
    instructions = re.sub(r"(?i)^instructions?\s*:\s*", "", instructions).strip()
    return medicine, strength, instructions


def extract_structured_medication_table(text: str) -> list[tuple[str, str, str]]:
    lines = [line.strip() for line in text.splitlines()]
    rows = [line for line in lines if line]
    table_header = next(
        (
            index
            for index, line in enumerate(lines)
            if re.search(r"(?i)medicine\s+name", line)
            and (
                "strength" in line.casefold()
                or any("strength" in following.casefold() for following in lines[index + 1 : index + 4])
            )
        ),
        None,
    )
    if table_header is None:
        return []

    cursor = table_header + 1
    while cursor < len(lines) and (
        not lines[cursor]
        or any(header in lines[cursor].casefold() for header in ("medicine name", "strength", "instruction"))
    ):
        cursor += 1

    sequential_rows: list[tuple[str, str, str]] = []
    line_index = cursor
    while line_index < len(lines):
        line = lines[line_index]
        if not line:
            line_index += 1
            continue
        medicine = find_medicine_name(line)
        exact_name = line.casefold() in {
            alias.casefold()
            for known in KNOWN_DRUGS
            for alias in medicine_aliases(known)
        }
        if not medicine or not exact_name:
            line_index += 1
            continue
        strength_index = line_index + 1
        while strength_index < len(lines) and not lines[strength_index]:
            strength_index += 1
        if strength_index >= len(lines) or not is_strength_value(lines[strength_index]):
            line_index += 1
            continue
        instruction_index = strength_index + 1
        while instruction_index < len(lines) and not lines[instruction_index]:
            instruction_index += 1
        if instruction_index >= len(lines):
            sequential_rows.append((medicine, lines[strength_index], ""))
            break
        possible_instruction = lines[instruction_index]
        if re.match(r"(?i)^(?:note:|fictional sample|synthetic demo|patient:|date:)", possible_instruction):
            sequential_rows.append((medicine, lines[strength_index], ""))
            line_index = instruction_index + 1
            continue
        sequential_rows.append((medicine, lines[strength_index], possible_instruction))
        line_index = instruction_index + 1

    if sequential_rows:
        return sequential_rows

    # Inline table rows keep all related fields on one OCR/PDF text line.
    inline_rows: list[tuple[str, str, str]] = []
    while cursor < len(lines):
        row = parse_medicine_table_row(lines[cursor])
        if row:
            inline_rows.append(row)
            cursor += 1
            continue
        break
    if inline_rows:
        return inline_rows

    # Some PDFs expose table columns as contiguous blocks: all names, then all
    # strengths, then all instructions. Pair only when every strength is explicit.
    medicine_names: list[str] = []
    while cursor < len(lines):
        line = lines[cursor]
        if not line:
            cursor += 1
            continue
        medicine = find_medicine_name(line)
        if not medicine or line.casefold() not in {
            alias.casefold()
            for known in KNOWN_DRUGS
            for alias in medicine_aliases(known)
        }:
            break
        medicine_names.append(medicine)
        cursor += 1

    if not medicine_names:
        return []

    table_values = [line for line in lines[cursor:] if line]
    count = len(medicine_names)
    if len(table_values) < count * 2:
        return []
    strengths = table_values[:count]
    instructions = table_values[count : count * 2]
    if not all(is_strength_value(strength) for strength in strengths):
        return []
    if any(
        re.match(r"(?i)^(?:note:|fictional sample|synthetic demo|patient:|date:)", instruction)
        for instruction in instructions
    ):
        return []
    return list(zip(medicine_names, strengths, instructions, strict=True))


def is_laboratory_row(line: str) -> bool:
    lab_aliases = (
        "hemoglobin", "ferritin", "white blood cell count", "white blood cells",
        "white cell count", "platelets", "platelet count", "fasting glucose",
        "fasting blood glucose", "glucose (fasting)", "glucose fasting",
    )
    lowered = line.lower()
    return any(alias in lowered for alias in lab_aliases) or bool(re.search(r"\b(?:hb|wbc)\b", lowered))


def extract_dosages(text: str) -> list[str]:
    matches: list[str] = []
    structured_rows = extract_structured_medication_table(text)
    structured_medicines = {medicine.casefold() for medicine, _, _ in structured_rows}
    for medicine, strength, instructions in structured_rows:
        pieces = [f"{medicine}: {strength}"]
        if instructions:
            pieces.append(f"instruction: {instructions}")
        matches.append("; ".join(pieces))

    dose_pattern = re.compile(
        r"\b(\d+(?:\.\d+)?)\s*(mg|mcg|g|ml|units|tablet|tablets|capsule|capsules)\b",
        flags=re.IGNORECASE,
    )
    for line in text.splitlines():
        if is_laboratory_row(line):
            continue
        for medicine in KNOWN_DRUGS:
            if medicine.casefold() in structured_medicines:
                continue
            med_match = re.search(rf"(?i)\b{re.escape(medicine)}\b", line)
            if not med_match:
                continue
            dose_match = dose_pattern.search(line, med_match.end())
            if not dose_match:
                continue
            candidate = f"{medicine.title()} {dose_match.group(1)} {dose_match.group(2)}"
            if candidate.casefold() not in {existing.casefold() for existing in matches}:
                matches.append(candidate)
    return matches


def extract_diagnoses(text: str) -> list[str]:
    matches = set()
    common_conditions = [
        "hypertension",
        "diabetes",
        "asthma",
        "anemia",
        "depression",
        "arthritis",
        "anxiety",
        "migraine",
        "obesity",
        "hyperlipidemia",
        "pneumonia",
        "covid-19",
        "chronic kidney disease",
        "heart failure",
        "fatigue",
    ]
    for condition in common_conditions:
        if condition in text.lower():
            matches.add(condition.title())

    section_pattern = re.compile(
        r"(?i)\b(?:diagnosis|diagnoses|assessment|impression|problem list|condition)\s*[:\-]?\s*([^\n]+)",
    )
    for match in section_pattern.finditer(text):
        fragment = match.group(1).strip()
        fragment = re.split(r"\b(?:medication|medications|lab values|assessment|history|review|consult)\b", fragment, flags=re.IGNORECASE)[0]
        fragment = re.sub(r"\s+", " ", fragment).strip(" .;,")
        if fragment and not is_noise_label(fragment) and len(fragment) < 180 and "synthetic" not in fragment.lower():
            matches.add(fragment)
    return sorted(matches)


def extract_lab_values(text: str) -> list[dict[str, str]]:
    allowed_names = {
        "hemoglobin": ("hemoglobin", "hb"),
        "ferritin": ("ferritin",),
        "white blood cell count": ("white blood cell count", "white blood cells (wbc)", "white blood cells", "white cell count", "wbc"),
        "platelets": ("platelets", "platelet count"),
        "fasting glucose": ("fasting glucose", "fasting blood glucose", "glucose (fasting)", "glucose fasting"),
        "total cholesterol": ("total cholesterol",),
        "hdl cholesterol": ("hdl cholesterol", "high-density lipoprotein cholesterol"),
        "creatinine": ("creatinine",),
    }
    display_names = {
        "hemoglobin": "Hemoglobin",
        "ferritin": "Ferritin",
        "white blood cell count": "White blood cells (WBC)",
        "platelets": "Platelets",
        "fasting glucose": "Fasting glucose",
        "total cholesterol": "Total cholesterol",
        "hdl cholesterol": "HDL cholesterol",
        "creatinine": "Creatinine",
    }

    def test_name(line: str) -> str | None:
        normalized = re.sub(r"\s+", " ", line.strip()).casefold()
        for canonical, aliases in allowed_names.items():
            if normalized in {alias.casefold() for alias in aliases}:
                return canonical
        return None

    def next_nonempty(lines: list[str], start: int) -> tuple[int, str] | None:
        for position in range(start, len(lines)):
            if lines[position].strip():
                return position, lines[position].strip()
        return None

    lines = [line.strip() for line in text.splitlines()]
    table_start = None
    for index, line in enumerate(lines):
        if re.fullmatch(r"(?i)test(?:\s+name)?", line.strip()):
            following = [
                candidate.casefold()
                for candidate in lines[index + 1 : index + 7]
                if candidate.strip()
            ]
            if "result" in following and "unit" in following and any("reference range" in value for value in following):
                table_start = index + 1
                break

    labs: list[dict[str, str]] = []
    seen: dict[tuple[str, str], int] = {}

    if table_start is not None:
        cursor = table_start
        while cursor < len(lines):
            current = lines[cursor].strip()
            if not current:
                cursor += 1
                continue
            if current.casefold() in {"result", "unit", "reference range", "flag"}:
                cursor += 1
                continue
            canonical = test_name(current)
            if canonical is None:
                # Table rows stop at the first non-header, non-test line/section.
                break

            result_item = next_nonempty(lines, cursor + 1)
            if result_item is None:
                break
            result_index, result_text = result_item
            result_match = re.fullmatch(r"(?:<|<=|>|>=)?\s*(-?\d+(?:\.\d+)?)", result_text)
            if not result_match:
                cursor += 1
                continue

            unit_item = next_nonempty(lines, result_index + 1)
            range_item = next_nonempty(lines, unit_item[0] + 1) if unit_item else None
            if not unit_item or not range_item:
                break
            unit_index, unit = unit_item
            range_index, reference_range = range_item
            if test_name(unit) or test_name(reference_range):
                cursor += 1
                continue

            flag_item = next_nonempty(lines, range_index + 1)
            status = ""
            next_cursor = range_index + 1
            if flag_item and flag_item[1].strip().casefold() in {"high", "low", "in range"}:
                status = "IN RANGE" if flag_item[1].strip().casefold() == "in range" else flag_item[1].strip().upper()
                next_cursor = flag_item[0] + 1

            normalized_range = re.sub(r"[�–—−]", "-", reference_range).strip()
            lab = {
                "name": display_names[canonical],
                "value": result_match.group(1),
                "unit": unit,
                "reference_range": normalized_range,
            }
            if status:
                lab["status"] = status
            key = (canonical, lab["value"])
            existing_index = seen.get(key)
            if existing_index is None:
                seen[key] = len(labs)
                labs.append(lab)
            else:
                existing = labs[existing_index]
                if (not existing.get("unit"), not existing.get("reference_range"), not existing.get("status")) > (
                    not lab.get("unit"), not lab.get("reference_range"), not lab.get("status")
                ):
                    labs[existing_index] = lab
            cursor = next_cursor

        if labs:
            return labs[:20]

    rows = [line for line in lines if line]
    for row in rows:
        lower_row = row.lower()
        matched_name = None
        for canonical_name, aliases in allowed_names.items():
            for alias in aliases:
                if alias.lower() in lower_row:
                    matched_name = canonical_name
                    break
            if matched_name:
                break
        if not matched_name:
            continue

        preferred_alias = None
        for alias in allowed_names[matched_name]:
            if alias.lower() in lower_row:
                preferred_alias = alias
                break
        if preferred_alias is None:
            continue

        match = re.search(
            rf"(?i)\b(?:{re.escape(preferred_alias)})\b\s*(?:[:=\-])?\s*(\d+(?:\.\d+)?)\s*(?:([A-Za-zµμ/]+(?:/[A-Za-z]+)?)|(?:x10\^?\d+/uL)|(?:K/uL)|(?:cells/uL)|(?:uL))?\s*(?:\(([^)]+)\))?",
            row,
        )
        if not match:
            continue

        value = match.group(1)
        unit = (match.group(2) or "").strip()
        reference = (match.group(3) or "").strip()
        key = (matched_name, value)
        if not value:
            continue

        display_name = {
            "hemoglobin": "Hemoglobin",
            "ferritin": "Ferritin",
            "white blood cell count": "White blood cell count",
            "platelets": "Platelets",
            "fasting glucose": "Fasting glucose",
        }.get(matched_name, matched_name.title())

        lab = {
            "name": display_name,
            "value": value,
            "unit": unit,
            "reference_range": reference,
        }
        existing_index = seen.get(key)
        if existing_index is not None:
            existing = labs[existing_index]
            existing_completeness = (bool(existing["unit"]), bool(existing["reference_range"]))
            candidate_completeness = (bool(unit), bool(reference))
            if candidate_completeness > existing_completeness:
                labs[existing_index] = lab
                continue
            if candidate_completeness < existing_completeness:
                continue
        seen[key] = len(labs)
        labs.append(lab)

    return labs[:20]


def build_summary(fields: dict[str, Any]) -> str:
    document_type = fields.get("document_type") or "medical document"
    document_date = fields.get("document_date") or "Not detected"
    diagnoses = list(dict.fromkeys(fields.get("diagnoses") or []))
    medicines = list(dict.fromkeys(fields.get("medicines") or []))
    lab_values = fields.get("lab_values") or []
    unique_labs: list[dict[str, str]] = []
    seen_labs: set[tuple[str, str, str, str]] = set()
    for lab in lab_values:
        key = (
            str(lab.get("name", "")).casefold().strip(),
            str(lab.get("value", "")).strip(),
            str(lab.get("unit", "")).casefold().strip(),
            str(lab.get("reference_range", "")).strip(),
        )
        if key not in seen_labs:
            seen_labs.add(key)
            unique_labs.append(lab)
    lab_values = unique_labs

    sections = [
        f"This appears to be a {document_type}.",
        f"Document date recorded: {document_date}.",
    ]
    if diagnoses:
        sections.append(f"Diagnoses mentioned in the source: {', '.join(diagnoses)}.")
    else:
        sections.append("No diagnosis was explicitly stated in the source text.")
    if medicines:
        sections.append(f"Medicines mentioned: {', '.join(medicines)}.")
    else:
        sections.append("No medicines were explicitly listed in the source text.")
    if lab_values:
        formatted = ", ".join(
            f"{item.get('name', 'Lab item')} {item.get('value', 'Not detected')}"
            + (f" {item['unit']}" if item.get("unit") else "")
            + (f" (reference range {item['reference_range']})" if item.get("reference_range") else "")
            + (f" (reported status: {item['status']})" if item.get("status") else "")
            for item in lab_values
        )
        sections.append(f"Lab findings identified: {formatted}.")
    else:
        sections.append("No verified numeric lab values were identified in the source text.")
    sections.append(
        "This summary is informational only, is not a diagnosis, and does not recommend medication changes or other treatment decisions."
    )
    return " ".join(sections)


def parse_document(raw_text: str, filename: str) -> dict[str, Any]:
    normalized = normalize_whitespace(raw_text)
    date = detect_date(normalized) or "Not detected"
    doc_type = detect_document_type(filename, normalized) or "Not detected"
    medicines = extract_medicines(normalized)
    dosages = extract_dosages(raw_text)
    diagnoses = extract_diagnoses(normalized)
    lab_values = extract_lab_values(raw_text)
    return {
        "document_date": date,
        "document_type": doc_type,
        "medicines": medicines if medicines else [],
        "dosages": dosages if dosages else [],
        "diagnoses": diagnoses if diagnoses else [],
        "lab_values": lab_values,
        "summary": build_summary({
            "document_type": doc_type,
            "document_date": date,
            "diagnoses": diagnoses,
            "medicines": medicines,
            "lab_values": lab_values,
        }),
    }


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return render_page(request, "home.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render_page(request, "auth.html", mode="login")


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request) -> HTMLResponse:
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render_page(request, "auth.html", mode="signup")


def protected_page(request: Request, template_name: str, **context: Any) -> Response:
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    return render_page(request, template_name, **context)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> Response:
    return protected_page(request, "dashboard.html", active_page="overview")


@app.get("/documents", response_class=HTMLResponse)
async def documents_page(request: Request) -> Response:
    return protected_page(request, "upload.html", active_page="upload")


@app.get("/timeline", response_class=HTMLResponse)
async def timeline_page(request: Request, category: str | None = None) -> Response:
    return protected_page(request, "timeline.html", active_page="timeline", selected_category=category or "")


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render_page(request, "profile.html", active_page="profile", profile_user=user)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Response:
    return protected_page(request, "settings.html", active_page="settings")


@app.get("/records/{record_id}", response_class=HTMLResponse)
async def record_page(request: Request, record_id: int) -> Response:
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = get_db_connection()
    record = conn.execute(
        "SELECT id, filename, document_type, document_date, status, is_synthetic FROM documents WHERE id = ? AND owner_id = ?",
        (record_id, user["id"]),
    ).fetchone()
    conn.close()
    if not record:
        return render_page(request, "not_found.html", active_page="timeline")
    return render_page(request, "record.html", active_page="timeline", record=record)


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request) -> HTMLResponse:
    return render_page(request, "about.html")


@app.get("/contact", response_class=HTMLResponse)
async def contact_page(request: Request) -> HTMLResponse:
    return render_page(request, "contact.html")


@app.post("/api/auth/signup")
async def signup(request: Request) -> JSONResponse:
    payload = await request.json()
    name = str(payload.get("name", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    if len(name) < 2 or len(name) > 100:
        raise HTTPException(status_code=400, detail="Enter a name between 2 and 100 characters.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if len(password) < 10 or len(password) > 128:
        raise HTTPException(status_code=400, detail="Password must be between 10 and 128 characters.")
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
            (email, password_digest(password), name, datetime.utcnow().isoformat(timespec="seconds")),
        )
        conn.commit()
        user_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    conn.close()
    response = JSONResponse({"status": "created", "redirect": "/dashboard"}, status_code=201)
    set_session_cookie(response, int(user_id))
    return response


@app.post("/api/auth/login")
async def login(request: Request) -> JSONResponse:
    payload = await request.json()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    conn = get_db_connection()
    user = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    if not user or not password_matches(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    response = JSONResponse({"status": "authenticated", "redirect": "/dashboard"})
    set_session_cookie(response, int(user["id"]))
    return response


@app.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        conn = get_db_connection()
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode("utf-8")).hexdigest(),))
        conn.commit()
        conn.close()
    response = JSONResponse({"status": "logged_out", "redirect": "/"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
async def auth_me(request: Request) -> JSONResponse:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return JSONResponse({"id": user["id"], "email": user["email"], "display_name": user["display_name"], "created_at": user["created_at"]})


@app.put("/api/profile")
async def update_profile(request: Request) -> JSONResponse:
    user = require_user(request)
    payload = await request.json()
    display_name = str(payload.get("display_name", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    if not 2 <= len(display_name) <= 100:
        raise HTTPException(status_code=400, detail="Display name must be between 2 and 100 characters.")
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET display_name = ?, email = ? WHERE id = ?", (display_name, email, user["id"]))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    updated = conn.execute("SELECT id, email, display_name, created_at FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()
    return JSONResponse({"status": "updated", "user": dict(updated)})


@app.get("/api/security/info")
async def security_info(request: Request) -> JSONResponse:
    user = require_user(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = get_db_connection()
    current = conn.execute(
        "SELECT created_at, expires_at FROM sessions WHERE token_hash = ? AND user_id = ?",
        (token_hash, user["id"]),
    ).fetchone()
    count = conn.execute("SELECT COUNT(*) AS count FROM sessions WHERE user_id = ?", (user["id"],)).fetchone()["count"]
    record_count = conn.execute("SELECT COUNT(*) AS count FROM documents WHERE owner_id = ?", (user["id"],)).fetchone()["count"]
    conn.close()
    if not current:
        raise HTTPException(status_code=401, detail="Session has expired. Please log in again.")
    return JSONResponse({
        "session_created_at": current["created_at"],
        "session_expires_at": current["expires_at"],
        "active_sessions": count,
        "saved_document_count": record_count,
        "cookie_http_only": True,
        "cookie_same_site": "lax",
    })


@app.post("/api/security/revoke-other-sessions")
async def revoke_other_sessions(request: Request) -> JSONResponse:
    user = require_user(request)
    token_hash = hashlib.sha256(request.cookies.get(SESSION_COOKIE, "").encode("utf-8")).hexdigest()
    conn = get_db_connection()
    cursor = conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user["id"], token_hash))
    conn.commit()
    revoked = cursor.rowcount
    conn.close()
    return JSONResponse({"status": "revoked", "revoked_sessions": revoked})


@app.post("/api/profile/password")
async def update_password(request: Request) -> JSONResponse:
    user = require_user(request)
    payload = await request.json()
    current_password = str(payload.get("current_password", ""))
    new_password = str(payload.get("new_password", ""))
    if len(new_password) < 10 or len(new_password) > 128:
        raise HTTPException(status_code=400, detail="New password must be between 10 and 128 characters.")
    conn = get_db_connection()
    account = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not account or not password_matches(current_password, account["password_hash"]):
        conn.close()
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_digest(new_password), user["id"]))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    conn.commit()
    conn.close()
    response = JSONResponse({"status": "password_updated", "redirect": "/login"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.post("/api/contact")
async def submit_feedback(request: Request) -> JSONResponse:
    user = current_user(request)
    payload = await request.json()
    name = str(payload.get("name", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    message = str(payload.get("message", "")).strip()
    if len(name) < 2 or len(name) > 100:
        raise HTTPException(status_code=400, detail="Enter a name between 2 and 100 characters.")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if len(message) < 10 or len(message) > 4000:
        raise HTTPException(status_code=400, detail="Message must be between 10 and 4,000 characters.")
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO feedback (user_id, name, email, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (user["id"] if user else None, name, email, message, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return JSONResponse({"status": "received", "message": "Your feedback was saved locally for this project. It was not emailed."}, status_code=201)


@app.get("/api/health")
async def healthcheck() -> JSONResponse:
    return JSONResponse({"status": "ok", "app": APP_NAME, "db": str(DB_PATH)})


@app.post("/api/upload")
async def upload_document(request: Request, file: UploadFile = File(...)) -> JSONResponse:
    user = require_user(request)
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file selected.")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Allowed types are PDF, JPG, and PNG only.")

    original_name = safe_filename(file.filename)
    file_id = uuid.uuid4().hex
    user_upload_dir = UPLOAD_DIR / f"user_{user['id']}"
    user_upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = user_upload_dir / f"{file_id}_{original_name}"
    file_bytes = await file.read()
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 10 MB upload limit.")
    saved_path.write_bytes(file_bytes)

    try:
        extracted_text = read_text_file(saved_path)
    except OCRUnavailableError as error:
        raise HTTPException(
            status_code=503,
            detail=f"{error} Original retained at {saved_path.relative_to(BASE_DIR)}.",
        ) from error
    except (OCRProcessingError, DocumentReadError) as error:
        raise HTTPException(
            status_code=422,
            detail=f"{error} Original retained at {saved_path.relative_to(BASE_DIR)}.",
        ) from error
    extracted_fields = parse_document(extracted_text, original_name)
    source_type = "pdf" if ext == ".pdf" else "image"
    is_synthetic = "synthetic" in original_name.lower()

    return JSONResponse(
        {
            "message": "Document processed.",
            "filename": original_name,
            "stored_path": str(saved_path.relative_to(BASE_DIR)),
            "source_type": source_type,
            "is_synthetic": is_synthetic,
            "ocr_available": tesseract_is_available(),
            "extracted_text": extracted_text[:4000],
            "fields": extracted_fields,
            "summary": extracted_fields["summary"],
        }
    )


@app.post("/api/save")
async def save_record(request: Request) -> JSONResponse:
    user = require_user(request)
    payload = await request.json()
    required = ["filename", "stored_path", "document_date", "document_type", "summary"]
    for item in required:
        if item not in payload:
            raise HTTPException(status_code=400, detail=f"Missing required field: {item}")

    requested_path = (BASE_DIR / str(payload["stored_path"])).resolve()
    user_upload_root = (UPLOAD_DIR / f"user_{user['id']}").resolve()
    sample_root = SAMPLE_DIR.resolve()
    if not (
        (requested_path.is_relative_to(user_upload_root) and requested_path.is_file())
        or (requested_path.is_relative_to(sample_root) and requested_path.is_file())
    ):
        raise HTTPException(status_code=403, detail="This original document is not available to your account.")

    medicines = json.dumps(payload.get("medicines", []), ensure_ascii=False)
    dosages = json.dumps(payload.get("dosages", []), ensure_ascii=False)
    diagnoses = json.dumps(payload.get("diagnoses", []), ensure_ascii=False)
    lab_values = json.dumps(payload.get("lab_values", []), ensure_ascii=False)
    raw_text = payload.get("raw_text", "")
    source_type = payload.get("source_type", "unknown")
    is_synthetic = 1 if payload.get("is_synthetic") else 0

    conn = get_db_connection()
    cursor = conn.execute(
        """
        INSERT INTO documents (
            filename, stored_path, document_date, document_type, medicines, dosages,
            diagnoses, lab_values, summary, raw_text, source_type, is_synthetic, created_at, status, owner_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["filename"],
            payload["stored_path"],
            payload["document_date"],
            payload["document_type"],
            medicines,
            dosages,
            diagnoses,
            lab_values,
            payload["summary"],
            raw_text,
            source_type,
            is_synthetic,
            datetime.utcnow().isoformat(timespec="seconds"),
            "verified",
            user["id"],
        ),
    )
    conn.commit()
    conn.close()

    return JSONResponse({"status": "saved", "record_id": cursor.lastrowid})


@app.get("/api/timeline")
async def get_timeline(request: Request, category: str | None = None) -> JSONResponse:
    user = require_user(request)
    conn = get_db_connection()
    query = "SELECT * FROM documents WHERE owner_id = ?"
    parameters: tuple[Any, ...] = (user["id"],)
    if category:
        query += " AND COALESCE(NULLIF(document_type, ''), 'Uncategorized') = ?"
        parameters += (category,)
    query += " ORDER BY CASE WHEN document_date = 'Not detected' THEN created_at ELSE document_date END DESC, created_at DESC"
    rows = conn.execute(query, parameters).fetchall()
    conn.close()

    return JSONResponse({"records": [serialize_document(row) for row in rows]})


@app.get("/api/dashboard")
async def get_dashboard_data(request: Request) -> JSONResponse:
    user = require_user(request)
    conn = get_db_connection()
    total = conn.execute("SELECT COUNT(*) AS count FROM documents WHERE owner_id = ?", (user["id"],)).fetchone()["count"]
    recent_rows = conn.execute(
        "SELECT id, filename, document_date, document_type, status, is_synthetic FROM documents WHERE owner_id = ? ORDER BY created_at DESC LIMIT 5",
        (user["id"],),
    ).fetchall()
    category_rows = conn.execute(
        "SELECT COALESCE(NULLIF(document_type, ''), 'Uncategorized') AS document_type, COUNT(*) AS count FROM documents WHERE owner_id = ? GROUP BY COALESCE(NULLIF(document_type, ''), 'Uncategorized') ORDER BY count DESC",
        (user["id"],),
    ).fetchall()
    conn.close()
    return JSONResponse({
        "document_count": total,
        "recent": [dict(row) for row in recent_rows],
        "recent_count": len(recent_rows),
        "categories": [dict(row) for row in category_rows],
    })


def dosage_duplicates_lab_result(dosage: str, lab_values: list[dict[str, str]]) -> bool:
    # Preserve an actual medication instruction even if its dose equals a lab result.
    if any(re.search(rf"(?i)\b{re.escape(medicine)}\b", dosage) for medicine in KNOWN_DRUGS):
        return False
    if is_laboratory_row(dosage):
        return True
    measurement = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(mg|mcg|g|ml|units|tablet|tablets|capsule|capsules)\b",
        dosage,
        flags=re.IGNORECASE,
    )
    if not measurement:
        return False
    amount, unit = measurement.group(1), measurement.group(2).casefold()
    for lab in lab_values:
        lab_unit = str(lab.get("unit", "")).casefold()
        unit_base = lab_unit.split("/", 1)[0]
        if str(lab.get("value", "")).strip() == amount and unit in {lab_unit, unit_base}:
            return True
    return False


def serialize_document(row: sqlite3.Row) -> dict[str, Any]:
    medicines = json.loads(row["medicines"] or "[]")
    dosages = json.loads(row["dosages"] or "[]")
    diagnoses = json.loads(row["diagnoses"] or "[]")
    lab_values = json.loads(row["lab_values"] or "[]")
    cleaned_dosages = [dose for dose in dosages if not dosage_duplicates_lab_result(str(dose), lab_values)]
    summary = row["summary"] or ""
    duplicated_lab_summary = any(
        summary.count(f"{lab.get('name', 'Lab item')} {lab.get('value', '')}{lab.get('unit', '')}") > 1
        for lab in lab_values
    )
    if cleaned_dosages != dosages or duplicated_lab_summary:
        summary = build_summary({
            "document_type": row["document_type"],
            "document_date": row["document_date"],
            "medicines": medicines,
            "diagnoses": diagnoses,
            "lab_values": lab_values,
        })

    return {
        "id": row["id"],
        "filename": row["filename"],
        "stored_path": row["stored_path"],
        "document_date": row["document_date"],
        "document_type": row["document_type"],
        "medicines": medicines,
        "dosages": cleaned_dosages,
        "diagnoses": diagnoses,
        "lab_values": lab_values,
        "summary": summary,
        "raw_text": row["raw_text"] or "",
        "source_type": row["source_type"],
        "is_synthetic": bool(row["is_synthetic"]),
        "created_at": row["created_at"],
        "status": row["status"],
    }


@app.get("/api/records/{record_id}")
async def get_record(request: Request, record_id: int) -> JSONResponse:
    user = require_user(request)
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM documents WHERE id = ? AND owner_id = ?", (record_id, user["id"])).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Record not found.")
    return JSONResponse(serialize_document(row))


@app.get("/api/records/{record_id}/file")
async def get_record_file(request: Request, record_id: int) -> FileResponse:
    user = require_user(request)
    conn = get_db_connection()
    row = conn.execute("SELECT stored_path, filename FROM documents WHERE id = ? AND owner_id = ?", (record_id, user["id"])).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Record not found.")
    resolved_path = (BASE_DIR / row["stored_path"]).resolve()
    allowed_roots = [UPLOAD_DIR.resolve(), SAMPLE_DIR.resolve()]
    if not any(resolved_path.is_relative_to(root) for root in allowed_roots) or not resolved_path.is_file():
        raise HTTPException(status_code=404, detail="Original file is unavailable.")
    media_type = "application/pdf" if resolved_path.suffix.lower() == ".pdf" else f"image/{'jpeg' if resolved_path.suffix.lower() in {'.jpg', '.jpeg'} else 'png'}"
    return FileResponse(resolved_path, filename=row["filename"], media_type=media_type, content_disposition_type="inline")


def generate_sample_pdf(path: Path, title: str, body_text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), f"SYNTHETIC SAMPLE - {title}", fontsize=18)
    page.insert_text((72, 120), body_text, fontsize=11)
    doc.save(path)
    doc.close()


def create_demo_documents() -> list[Path]:
    samples = [
        (
            "CBC_Lab_Report",
            "SYNTHETIC SAMPLE - CBC LAB REPORT\nDate: 2025-02-14\nDocument type: Lab report\nHemoglobin 12.4 g/dL (12-16)\nPlatelets 245 K/uL (150-450)\nSodium 140 mmol/L (135-145)\nDiagnosis: Anemia not confirmed; review with clinician.",
        ),
        (
            "Annual_Visit_Note",
            "SYNTHETIC SAMPLE - ANNUAL VISIT NOTE\nDate: 2025-03-22\nProgress note\nDiagnosis: Hypertension and elevated cholesterol discussed.\nMedication: Atorvastatin 20 mg daily; Lisinopril 10 mg daily.\nAssessment: Blood pressure stable, values reviewed.",
        ),
        (
            "Medication_Review",
            "SYNTHETIC SAMPLE - MEDICATION REVIEW\nDate: 2025-04-10\nMedication list\nAtorvastatin 20 mg tablet daily\nMetformin 500 mg twice daily\nDiagnosis: Type 2 diabetes noted in history.\nNo medication changes were instructed by this system.",
        ),
        (
            "Discharge_Summary",
            "SYNTHETIC SAMPLE - DISCHARGE SUMMARY\nDate: 2025-05-02\nDischarge summary\nDiagnosis: Asthma flare resolved.\nMedications: Albuterol 2 puffs every 4 hours, as needed.\nLab values: Potassium 4.1 mEq/L (3.5-5.0), Creatinine 0.9 mg/dL (0.6-1.1).",
        ),
        (
            "Follow_Up_Consult",
            "SYNTHETIC SAMPLE - FOLLOW-UP CONSULT\nDate: 2025-06-18\nSpecialist consult\nDiagnosis: Persisting fatigue, no confirmed diagnosis.\nLab values: Ferritin 89 ng/mL (30-400), Hemoglobin 11.7 g/dL (12-16).\nMedication list: Iron supplement 325 mg once daily.",
        ),
    ]

    created: list[Path] = []
    for index, (name, content) in enumerate(samples, start=1):
        file_path = SAMPLE_DIR / f"synthetic_{index:02d}_{name}.pdf"
        generate_sample_pdf(file_path, name, content)
        created.append(file_path)
    return created


@app.post("/api/demo/load")
async def load_demo_documents(request: Request) -> JSONResponse:
    user = require_user(request)
    conn = get_db_connection()
    conn.execute("DELETE FROM documents WHERE is_synthetic = 1 AND owner_id = ?", (user["id"],))
    conn.commit()
    conn.close()

    created = create_demo_documents()
    records = []
    for file_path in created:
        text = read_text_file(file_path)
        fields = parse_document(text, file_path.name)
        relative_path = str(file_path.relative_to(BASE_DIR))
        record = {
            "filename": file_path.name,
            "stored_path": relative_path,
            "source_type": "pdf",
            "is_synthetic": True,
            "raw_text": text,
            **fields,
        }
        records.append(record)
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO documents (
                filename, stored_path, document_date, document_type, medicines, dosages,
                diagnoses, lab_values, summary, raw_text, source_type, is_synthetic, created_at, status, owner_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_path.name,
                relative_path,
                fields["document_date"],
                fields["document_type"],
                json.dumps(fields["medicines"], ensure_ascii=False),
                json.dumps(fields["dosages"], ensure_ascii=False),
                json.dumps(fields["diagnoses"], ensure_ascii=False),
                json.dumps(fields["lab_values"], ensure_ascii=False),
                fields["summary"],
                text,
                "pdf",
                1,
                datetime.utcnow().isoformat(timespec="seconds"),
                "verified",
                user["id"],
            ),
        )
        conn.commit()
        conn.close()
    return JSONResponse({"status": "demo_loaded", "count": len(records), "records": records})


@app.on_event("startup")
async def startup_event() -> None:
    init_db()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
