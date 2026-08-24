// Copyright (c) 2026, Praveen Vemula and contributors
// For license information, please see license.txt

frappe.query_reports["Weekly Review"] = {
	filters: [
		{
			fieldname: "section",
			label: __("Section"),
			fieldtype: "Select",
			options: [
				"",
				"Quick Captures",
				"Waiting For",
				"CSR Reporting Obligations",
				"CSR Tranches",
				"Research Ethics",
				"Hospital Signs",
				"Hospital Web Pages",
				"Software Project Records",
			],
		},
	],
};
