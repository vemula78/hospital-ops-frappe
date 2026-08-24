from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, today

from hospital_ops.hospital_ops.doctype.waiting_for.waiting_for import log_follow_up


class TestWaitingFor(IntegrationTestCase):
    def setUp(self) -> None:
        self.contact = frappe.get_doc(
            {"doctype": "Contact", "first_name": "Test Supplier Contact"}
        ).insert(ignore_if_duplicate=True)

    def _new_waiting_item(self, promised_on: str | None = None):
        return frappe.get_doc(
            {
                "doctype": "Waiting For",
                "waiting_on": self.contact.name,
                "subject": "Quotation for the CT scanner AMC",
                "delegated_on": today(),
                "promised_on": promised_on,
            }
        ).insert()

    def test_promised_after_delegated_is_accepted(self):
        # Positive control for the rule below: a promise dated on/after the
        # delegation is perfectly normal and must go through.
        doc = self._new_waiting_item(promised_on=add_days(today(), 7))
        self.assertEqual(doc.status, "Waiting")
        self.assertEqual(str(doc.follow_up_on), str(doc.promised_on))

    def test_promised_before_delegated_is_refused(self):
        # The exact rule that caught a real bug in the source app: a promise
        # dated before the delegation itself is nonsensical.
        with self.assertRaises(frappe.ValidationError):
            self._new_waiting_item(promised_on=add_days(today(), -1))

    def test_follow_up_on_defaults_to_promised_on_when_empty(self):
        doc = frappe.get_doc(
            {
                "doctype": "Waiting For",
                "waiting_on": self.contact.name,
                "subject": "Follow up on the AMC quotation",
                "delegated_on": today(),
                "promised_on": add_days(today(), 5),
                "follow_up_on": None,
            }
        ).insert()
        self.assertEqual(str(doc.follow_up_on), str(doc.promised_on))

    def test_log_follow_up_moves_promised_and_follow_up_on(self):
        doc = self._new_waiting_item(promised_on=add_days(today(), 7))
        new_date = add_days(today(), 14)

        result = log_follow_up(doc.name, note="They asked for another week", new_promised_on=new_date)

        self.assertEqual(str(result["promised_on"]), str(new_date))
        self.assertEqual(str(result["follow_up_on"]), str(new_date))

        doc.reload()
        self.assertEqual(len(doc.follow_ups), 1)
        self.assertEqual(doc.follow_ups[0].note, "They asked for another week")
        self.assertEqual(str(doc.promised_on), str(new_date))

    def test_log_follow_up_on_resolved_item_is_refused(self):
        doc = self._new_waiting_item(promised_on=add_days(today(), 7))
        doc.status = "Resolved"
        doc.save()

        # Negative case: nothing left to chase once resolved.
        with self.assertRaises(frappe.ValidationError):
            log_follow_up(doc.name, note="Too late")

    def test_log_follow_up_on_waiting_item_is_accepted(self):
        # Positive control for the check above: a still-Waiting item accepts
        # a follow-up with no new promise, and just logs history.
        doc = self._new_waiting_item(promised_on=add_days(today(), 7))
        result = log_follow_up(doc.name, note="Still chasing")
        self.assertEqual(result["name"], doc.name)

        doc.reload()
        self.assertEqual(len(doc.follow_ups), 1)
        # No new promise given: the parent's dates are unchanged.
        self.assertEqual(str(doc.promised_on), str(add_days(today(), 7)))
