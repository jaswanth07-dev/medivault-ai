import json
import unittest

from fhir_export import MOCK_ABHA_LABEL, MOCK_ABHA_SYSTEM, PROTOTYPE_LABEL, build_fhir_bundle


class FhirExportMappingTests(unittest.TestCase):
    def make_record(self):
        return {
            "id": 27,
            "filename": "synthetic_lab_report.pdf",
            "document_type": "Lab report",
            "document_date": "2026-10-10",
            "medicines": ["Paracetamol", "Unmatched medicine"],
            "dosages": ["Paracetamol: 500 mg; instruction: 1 tablet as needed"],
            "diagnoses": ["Source diagnosis text"],
            "lab_values": [
                {"name": "Hemoglobin", "value": "10.60", "unit": "g/dL", "reference_range": "12.0-16.0", "status": "LOW"},
                {"name": "Glucose", "value": "112", "unit": "mg/dL", "reference_range": "70-99", "status": "HIGH"},
                {"name": "Creatinine", "value": "0.9", "unit": "mg/dL", "reference_range": "0.6-1.1", "status": "IN RANGE"},
                {"name": "Unclassified test", "value": "3.25", "unit": "mmol/L", "reference_range": "2-4"},
            ],
            "source_type": "pdf",
            "is_synthetic": True,
            "status": "verified",
            "created_at": "2026-10-13T12:00:00",
        }

    def test_bundle_resources_and_all_references_are_consistent(self):
        bundle = build_fhir_bundle(self.make_record(), file_url="/api/records/27/file", export_id="bundle-test")
        encoded = json.dumps(bundle, ensure_ascii=False)
        decoded = json.loads(encoded)
        entries = decoded["entry"]
        full_urls = {entry["fullUrl"] for entry in entries}
        self.assertEqual(len(full_urls), len(entries))
        self.assertEqual(decoded["resourceType"], "Bundle")
        self.assertEqual(decoded["type"], "collection")
        self.assertEqual(decoded["id"], "bundle-test")
        self.assertIn(PROTOTYPE_LABEL, str(decoded["meta"]["tag"]))

        resource_types = [entry["resource"]["resourceType"] for entry in entries]
        self.assertIn("Patient", resource_types)
        self.assertIn("DiagnosticReport", resource_types)
        self.assertIn("Observation", resource_types)
        self.assertIn("DocumentReference", resource_types)
        self.assertIn("Medication", resource_types)
        self.assertIn("MedicationStatement", resource_types)
        for entry in entries:
            resource = entry["resource"]
            self.assertEqual(entry["fullUrl"], f"urn:uuid:{resource['id']}")
            references = []

            def collect(value):
                if isinstance(value, dict):
                    if isinstance(value.get("reference"), str):
                        references.append(value["reference"])
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            collect(resource)
            for reference in references:
                self.assertIn(reference, full_urls, reference)

    def test_export_preserves_clinical_strings_and_labels_mock_patient(self):
        record = self.make_record()
        bundle = build_fhir_bundle(record, file_url="/api/records/27/file")
        resources = [entry["resource"] for entry in bundle["entry"]]
        patient = next(item for item in resources if item["resourceType"] == "Patient")
        self.assertEqual(patient["name"][0]["text"], "Synthetic / unknown demo subject")
        identifier = patient["identifier"][0]
        self.assertEqual(identifier["system"], MOCK_ABHA_SYSTEM)
        self.assertTrue(identifier["value"].startswith("MOCK-ABHA-NOT-REAL-"))
        self.assertEqual(identifier["type"]["text"], MOCK_ABHA_LABEL)
        self.assertNotRegex(identifier["value"], r"^\d+$")
        self.assertNotIn("email", patient)
        self.assertNotIn("birthDate", patient)
        self.assertNotIn("gender", patient)

        observations = [item for item in resources if item["resourceType"] == "Observation"]
        self.assertEqual(len(observations), 4)
        by_name = {item["code"]["text"]: item for item in observations}
        hemoglobin = by_name["Hemoglobin"]
        self.assertEqual(hemoglobin["valueQuantity"]["value"], 10.6)
        self.assertEqual(hemoglobin["valueQuantity"]["unit"], "g/dL")
        self.assertEqual(hemoglobin["extension"][0]["valueString"], "10.60")
        self.assertEqual(hemoglobin["referenceRange"][0]["text"], "12.0-16.0")
        self.assertEqual(hemoglobin["interpretation"][0]["text"], "LOW")
        self.assertEqual(by_name["Glucose"]["interpretation"][0]["text"], "HIGH")
        self.assertEqual(by_name["Creatinine"]["interpretation"][0]["text"], "IN RANGE")
        self.assertNotIn("interpretation", by_name["Unclassified test"])

        report = next(item for item in resources if item["resourceType"] == "DiagnosticReport")
        self.assertEqual(report["effectiveDateTime"], "2026-10-10")
        self.assertIn("Source document date (preserved): 2026-10-10", report["note"][0]["text"])
        self.assertEqual(report["conclusion"], ["Source diagnosis text"])
        doc_ref = next(item for item in resources if item["resourceType"] == "DocumentReference")
        attachment = doc_ref["content"][0]["attachment"]
        self.assertEqual(attachment["url"], "/api/records/27/file")
        self.assertEqual(attachment["title"], record["filename"])
        self.assertIn("source_type=pdf", doc_ref["description"])
        self.assertIn("synthetic_sample=True", doc_ref["description"])

    def test_medication_resources_require_explicit_dosage_association(self):
        bundle = build_fhir_bundle(self.make_record(), file_url="/api/records/27/file")
        resources = [entry["resource"] for entry in bundle["entry"]]
        medications = [item for item in resources if item["resourceType"] == "Medication"]
        statements = [item for item in resources if item["resourceType"] == "MedicationStatement"]
        self.assertEqual(len(medications), 1)
        self.assertEqual(medications[0]["code"]["text"], "Paracetamol")
        self.assertEqual(len(statements), 1)
        self.assertEqual(statements[0]["status"], "unknown")
        self.assertEqual(statements[0]["dosage"][0]["text"], "Paracetamol: 500 mg; instruction: 1 tablet as needed")

    def test_non_numeric_lab_result_is_preserved_without_numeric_guess(self):
        record = self.make_record()
        record["lab_values"] = [{"name": "Qualitative test", "value": "<5", "unit": "mg/L", "reference_range": "<10", "status": "BORDERLINE"}]
        bundle = build_fhir_bundle(record, file_url="/api/records/27/file")
        observation = next(entry["resource"] for entry in bundle["entry"] if entry["resource"]["resourceType"] == "Observation")
        self.assertEqual(observation["valueString"], "<5")
        self.assertEqual(observation["note"][0]["text"], "Source unit: mg/L")
        self.assertEqual(observation["referenceRange"][0]["text"], "<10")
        self.assertEqual(observation["interpretation"][0]["text"], "BORDERLINE")
        self.assertNotIn("valueQuantity", observation)


if __name__ == "__main__":
    unittest.main()
