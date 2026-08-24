from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, today

from hospital_ops.hospital_ops.doctype.meeting_record.meeting_record import (
    create_todo_from_decision,
)


class TestMeetingRecord(IntegrationTestCase):
    def _new_meeting(self):
        return frappe.get_doc(
            {
                "doctype": "Meeting Record",
                "meeting_title": "Cardiology weekly review",
                "held_on": today(),
                "attendees": "Dr. Varyani, Dr. Kini",
                "decisions": [
                    {
                        "decision": "Order two additional Cath Lab guidewires",
                        "owner_name": "Praveen",
                        "due_on": add_days(today(), 3),
                    }
                ],
            }
        ).insert()

    def test_meeting_record_holds_a_decision(self):
        # Positive control for the flow below: a plain decision row, with no
        # ToDo yet, is exactly what a fresh meeting record should have.
        doc = self._new_meeting()
        self.assertEqual(len(doc.decisions), 1)
        self.assertFalse(doc.decisions[0].todo)

    def test_create_todo_from_decision_stamps_the_row(self):
        doc = self._new_meeting()
        row_name = doc.decisions[0].name

        result = create_todo_from_decision(doc.name, row_name)

        todo = frappe.get_doc("ToDo", result["todo"])
        self.assertEqual(todo.description, "Order two additional Cath Lab guidewires")
        self.assertEqual(todo.reference_type, "Meeting Record")
        self.assertEqual(todo.reference_name, doc.name)

        doc.reload()
        self.assertEqual(doc.decisions[0].todo, todo.name)

    def test_create_todo_twice_for_same_decision_is_refused(self):
        doc = self._new_meeting()
        row_name = doc.decisions[0].name
        create_todo_from_decision(doc.name, row_name)

        # Negative case: a decision that already has a ToDo must not get a
        # second, orphaned one.
        with self.assertRaises(frappe.ValidationError):
            create_todo_from_decision(doc.name, row_name)

    def test_create_todo_for_unknown_row_is_refused(self):
        doc = self._new_meeting()
        with self.assertRaises(frappe.ValidationError):
            create_todo_from_decision(doc.name, "not-a-real-row")
