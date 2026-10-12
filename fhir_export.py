"""Prototype FHIR R4-style export mapping for reviewed MediVault records."""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
import mimetypes


MOCK_ABHA_SYSTEM = "urn:medivault:prototype:mock-abha"
MOCK_ABHA_LABEL = "MOCK DATA ONLY — NOT AN ABHA ID"
PROTOTYPE_LABEL = "FHIR-style export (prototype; not validated for full FHIR or ABDM compliance)"


def _resource_id() -> str:
    return str(uuid.uuid4())


def _entry(resource: dict[str, Any]) -> dict[str, Any]:
    return {"fullUrl": f"urn:uuid:{resource['id']}", "resource": resource}


def _reference(resource: dict[str, Any]) -> dict[str, str]:
    return {"reference": f"urn:uuid:{resource['id']}"}


def _valid_fhir_date(value: Any) -> str | None:
    text = str(value or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _quantity(value: Any) -> dict[str, Any] | None:
    text = str(value or "").strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return {"value": float(number)}


def _associated_dosages(medicine: str, dosages: list[Any]) -> list[str]:
    """Return only dosage strings with an explicit medicine-name prefix."""
    matched = []
    medicine_key = medicine.casefold()
    for dosage in dosages:
        text = str(dosage)
        if text.casefold().startswith(medicine_key) and len(text) > len(medicine):
            if text[len(medicine)] in ": \t":
                matched.append(text)
    return matched


def build_fhir_bundle(
    record: dict[str, Any],
    *,
    file_url: str,
    export_id: str | None = None,
) -> dict[str, Any]:
    """Map one owner-authorized, reviewed MediVault record to a FHIR-style Bundle."""
    patient = {
        "resourceType": "Patient",
        "id": _resource_id(),
        "name": [{"text": "Synthetic / unknown demo subject"}],
        "identifier": [{
            "system": MOCK_ABHA_SYSTEM,
            "value": f"MOCK-ABHA-NOT-REAL-{uuid.uuid4()}",
            "type": {"text": MOCK_ABHA_LABEL},
        }],
    }

    observations: list[dict[str, Any]] = []
    for lab in record.get("lab_values") or []:
        observation: dict[str, Any] = {
            "resourceType": "Observation",
            "id": _resource_id(),
            "status": "unknown",
            "code": {"text": str(lab.get("name", ""))},
            "subject": _reference(patient),
        }
        source_value = str(lab.get("value", ""))
        quantity = _quantity(source_value)
        if quantity is None:
            observation["valueString"] = source_value
        else:
            quantity["unit"] = str(lab.get("unit", ""))
            observation["valueQuantity"] = quantity
            observation["extension"] = [{
                "url": "urn:medivault:prototype:source-value",
                "valueString": source_value,
            }]
        if lab.get("unit") and quantity is None:
            observation.setdefault("note", []).append({"text": f"Source unit: {lab['unit']}"})
        if lab.get("reference_range"):
            observation["referenceRange"] = [{"text": str(lab["reference_range"])}]
        if lab.get("status"):
            # This is source interpretation, not the FHIR Observation.lifecycle status.
            observation["interpretation"] = [{"text": str(lab["status"])}]
        observations.append(observation)

    doc_id = str(record.get("id", ""))
    content_type = mimetypes.guess_type(str(record.get("filename", "")))[0] or "application/octet-stream"
    document_reference = {
        "resourceType": "DocumentReference",
        "id": _resource_id(),
        "status": "current",
        "type": {"text": str(record.get("document_type") or "")},
        "subject": _reference(patient),
        "identifier": [{"system": "urn:medivault:prototype:record", "value": doc_id}],
        "description": (
            f"MediVault source_type={record.get('source_type') or 'unknown'}; "
            f"synthetic_sample={bool(record.get('is_synthetic'))}; "
            f"saved_status={record.get('status') or 'unknown'}; "
            f"record_created_at={record.get('created_at') or 'unknown'}"
        ),
        "content": [{"attachment": {
            "contentType": content_type,
            "url": file_url,
            "title": str(record.get("filename") or "Original document"),
        }}],
    }

    report: dict[str, Any] = {
        "resourceType": "DiagnosticReport",
        "id": _resource_id(),
        "status": "unknown",
        "code": {"text": str(record.get("document_type") or "")},
        "subject": _reference(patient),
        "presentedForm": [{"contentType": content_type, "url": file_url, "title": str(record.get("filename") or "Original document")}],
    }
    source_date = str(record.get("document_date") or "")
    valid_date = _valid_fhir_date(source_date)
    if valid_date:
        report["effectiveDateTime"] = valid_date
    if source_date:
        report.setdefault("note", []).append({"text": f"Source document date (preserved): {source_date}"})
    diagnoses = [str(item) for item in (record.get("diagnoses") or []) if str(item)]
    if diagnoses:
        report["conclusion"] = diagnoses
    if observations:
        report["result"] = [_reference(item) for item in observations]

    entries = [_entry(patient)]
    entries.extend(_entry(item) for item in observations)
    entries.append(_entry(report))
    entries.append(_entry(document_reference))

    # Lists are stored independently. Only create medication resources when a dosage
    # string explicitly starts with the matching stored medicine name.
    for medicine in dict.fromkeys(str(item) for item in (record.get("medicines") or []) if str(item)):
        associated = _associated_dosages(medicine, record.get("dosages") or [])
        if not associated:
            continue
        medication = {
            "resourceType": "Medication",
            "id": _resource_id(),
            "code": {"text": medicine},
        }
        statement = {
            "resourceType": "MedicationStatement",
            "id": _resource_id(),
            "status": "unknown",
            "medicationReference": _reference(medication),
            "subject": _reference(patient),
            "derivedFrom": [_reference(document_reference)],
            "dosage": [{"text": text} for text in associated],
        }
        entries.extend((_entry(medication), _entry(statement)))

    return {
        "resourceType": "Bundle",
        "id": export_id or _resource_id(),
        "meta": {"tag": [{
            "system": "urn:medivault:prototype:export",
            "code": "mock-demo",
            "display": PROTOTYPE_LABEL,
        }]},
        "type": "collection",
        "entry": entries,
    }
