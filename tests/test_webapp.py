import tempfile
import unittest
import json
from io import BytesIO
from pathlib import Path

import fitz
from fastapi.testclient import TestClient
from PIL import Image

import app as medivault


class MediVaultWebWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=medivault.BASE_DIR)
        self.root = Path(self.temp_dir.name)
        self.old_db_path = medivault.DB_PATH
        self.old_upload_dir = medivault.UPLOAD_DIR
        self.old_sample_dir = medivault.SAMPLE_DIR
        medivault.DB_PATH = self.root / "test.db"
        medivault.UPLOAD_DIR = self.root / "originals"
        medivault.SAMPLE_DIR = self.root / "samples"
        medivault.init_db()
        self.client_context = TestClient(medivault.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        medivault.DB_PATH = self.old_db_path
        medivault.UPLOAD_DIR = self.old_upload_dir
        medivault.SAMPLE_DIR = self.old_sample_dir
        self.temp_dir.cleanup()

    def make_pdf(self) -> bytes:
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "SYNTHETIC SAMPLE - LAB REPORT")
        page.insert_text((72, 100), "Date: 2025-02-14")
        page.insert_text((72, 128), "Hemoglobin 12.4 g/dL (12-16)")
        data = document.tobytes()
        document.close()
        return data

    def signup(self, client: TestClient, email: str) -> None:
        response = client.post(
            "/api/auth/signup",
            json={"name": "Test User", "email": email, "password": "very-secure-test-password"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue(client.cookies.get(medivault.SESSION_COOKIE))

    def test_protected_medical_record_workflow_and_account_isolation(self) -> None:
        anonymous = TestClient(medivault.app)
        for path in ("/dashboard", "/documents", "/timeline", "/profile", "/settings"):
            self.assertEqual(anonymous.get(path, follow_redirects=False).status_code, 303)
        self.assertEqual(anonymous.get("/api/timeline").status_code, 401)
        self.assertEqual(anonymous.get("/").status_code, 200)
        self.assertEqual(anonymous.get("/about").status_code, 200)
        self.assertEqual(anonymous.get("/contact").status_code, 200)
        self.assertEqual(anonymous.get("/login").status_code, 200)
        self.assertEqual(anonymous.get("/signup").status_code, 200)
        anonymous.close()

        self.signup(self.client, "one@example.test")
        dashboard_page = self.client.get("/dashboard")
        self.assertEqual(dashboard_page.status_code, 200)
        self.assertIn('data-logout', dashboard_page.text)
        self.assertIn('value="cancel">Cancel', dashboard_page.text)
        self.assertIn('value="logout">Log out', dashboard_page.text)
        self.assertEqual(self.client.get("/api/dashboard").json()["document_count"], 0)

        upload = self.client.post(
            "/api/upload",
            files={"file": ("synthetic_lab_report_medivault.pdf", self.make_pdf(), "application/pdf")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        extraction = upload.json()
        self.assertEqual(extraction["fields"]["document_date"], "2025-02-14")
        self.assertEqual(extraction["fields"]["document_type"], "Lab report")
        self.assertIn("Hemoglobin 12.4", extraction["extracted_text"])

        save = self.client.post(
            "/api/save",
            json={
                "filename": extraction["filename"],
                "stored_path": extraction["stored_path"],
                "document_date": extraction["fields"]["document_date"],
                "document_type": extraction["fields"]["document_type"],
                "medicines": [],
                "dosages": [],
                "diagnoses": [],
                "lab_values": [{"name": "Hemoglobin", "value": "12.4", "unit": "g/dL", "reference_range": "12-16"}],
                "summary": extraction["summary"],
                "raw_text": extraction["extracted_text"],
                "source_type": "pdf",
                "is_synthetic": True,
            },
        )
        self.assertEqual(save.status_code, 200, save.text)
        record_id = save.json()["record_id"]
        self.assertEqual(self.client.get("/timeline").status_code, 200)
        timeline = self.client.get("/api/timeline").json()["records"]
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]["id"], record_id)
        detail = self.client.get(f"/api/records/{record_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["raw_text"], extraction["extracted_text"])
        source_file_before = self.client.get(f"/api/records/{record_id}/file")
        self.assertEqual(source_file_before.status_code, 200)
        export = self.client.get(f"/api/records/{record_id}/fhir")
        self.assertEqual(export.status_code, 200, export.text)
        self.assertIn("attachment; filename=", export.headers["content-disposition"])
        self.assertEqual(export.headers["content-type"], "application/fhir+json")
        export_bundle = export.json()
        self.assertEqual(export_bundle["resourceType"], "Bundle")
        self.assertIn("MOCK-ABHA-NOT-REAL-", str(export_bundle))
        self.assertIn("MOCK DATA ONLY", str(export_bundle))
        self.assertEqual(self.client.get(f"/records/{record_id}").status_code, 200)
        page = self.client.get(f"/records/{record_id}")
        self.assertIn('id="recordSummaryLanguage"', page.text)
        self.assertIn("FHIR-style export (prototype; not validated for full FHIR or ABDM compliance)", page.text)
        self.assertIn("Mock data only", page.text)
        self.assertIn(f'href="/api/records/{record_id}/fhir"', page.text)
        telugu_update = self.client.put(f"/api/records/{record_id}/summary", json={"language": "te"})
        self.assertEqual(telugu_update.status_code, 200, telugu_update.text)
        self.assertEqual(telugu_update.json()["summary_language"], "te")
        reopened = self.client.get(f"/api/records/{record_id}").json()
        self.assertEqual(reopened["summary_language"], "te")
        self.assertEqual(reopened["raw_text"], extraction["extracted_text"])
        english_update = self.client.put(f"/api/records/{record_id}/summary", json={"language": "en"})
        self.assertEqual(english_update.status_code, 200, english_update.text)
        self.assertEqual(self.client.get(f"/api/records/{record_id}").json()["summary_language"], "en")
        self.assertEqual(self.client.get(f"/api/records/{record_id}/file").content, source_file_before.content)

        anonymous_export = TestClient(medivault.app)
        self.assertEqual(anonymous_export.get(f"/api/records/{record_id}/fhir").status_code, 401)
        anonymous_export.close()
        second_user = TestClient(medivault.app)
        self.signup(second_user, "two@example.test")
        self.assertEqual(second_user.get("/api/timeline").json()["records"], [])
        self.assertEqual(second_user.get(f"/api/records/{record_id}").status_code, 404)
        self.assertEqual(second_user.get(f"/api/records/{record_id}/file").status_code, 404)
        self.assertEqual(second_user.put(f"/api/records/{record_id}/summary", json={"language": "te"}).status_code, 404)
        self.assertEqual(second_user.get(f"/api/records/{record_id}/fhir").status_code, 404)
        second_user.close()

        feedback = self.client.post(
            "/api/contact",
            json={"name": "Test User", "email": "one@example.test", "message": "A useful workflow feedback message."},
        )
        self.assertEqual(feedback.status_code, 201)
        self.assertIn("not emailed", feedback.json()["message"])
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_login_validation_password_change_and_logout(self) -> None:
        email = "security@example.test"
        original_password = "very-secure-test-password"
        self.signup(self.client, email)

        failed_login = self.client.post("/api/auth/login", json={"email": email, "password": "wrong-password"})
        self.assertEqual(failed_login.status_code, 401)

        self.client.post("/api/auth/logout")
        successful_login = self.client.post("/api/auth/login", json={"email": email, "password": original_password})
        self.assertEqual(successful_login.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").json()["email"], email)

        short_password = self.client.post(
            "/api/profile/password",
            json={"current_password": original_password, "new_password": "short"},
        )
        self.assertEqual(short_password.status_code, 400)
        changed = self.client.post(
            "/api/profile/password",
            json={"current_password": original_password, "new_password": "replacement-secure-password"},
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

        relogin = self.client.post(
            "/api/auth/login",
            json={"email": email, "password": "replacement-secure-password"},
        )
        self.assertEqual(relogin.status_code, 200)

    def test_lab_units_are_not_repeated_as_medication_dosages(self) -> None:
        source = """SYNTHETIC LAB REPORT
Date: 2025-02-14
Hemoglobin 11.7 g/dL (12-16)
Fasting glucose 94 mg/dL (70-99)
range Hemoglobin 11.7 g
Fasting glucose 94 mg
11.7 g
94 mg
Medication: Metformin 500 mg twice daily
"""
        fields = medivault.parse_document(source, "synthetic_lab_report.pdf")
        self.assertEqual(fields["lab_values"], [
            {"name": "Hemoglobin", "value": "11.7", "unit": "g/dL", "reference_range": "12-16"},
            {"name": "Fasting glucose", "value": "94", "unit": "mg/dL", "reference_range": "70-99"},
        ])
        self.assertEqual(fields["dosages"], ["Metformin 500 mg"])
        self.assertNotIn("Hemoglobin", " ".join(fields["dosages"]))
        self.assertEqual(fields["summary"].count("Hemoglobin 11.7 g/dL"), 1)

        repeated_source = "Hemoglobin 11.7 g/dL (12-16)\nHemoglobin 11.7 g/dL (12-16)"
        repeated_fields = medivault.parse_document(repeated_source, "repeated_lab.pdf")
        self.assertEqual(len(repeated_fields["lab_values"]), 2)

        inline_prescription = "Medicine name | Strength | Instructions\nParacetamol | 500 mg | 1 tablet as needed"
        inline_dosages = medivault.extract_dosages(inline_prescription)
        self.assertEqual(len(inline_dosages), 1)
        self.assertEqual(inline_dosages[0], "Paracetamol: 500 mg; instruction: 1 tablet as needed")

    def test_english_and_telugu_summaries_preserve_source_facts(self) -> None:
        fields = {
            "document_type": "Lab report",
            "document_date": "2025-02-14",
            "diagnoses": ["Type 2 diabetes"],
            "medicines": ["Metformin"],
            "dosages": ["Metformin 500 mg twice daily"],
            "lab_values": [{
                "name": "Fasting glucose", "value": "94.0", "unit": "mg/dL",
                "reference_range": "70-99", "status": "IN RANGE",
            }],
        }
        english = medivault.build_summary(fields)
        telugu = medivault.build_summary(fields, language="te")

        self.assertIn("This appears to be a Lab report", english)
        self.assertIn("Document date recorded: 2025-02-14", english)
        self.assertIn("Metformin", english)
        self.assertIn("Fasting glucose 94.0 mg/dL (reference range 70-99)", english)
        self.assertIn("ఇది Lab reportగా కనిపిస్తోంది", telugu)
        self.assertIn("2025-02-14", telugu)
        self.assertIn("Type 2 diabetes", telugu)
        self.assertIn("Metformin", telugu)
        self.assertIn("94.0 mg/dL", telugu)
        self.assertIn("70-99", telugu)
        self.assertIn("IN RANGE", telugu)
        self.assertIn("సమాచార ప్రయోజనాలకే", telugu)
        self.assertNotIn("diagnosis", telugu.casefold())

    def test_lab_summary_groups_reported_abnormal_normal_and_unclear_results(self) -> None:
        labs = [
            {"name": "Hemoglobin", "value": "10.6", "unit": "g/dL", "reference_range": "12.0-16.0", "status": "LOW"},
            {"name": "Fasting glucose", "value": "112", "unit": "mg/dL", "reference_range": "70-99", "status": "HIGH"},
            {"name": "Total cholesterol", "value": "228", "unit": "mg/dL", "reference_range": "< 200", "status": "HIGH"},
            {"name": "HDL cholesterol", "value": "38", "unit": "mg/dL", "reference_range": ">= 50", "status": "LOW"},
            {"name": "Creatinine", "value": "0.9", "unit": "mg/dL", "reference_range": "0.6-1.1", "status": "IN RANGE"},
            {"name": "White blood cells (WBC)", "value": "7.2", "unit": "10^3/µL", "reference_range": "4.0-11.0", "status": "IN RANGE"},
            {"name": "Platelets", "value": "265", "unit": "10^3/µL", "reference_range": "150-400", "status": "IN RANGE"},
            {"name": "Ferritin", "value": "24", "unit": "ng/mL", "reference_range": "15-150", "status": "IN RANGE"},
        ]
        fields = {"document_date": "2026-10-10", "lab_values": labs}
        english = medivault.build_summary(fields)
        telugu = medivault.build_summary(fields, language="te")

        self.assertLess(english.index("Abnormal results"), english.index("Within reference range"))
        self.assertLess(english.index("Within reference range"), english.index("Status unclear"))
        self.assertLess(telugu.index("అసాధారణ ఫలితాలు"), telugu.index("సూచన పరిధిలో ఉన్న ఫలితాలు"))
        self.assertLess(telugu.index("సూచన పరిధిలో ఉన్న ఫలితాలు"), telugu.index("స్థితి స్పష్టంగా లేని ఫలితాలు"))
        for lab in labs:
            for value in (lab["name"], lab["value"], lab["unit"], lab["reference_range"], lab["status"]):
                self.assertIn(value, english)
                self.assertIn(value, telugu)
        self.assertEqual(english.count("reported status: HIGH"), 2)
        self.assertEqual(english.count("reported status: LOW"), 2)
        self.assertEqual(english.count("reported status: IN RANGE"), 4)

    def test_lab_summary_does_not_infer_missing_or_unknown_status(self) -> None:
        labs = [
            {"name": "Creatinine", "value": "0.9", "unit": "mg/dL", "reference_range": "0.6-1.1", "status": "IN RANGE"},
            {"name": "Ferritin", "value": "24", "unit": "ng/mL", "reference_range": "15-150"},
            {"name": "Glucose", "value": "105", "unit": "mg/dL", "reference_range": "70-99", "status": "BORDERLINE"},
        ]
        english = medivault.build_summary({"lab_values": labs})
        telugu = medivault.build_summary({"lab_values": labs}, language="te")
        for summary in (english, telugu):
            self.assertIn("0.9 mg/dL", summary)
            self.assertIn("15-150", summary)
            self.assertIn("24 ng/mL", summary)
            self.assertIn("105 mg/dL", summary)
            self.assertIn("BORDERLINE", summary)
        self.assertIn("No results were explicitly marked HIGH or LOW", english)
        self.assertIn("this does not establish that all results are within range", english)
        self.assertIn("status unclear: not reported", english)
        self.assertIn("status unclear", english)
        self.assertNotIn("reported status: HIGH", english)
        self.assertNotIn("reported status: LOW", english)

    def test_saved_record_page_displays_grouped_lab_summary_and_values(self) -> None:
        self.signup(self.client, "grouped-record@example.test")
        user = self.client.get("/api/auth/me").json()
        labs = [
            {"name": "Hemoglobin", "value": "10.6", "unit": "g/dL", "reference_range": "12.0-16.0", "status": "LOW"},
            {"name": "Fasting glucose", "value": "112", "unit": "mg/dL", "reference_range": "70-99", "status": "HIGH"},
            {"name": "Total cholesterol", "value": "228", "unit": "mg/dL", "reference_range": "< 200", "status": "HIGH"},
            {"name": "HDL cholesterol", "value": "38", "unit": "mg/dL", "reference_range": ">= 50", "status": "LOW"},
            {"name": "Creatinine", "value": "0.9", "unit": "mg/dL", "reference_range": "0.6-1.1", "status": "IN RANGE"},
            {"name": "White blood cells (WBC)", "value": "7.2", "unit": "10^3/µL", "reference_range": "4.0-11.0", "status": "IN RANGE"},
            {"name": "Platelets", "value": "265", "unit": "10^3/µL", "reference_range": "150-400", "status": "IN RANGE"},
            {"name": "Ferritin", "value": "24", "unit": "ng/mL", "reference_range": "15-150", "status": "IN RANGE"},
        ]
        fields = {"document_type": "Lab report", "document_date": "2026-10-10", "lab_values": labs}
        summary = medivault.build_summary(fields)
        conn = medivault.get_db_connection()
        cursor = conn.execute(
            """INSERT INTO documents (filename, stored_path, document_date, document_type,
               medicines, dosages, diagnoses, lab_values, summary, raw_text, source_type,
               is_synthetic, created_at, status, owner_id)
               VALUES (?, ?, ?, ?, '[]', '[]', '[]', ?, ?, ?, 'pdf', 1, ?, 'verified', ?)""",
            (
                "grouped_labs.pdf", "storage/sample_docs/grouped_labs.pdf", "2026-10-10", "Lab report",
                json.dumps(labs), summary, "original extracted text", medivault.datetime.utcnow().isoformat(timespec="seconds"), user["id"],
            ),
        )
        conn.commit()
        record_id = cursor.lastrowid
        conn.close()

        page = self.client.get(f"/records/{record_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="recordSummary"', page.text)
        displayed_record = self.client.get(f"/api/records/{record_id}").json()
        displayed_summary = displayed_record["summary"]
        self.assertLess(displayed_summary.index("Abnormal results"), displayed_summary.index("Within reference range"))
        self.assertLess(displayed_summary.index("Within reference range"), displayed_summary.index("Status unclear"))
        self.assertEqual(len(displayed_record["lab_groups"]["abnormal"]), 4)
        self.assertEqual(len(displayed_record["lab_groups"]["normal"]), 4)
        for lab in labs:
            self.assertIn(lab["name"], displayed_summary)
            self.assertIn(lab["value"], displayed_summary)
            self.assertIn(lab["reference_range"], displayed_summary)
            self.assertIn(lab["status"], displayed_summary)

        telugu_update = self.client.put(f"/api/records/{record_id}/summary", json={"language": "te"}).json()
        self.assertIn("అసాధారణ ఫలితాలు", telugu_update["summary"])
        self.assertIn("సూచన పరిధిలో ఉన్న ఫలితాలు", telugu_update["summary"])
        self.assertIn("స్థితి స్పష్టంగా లేని ఫలితాలు", telugu_update["summary"])
        for lab in labs:
            self.assertIn(lab["value"], telugu_update["summary"])
            self.assertIn(lab["reference_range"], telugu_update["summary"])
            self.assertIn(lab["status"], telugu_update["summary"])

        site_script = (medivault.BASE_DIR / "static" / "site.js").read_text(encoding="utf-8")
        self.assertIn("recordSummary').textContent = record.summary", site_script)
        for heading in ("Abnormal results (source-reported HIGH/LOW)", "Within reference range (source-reported IN RANGE)", "Status unclear"):
            self.assertIn(heading, site_script)

    def test_summary_language_api_and_upload_selector(self) -> None:
        self.signup(self.client, "summary-language@example.test")
        page = self.client.get("/documents")
        self.assertIn('id="summaryLanguage"', page.text)
        self.assertIn('<option value="en" selected>English</option>', page.text)
        payload = {
            "language": "te",
            "fields": {
                "document_date": "2025-02-14",
                "document_type": "Lab report",
                "medicines": ["Paracetamol"],
                "dosages": ["Paracetamol 500 mg"],
                "lab_values": [{"name": "Hemoglobin", "value": "12.4", "unit": "g/dL", "reference_range": "12-16"}],
            },
        }
        response = self.client.post("/api/summary", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Paracetamol", response.json()["summary"])
        self.assertIn("12.4 g/dL", response.json()["summary"])
        self.assertIn("12-16", response.json()["summary"])
        self.assertEqual(self.client.post("/api/summary", json={"language": "xx", "fields": {}}).status_code, 400)

    def test_existing_abnormal_lab_report_extracts_all_eight_flagged_results(self) -> None:
        originals = medivault.BASE_DIR / "storage" / "originals" / "user_2"
        report_path = next(originals.glob("*_medivault_sample_lab_report_abnormal_demo.pdf"))
        source_text = medivault.read_text_file(report_path)
        self.assertIn("Laboratory Results", source_text)

        fields = medivault.parse_document(source_text, report_path.name)
        expected = [
            {"name": "Hemoglobin", "value": "10.6", "unit": "g/dL", "reference_range": "12.0-16.0", "status": "LOW"},
            {"name": "Fasting glucose", "value": "112", "unit": "mg/dL", "reference_range": "70-99", "status": "HIGH"},
            {"name": "Total cholesterol", "value": "228", "unit": "mg/dL", "reference_range": "< 200", "status": "HIGH"},
            {"name": "HDL cholesterol", "value": "38", "unit": "mg/dL", "reference_range": ">= 50", "status": "LOW"},
            {"name": "Creatinine", "value": "0.9", "unit": "mg/dL", "reference_range": "0.6-1.1", "status": "IN RANGE"},
            {"name": "White blood cells (WBC)", "value": "7.2", "unit": "10^3/µL", "reference_range": "4.0-11.0", "status": "IN RANGE"},
            {"name": "Platelets", "value": "265", "unit": "10^3/µL", "reference_range": "150-400", "status": "IN RANGE"},
            {"name": "Ferritin", "value": "24", "unit": "ng/mL", "reference_range": "15-150", "status": "IN RANGE"},
        ]
        self.assertEqual(fields["lab_values"], expected)
        summary = fields["summary"]
        self.assertLess(summary.index("Abnormal results"), summary.index("Within reference range"))
        for lab in expected:
            self.assertIn(lab["name"], summary)
            self.assertIn(lab["value"], summary)
            self.assertIn(lab["reference_range"], summary)
            self.assertIn(lab["status"], summary)
        self.assertEqual(fields["medicines"], [])
        self.assertEqual(fields["dosages"], [])
        for status in ("LOW", "HIGH", "IN RANGE"):
            self.assertIn(f"reported status: {status}", fields["summary"])

        self.signup(self.client, "abnormal-lab-report@example.test")
        response = self.client.post(
            "/api/upload",
            files={"file": (report_path.name, report_path.read_bytes(), "application/pdf")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["fields"]["lab_values"], expected)
        self.assertEqual(response.json()["fields"]["summary"], fields["summary"])

    def test_existing_synthetic_prescription_and_lab_pdf_fields(self) -> None:
        originals = medivault.BASE_DIR / "storage" / "originals" / "user_2"
        prescription_path = next(originals.glob("*_02_synthetic_prescription_digital.pdf"))
        prescription_text = medivault.read_text_file(prescription_path)
        prescription = medivault.parse_document(prescription_text, prescription_path.name)

        self.assertEqual(prescription["document_date"], "2026-09-18")
        self.assertEqual(prescription["medicines"], ["Cetirizine", "Oral Rehydration Salts", "Paracetamol"])
        self.assertEqual(
            prescription["dosages"],
            [
                "Paracetamol: 500 mg; instruction: 1 tablet as needed; no maximum-use instructions supplied",
                "Cetirizine: 10 mg; instruction: 1 tablet at night",
                "Oral Rehydration Salts: As packaged; instruction: Use according to packet directions",
            ],
        )
        self.signup(self.client, "prescription-sample@example.test")
        upload = self.client.post(
            "/api/upload",
            files={"file": (prescription_path.name, prescription_path.read_bytes(), "application/pdf")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        self.assertEqual(upload.json()["fields"]["medicines"], prescription["medicines"])
        self.assertEqual(upload.json()["fields"]["dosages"], prescription["dosages"])
        self.assertEqual(upload.json()["extracted_text"], prescription_text)

        lab_path = next(originals.glob("*_01_synthetic_lab_report_digital.pdf"))
        lab_text = medivault.read_text_file(lab_path)
        lab_report = medivault.parse_document(lab_text, lab_path.name)
        self.assertEqual(lab_report["document_date"], "2026-10-10")
        self.assertEqual(lab_report["document_type"], "Lab report")
        self.assertEqual(lab_report["medicines"], [])
        self.assertEqual(lab_report["dosages"], [])
        lab_upload = self.client.post(
            "/api/upload",
            files={"file": (lab_path.name, lab_path.read_bytes(), "application/pdf")},
        )
        self.assertEqual(lab_upload.status_code, 200, lab_upload.text)
        self.assertEqual(lab_upload.json()["fields"]["medicines"], [])
        self.assertEqual(lab_upload.json()["fields"]["dosages"], [])
        self.assertEqual(lab_upload.json()["extracted_text"], lab_text)

    def test_legacy_corrupt_dosages_are_cleaned_without_mutating_stored_source(self) -> None:
        self.signup(self.client, "legacy-record@example.test")
        user = self.client.get("/api/auth/me").json()
        raw_text = "Hemoglobin 11.7 g/dL (12-16)\nFasting glucose 94 mg/dL (70-99)"
        bad_dosages = ["range Hemoglobin 11.7 g", "Fasting glucose 94 mg", "11.7 g", "94 mg"]
        bad_summary = "Hemoglobin 11.7g/dL; Hemoglobin 11.7g/dL; Fasting glucose 94mg/dL; Fasting glucose 94mg/dL"
        labs = [
            {"name": "Hemoglobin", "value": "11.7", "unit": "g/dL", "reference_range": "12-16"},
            {"name": "Hemoglobin", "value": "11.7", "unit": "g/dL", "reference_range": "12-16"},
            {"name": "Fasting glucose", "value": "94", "unit": "mg/dL", "reference_range": "70-99"},
        ]
        conn = medivault.get_db_connection()
        cursor = conn.execute(
            """INSERT INTO documents (filename, stored_path, document_date, document_type,
               medicines, dosages, diagnoses, lab_values, summary, raw_text, source_type,
               is_synthetic, created_at, status, owner_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'verified', ?)""",
            (
                "legacy_lab.pdf", "storage/sample_docs/synthetic_01_CBC_Lab_Report.pdf",
                "2025-02-14", "Lab report", "[]", json.dumps(bad_dosages), "[]",
                json.dumps(labs), bad_summary, raw_text, "pdf",
                medivault.datetime.utcnow().isoformat(timespec="seconds"), user["id"],
            ),
        )
        conn.commit()
        record_id = cursor.lastrowid
        conn.close()

        detail = self.client.get(f"/api/records/{record_id}").json()
        self.assertEqual(detail["dosages"], [])
        self.assertEqual(detail["lab_values"], labs)
        self.assertEqual(detail["summary"].count("Hemoglobin 11.7 g/dL"), 1)
        self.assertEqual(detail["summary"].count("Fasting glucose 94 mg/dL"), 1)
        self.assertEqual(detail["raw_text"], raw_text)

        conn = medivault.get_db_connection()
        stored = conn.execute("SELECT dosages, summary, raw_text FROM documents WHERE id = ?", (record_id,)).fetchone()
        conn.close()
        self.assertEqual(json.loads(stored["dosages"]), bad_dosages)
        self.assertEqual(stored["summary"], bad_summary)
        self.assertEqual(stored["raw_text"], raw_text)

    def test_existing_scanned_png_and_jpg_uploads_report_ocr_result_truthfully(self) -> None:
        self.signup(self.client, "image-ocr@example.test")
        sample_candidates = sorted((medivault.BASE_DIR / "storage" / "originals").glob(
            "user_*/*_05_synthetic_prescription_scanned.png"
        ))
        self.assertTrue(sample_candidates, "Expected the existing synthetic scanned prescription PNG sample.")
        image_files = []
        for index, sample_path in enumerate(sample_candidates, start=1):
            image_files.append((f"synthetic_prescription_{index}.png", sample_path.read_bytes(), "image/png"))
        png_bytes = sample_candidates[0].read_bytes()
        with Image.open(BytesIO(png_bytes)) as source_image:
            jpg_output = BytesIO()
            source_image.convert("RGB").save(jpg_output, format="JPEG", quality=95)
        image_files.append(("synthetic_prescription.jpg", jpg_output.getvalue(), "image/jpeg"))

        ocr_available = medivault.tesseract_is_available()
        if ocr_available:
            self.assertEqual(
                Path(medivault.configure_tesseract()),
                Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
            )
        upload_folder = medivault.UPLOAD_DIR / f"user_{self.client.get('/api/auth/me').json()['id']}"
        for filename, image_bytes, media_type in image_files:
            before = len(list(upload_folder.glob("*"))) if upload_folder.exists() else 0
            response = self.client.post("/api/upload", files={"file": (filename, image_bytes, media_type)})
            if ocr_available:
                self.assertEqual(response.status_code, 200, response.text)
                fields = response.json()["fields"]
                self.assertEqual(fields["document_date"], "2026-09-22")
                self.assertIn("Amoxicillin", fields["medicines"])
                self.assertIn("Paracetamol", fields["medicines"])
                self.assertEqual(fields["lab_values"], [], "The source prescription contains no lab results.")
                self.assertTrue(response.json()["extracted_text"].strip())
            else:
                self.assertEqual(response.status_code, 503, response.text)
                self.assertIn("Tesseract OCR", response.json()["detail"])
                self.assertIn("TESSERACT_CMD", response.json()["detail"])
                self.assertIn("Original retained at", response.json()["detail"])
                self.assertGreater(len(list(upload_folder.glob("*"))), before)

    def test_profile_update_validation_and_email_uniqueness(self) -> None:
        self.signup(self.client, "profile-one@example.test")
        second_client = TestClient(medivault.app)
        self.addCleanup(second_client.close)
        self.signup(second_client, "profile-two@example.test")
        invalid = self.client.put("/api/profile", json={"display_name": "New Name", "email": "not-an-email"})
        self.assertEqual(invalid.status_code, 400)
        duplicate = self.client.put(
            "/api/profile",
            json={"display_name": "New Name", "email": "profile-two@example.test"},
        )
        self.assertEqual(duplicate.status_code, 409)
        updated = self.client.put(
            "/api/profile",
            json={"display_name": "New Name", "email": "new-profile@example.test"},
        )
        self.assertEqual(updated.status_code, 200)
        current = self.client.get("/api/auth/me").json()
        self.assertEqual(current["display_name"], "New Name")
        self.assertEqual(current["email"], "new-profile@example.test")

    def test_dashboard_recent_count_category_filter_and_security_sessions(self) -> None:
        self.signup(self.client, "dashboard@example.test")
        user = self.client.get("/api/auth/me").json()
        conn = medivault.get_db_connection()
        now = medivault.datetime.utcnow().isoformat(timespec="seconds")
        for index in range(7):
            conn.execute(
                """INSERT INTO documents (filename, stored_path, document_date, document_type,
                   medicines, dosages, diagnoses, lab_values, summary, raw_text, source_type,
                   is_synthetic, created_at, status, owner_id)
                   VALUES (?, ?, ?, ?, '[]', '[]', '[]', '[]', '', '', 'pdf', 1, ?, 'verified', ?)""",
                (f"record-{index}.pdf", f"storage/sample_docs/synthetic_{index}.pdf", "2025-01-01", "Lab report" if index < 3 else "Progress note", now, user["id"]),
            )
        conn.commit()
        conn.close()

        dashboard = self.client.get("/api/dashboard").json()
        self.assertEqual(dashboard["document_count"], 7)
        self.assertEqual(dashboard["recent_count"], 5)
        self.assertEqual(len(dashboard["recent"]), 5)
        self.assertEqual(sum(category["count"] for category in dashboard["categories"]), 7)
        filtered = self.client.get("/api/timeline", params={"category": "Lab report"}).json()["records"]
        self.assertEqual(len(filtered), 3)
        self.assertTrue(all(record["document_type"] == "Lab report" for record in filtered))
        self.assertEqual(len(self.client.get("/api/timeline").json()["records"]), 7)

        self.client.post("/api/auth/login", json={"email": "dashboard@example.test", "password": "very-secure-test-password"})
        security = self.client.get("/api/security/info").json()
        self.assertEqual(security["active_sessions"], 2)
        revoked = self.client.post("/api/security/revoke-other-sessions").json()
        self.assertEqual(revoked["revoked_sessions"], 1)
        self.assertEqual(self.client.get("/api/security/info").json()["active_sessions"], 1)


if __name__ == "__main__":
    unittest.main()
