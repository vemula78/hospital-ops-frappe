from __future__ import annotations

"""The §4.3 data-boundary scan, as a function rather than a hope.

Ported from the reference Next.js application's schema-wide scan (plan
§4.3): no research-participant identifier, patient field, diagnosis, consent
flag or enrolment marker may exist on any doctype in the ``Hospital Ops``
module. This walks both standard fields (``tabDocField``) and Custom Fields
(``tabCustom Field``) — a custom field bolted onto a Hospital Ops doctype
later would otherwise slip the boundary silently.
"""

import frappe

#: Case-insensitive; MariaDB's default collation makes REGEXP case-insensitive
#: already, so this is not wrapped in LOWER() on either side.
PARTICIPANT_IDENTIFIER_PATTERN = r"(participant|patient|mrn|diagnos|consent|enrol|subject_id)"


def find_participant_identifier_fields(module: str | None = None) -> list[dict]:
    """Every field (standard or custom) whose fieldname or label matches the
    participant-identifier pattern, optionally restricted to one module.

    Returns a list of ``{"doctype": ..., "fieldname": ..., "label": ...}``.
    Empty means clean; non-empty names exactly what to fix.
    """
    params: dict = {"pattern": PARTICIPANT_IDENTIFIER_PATTERN}
    module_clause = ""
    if module:
        params["module"] = module
        module_clause = "AND dt.module = %(module)s"

    standard = frappe.db.sql(
        f"""
        SELECT dt.name AS `doctype`, df.fieldname AS fieldname, df.label AS label
        FROM `tabDocField` df
        JOIN `tabDocType` dt ON dt.name = df.parent
        WHERE (df.fieldname REGEXP %(pattern)s OR COALESCE(df.label, '') REGEXP %(pattern)s)
        {module_clause}
        """,
        params,
        as_dict=True,
    )

    custom = frappe.db.sql(
        f"""
        SELECT cf.dt AS `doctype`, cf.fieldname AS fieldname, cf.label AS label
        FROM `tabCustom Field` cf
        JOIN `tabDocType` dt ON dt.name = cf.dt
        WHERE (cf.fieldname REGEXP %(pattern)s OR COALESCE(cf.label, '') REGEXP %(pattern)s)
        {module_clause}
        """,
        params,
        as_dict=True,
    )

    return list(standard) + list(custom)
