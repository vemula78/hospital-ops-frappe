from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from hospital_ops.hospital_ops.doctype.quick_capture.quick_capture import discard, process_into_todo


class TestQuickCapture(IntegrationTestCase):
    def _new_capture(self, text: str = "Call the pharmacy about stock"):
        return frappe.get_doc(
            {"doctype": "Quick Capture", "capture_text": text}
        ).insert()

    def test_default_status_is_open(self):
        # Positive control: a freshly captured item is Open and unlinked.
        doc = self._new_capture()
        self.assertEqual(doc.status, "Open")
        self.assertFalse(doc.processed_into)

    def test_process_into_todo_creates_and_links(self):
        doc = self._new_capture("Chase the CSR quotation")
        result = process_into_todo(doc.name)

        todo = frappe.get_doc("ToDo", result["todo"])
        self.assertEqual(todo.description, "Chase the CSR quotation")
        self.assertEqual(todo.reference_type, "Quick Capture")
        self.assertEqual(todo.reference_name, doc.name)

        doc.reload()
        self.assertEqual(doc.status, "Processed")
        self.assertEqual(doc.processed_into_doctype, "ToDo")
        self.assertEqual(doc.processed_into, todo.name)

    def test_process_already_processed_capture_is_refused(self):
        doc = self._new_capture()
        process_into_todo(doc.name)
        doc.reload()
        self.assertEqual(doc.status, "Processed")

        # Negative case: processing it a second time must not silently create
        # a second ToDo or leave the pair half-done.
        with self.assertRaises(frappe.ValidationError):
            process_into_todo(doc.name)

    def test_discarded_capture_cannot_be_processed(self):
        doc = self._new_capture()
        discard(doc.name)
        doc.reload()
        self.assertEqual(doc.status, "Discarded")

        with self.assertRaises(frappe.ValidationError):
            process_into_todo(doc.name)

    def test_direct_save_cannot_change_status_or_processed_into(self):
        # Codex re-audit, P3-2: a plain save can no longer move status or
        # forge a processed_into pointer at all, on either a fresh insert or
        # an existing row — only process_into_todo/discard may.
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc(
                {
                    "doctype": "Quick Capture",
                    "capture_text": "Forged at birth",
                    "status": "Processed",
                    "processed_into_doctype": "ToDo",
                    "processed_into": "SOME-FAKE-TODO",
                }
            ).insert()

        doc = self._new_capture()
        doc.status = "Processed"
        doc.processed_into_doctype = "ToDo"
        doc.processed_into = "SOME-FAKE-TODO"
        with self.assertRaises(frappe.ValidationError):
            doc.save()
