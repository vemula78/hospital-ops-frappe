// Copyright (c) 2026, Praveen Vemula and contributors
// For license information, please see license.txt

frappe.query_reports["CSR Project Financials"] = {
	filters: [
		{
			fieldname: "funder",
			label: __("Funder"),
			fieldtype: "Link",
			options: "CSR Funder",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: ["", "Active", "Closed", "Cancelled"],
		},
	],
};
