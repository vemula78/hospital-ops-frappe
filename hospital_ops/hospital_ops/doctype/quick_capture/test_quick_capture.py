from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from hospital_ops.hospital_ops.doctype.quick_capture.quick_capture import process_into_todo


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
        doc.status = "Discarded"
        doc.save()

        with self.assertRaises(frappe.ValidationError):
            process_into_todo(doc.name)
