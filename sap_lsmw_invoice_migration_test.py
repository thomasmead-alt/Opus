#!/usr/bin/env python3
"""
SAP LSMW Invoice Migration Test Script
=======================================
Migrates Accounts Payable (AP) invoice data from a legacy system into
SAP LSMW recording format (transaction FB60 / MIRO).

Features:
  - Validates all master-data dependencies before generating the LSMW file
  - Produces a structured remediation report for every missing or
    mismatched master-data element
  - Outputs a ready-to-import LSMW flat file for validated records

Usage:
    python sap_lsmw_invoice_migration_test.py \
        --ap-data       ap_invoices.csv \
        --customers     customer_master.csv \
        --cost-centres  cost_centre_master.csv \
        --vendors       vendor_master.csv \
        --gl-accounts   gl_account_master.csv \
        --company-codes company_code_master.csv \
        --output        lsmw_invoices.txt \
        --report        migration_report.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 1.  DATA MODELS
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass
class ValidationIssue:
    """Single master-data validation finding."""
    severity: Severity
    invoice_ref: str
    field_name: str
    supplied_value: str
    message: str
    resolution: str
    sap_transaction: str = ""
    responsible_team: str = ""


@dataclass
class InvoiceRecord:
    """Legacy AP invoice row."""
    invoice_ref: str = ""
    vendor_id: str = ""
    customer_id: str = ""
    company_code: str = ""
    invoice_date: str = ""
    posting_date: str = ""
    baseline_date: str = ""
    currency: str = ""
    gross_amount: str = ""
    tax_amount: str = ""
    tax_code: str = ""
    gl_account: str = ""
    cost_centre: str = ""
    profit_centre: str = ""
    payment_terms: str = ""
    reference: str = ""
    header_text: str = ""
    assignment: str = ""
    item_text: str = ""


@dataclass
class MigrationReport:
    """Full remediation report returned after validation."""
    run_timestamp: str = ""
    total_records: int = 0
    passed: int = 0
    failed: int = 0
    warnings: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)
    summary_by_severity: dict[str, int] = field(default_factory=dict)
    summary_by_field: dict[str, int] = field(default_factory=dict)
    summary_by_responsible_team: dict[str, int] = field(default_factory=dict)
    remediation_steps: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 2.  SAMPLE / DEFAULT DATA (used when no CSV files are supplied)
# ---------------------------------------------------------------------------

SAMPLE_AP_DATA: list[dict[str, str]] = [
    {
        "invoice_ref": "INV-2026-0001",
        "vendor_id": "V1001",
        "customer_id": "C2001",
        "company_code": "1000",
        "invoice_date": "2026-03-15",
        "posting_date": "2026-03-15",
        "baseline_date": "2026-03-15",
        "currency": "USD",
        "gross_amount": "15000.00",
        "tax_amount": "1500.00",
        "tax_code": "V1",
        "gl_account": "400100",
        "cost_centre": "CC1010",
        "profit_centre": "PC2000",
        "payment_terms": "ZN30",
        "reference": "PO-88001",
        "header_text": "Consulting services Mar 2026",
        "assignment": "PROJECT-ALPHA",
        "item_text": "Professional services",
    },
    {
        "invoice_ref": "INV-2026-0002",
        "vendor_id": "V1002",
        "customer_id": "C9999",        # deliberately missing customer
        "company_code": "1000",
        "invoice_date": "2026-03-16",
        "posting_date": "2026-03-16",
        "baseline_date": "2026-03-16",
        "currency": "EUR",
        "gross_amount": "8200.50",
        "tax_amount": "820.05",
        "tax_code": "V1",
        "gl_account": "400200",
        "cost_centre": "CC_INVALID",   # deliberately missing cost centre
        "profit_centre": "PC2000",
        "payment_terms": "ZN30",
        "reference": "PO-88002",
        "header_text": "Hardware procurement",
        "assignment": "PROJECT-BETA",
        "item_text": "Server equipment",
    },
    {
        "invoice_ref": "INV-2026-0003",
        "vendor_id": "V_UNKNOWN",       # deliberately missing vendor
        "customer_id": "C2002",
        "company_code": "2000",
        "invoice_date": "2026-03-17",
        "posting_date": "2026-03-17",
        "baseline_date": "2026-03-17",
        "currency": "GBP",
        "gross_amount": "3400.00",
        "tax_amount": "680.00",
        "tax_code": "V2",
        "gl_account": "999999",         # deliberately missing GL account
        "cost_centre": "CC1020",
        "profit_centre": "",            # deliberately blank
        "payment_terms": "ZN60",
        "reference": "PO-88003",
        "header_text": "Facilities maintenance",
        "assignment": "PROJECT-GAMMA",
        "item_text": "Building repairs",
    },
    {
        "invoice_ref": "INV-2026-0004",
        "vendor_id": "V1001",
        "customer_id": "C2001",
        "company_code": "1000",
        "invoice_date": "2026-03-18",
        "posting_date": "2026-03-18",
        "baseline_date": "2026-03-18",
        "currency": "USD",
        "gross_amount": "45000.00",
        "tax_amount": "4500.00",
        "tax_code": "V1",
        "gl_account": "400100",
        "cost_centre": "CC1010",
        "profit_centre": "PC2000",
        "payment_terms": "ZN30",
        "reference": "PO-88004",
        "header_text": "Software licences Q2",
        "assignment": "PROJECT-ALPHA",
        "item_text": "Annual licence renewal",
    },
]

SAMPLE_CUSTOMERS = {"C2001", "C2002", "C2003"}
SAMPLE_VENDORS = {"V1001", "V1002", "V1003"}
SAMPLE_COST_CENTRES = {"CC1010", "CC1020", "CC1030"}
SAMPLE_GL_ACCOUNTS = {"400100", "400200", "400300", "500100"}
SAMPLE_COMPANY_CODES = {"1000", "2000"}


# ---------------------------------------------------------------------------
# 3.  MASTER DATA LOADERS
# ---------------------------------------------------------------------------

def _load_set_from_csv(path: str, key_column: str) -> set[str]:
    """Load a single-column lookup set from a CSV file."""
    result: set[str] = set()
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            val = row.get(key_column, "").strip()
            if val:
                result.add(val)
    return result


def load_master_data(args: argparse.Namespace) -> dict[str, set[str]]:
    """Return lookup sets for every master-data domain."""
    md: dict[str, set[str]] = {}

    if args.customers and os.path.isfile(args.customers):
        md["customers"] = _load_set_from_csv(args.customers, "customer_id")
    else:
        md["customers"] = SAMPLE_CUSTOMERS

    if args.vendors and os.path.isfile(args.vendors):
        md["vendors"] = _load_set_from_csv(args.vendors, "vendor_id")
    else:
        md["vendors"] = SAMPLE_VENDORS

    if args.cost_centres and os.path.isfile(args.cost_centres):
        md["cost_centres"] = _load_set_from_csv(args.cost_centres, "cost_centre_id")
    else:
        md["cost_centres"] = SAMPLE_COST_CENTRES

    if args.gl_accounts and os.path.isfile(args.gl_accounts):
        md["gl_accounts"] = _load_set_from_csv(args.gl_accounts, "gl_account")
    else:
        md["gl_accounts"] = SAMPLE_GL_ACCOUNTS

    if args.company_codes and os.path.isfile(args.company_codes):
        md["company_codes"] = _load_set_from_csv(args.company_codes, "company_code")
    else:
        md["company_codes"] = SAMPLE_COMPANY_CODES

    return md


def load_ap_data(path: str | None) -> list[dict[str, str]]:
    """Load AP invoice data from CSV or fall back to sample data."""
    if path and os.path.isfile(path):
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    return SAMPLE_AP_DATA


# ---------------------------------------------------------------------------
# 4.  VALIDATION ENGINE
# ---------------------------------------------------------------------------

FIELD_RESOLUTION_MAP: dict[str, dict[str, str]] = {
    "vendor_id": {
        "message": "Vendor master record not found in SAP.",
        "resolution": (
            "Create the vendor master via transaction XK01 (centralised) or "
            "MK01 (purchasing). Ensure the vendor is extended to company "
            "code {company_code} with the correct reconciliation account, "
            "payment terms, and withholding-tax settings. After creation, "
            "update the legacy mapping table and re-run this migration."
        ),
        "sap_transaction": "XK01 / MK01",
        "responsible_team": "Master Data – Vendor Management",
    },
    "customer_id": {
        "message": "Customer master record not found in SAP.",
        "resolution": (
            "Create the customer master via transaction XD01 (centralised) or "
            "FD01 (FI only). Verify credit-management data, payment terms, "
            "and dunning configuration. Extend the customer to company code "
            "{company_code} and sales organisation as applicable. Update the "
            "legacy mapping table and re-run this migration."
        ),
        "sap_transaction": "XD01 / FD01",
        "responsible_team": "Master Data – Customer Management",
    },
    "cost_centre": {
        "message": "Cost centre not found in SAP controlling area.",
        "resolution": (
            "Create the cost centre via transaction KS01 or maintain it in "
            "the cost-centre hierarchy (OKEON). Confirm the controlling area, "
            "validity dates (must cover the posting period), manager "
            "assignment, and cost-centre category. Update the legacy mapping "
            "table and re-run this migration."
        ),
        "sap_transaction": "KS01 / OKEON",
        "responsible_team": "Master Data – Controlling / Cost Centre Owners",
    },
    "gl_account": {
        "message": "G/L account does not exist in the target chart of accounts.",
        "resolution": (
            "Create the G/L account via transaction FS00. Define it in the "
            "chart of accounts (account group, P&L / BS indicator, short & "
            "long text). Then extend it to company code {company_code} with "
            "the correct currency, tax category, and field-status group. "
            "Update the legacy mapping table and re-run this migration."
        ),
        "sap_transaction": "FS00",
        "responsible_team": "Master Data – General Ledger / Chart of Accounts",
    },
    "company_code": {
        "message": "Company code is not defined in SAP.",
        "resolution": (
            "Company codes are maintained via transaction OX02 in IMG. This "
            "is typically a one-time configuration activity. Confirm the "
            "company code, country, currency, chart of accounts, fiscal-year "
            "variant, and posting-period variant with the SAP Basis / "
            "configuration team before proceeding."
        ),
        "sap_transaction": "OX02 (IMG)",
        "responsible_team": "SAP Basis / Configuration",
    },
    "profit_centre": {
        "message": "Profit centre is blank or not found in SAP.",
        "resolution": (
            "Create the profit centre via transaction KE51 or derive it from "
            "the cost-centre master (field PRCTR in KS02). If profit-centre "
            "accounting is mandatory in your implementation, every line item "
            "must carry a valid profit centre. Update the legacy mapping "
            "table and re-run this migration."
        ),
        "sap_transaction": "KE51 / KS02",
        "responsible_team": "Master Data – Controlling / Profit Centre Owners",
    },
}


def validate_invoice(
    row: dict[str, str],
    master_data: dict[str, set[str]],
) -> list[ValidationIssue]:
    """Validate a single AP invoice row against all master-data domains."""
    issues: list[ValidationIssue] = []
    inv = row.get("invoice_ref", "UNKNOWN")
    cc = row.get("company_code", "")

    def _check(field: str, lookup_key: str, severity: Severity = Severity.CRITICAL) -> None:
        value = row.get(field, "").strip()
        if not value:
            meta = FIELD_RESOLUTION_MAP.get(field, {})
            issues.append(ValidationIssue(
                severity=severity if severity == Severity.CRITICAL else Severity.WARNING,
                invoice_ref=inv,
                field_name=field,
                supplied_value="(blank)",
                message=f"Required field '{field}' is empty.",
                resolution=meta.get("resolution", "Provide a valid value.").format(company_code=cc),
                sap_transaction=meta.get("sap_transaction", ""),
                responsible_team=meta.get("responsible_team", "Data Migration Team"),
            ))
        elif value not in master_data.get(lookup_key, set()):
            meta = FIELD_RESOLUTION_MAP.get(field, {})
            issues.append(ValidationIssue(
                severity=severity,
                invoice_ref=inv,
                field_name=field,
                supplied_value=value,
                message=meta.get("message", f"Value '{value}' not found in master data."),
                resolution=meta.get("resolution", "Verify and correct the value.").format(company_code=cc),
                sap_transaction=meta.get("sap_transaction", ""),
                responsible_team=meta.get("responsible_team", "Data Migration Team"),
            ))

    _check("vendor_id", "vendors", Severity.CRITICAL)
    _check("customer_id", "customers", Severity.CRITICAL)
    _check("company_code", "company_codes", Severity.CRITICAL)
    _check("gl_account", "gl_accounts", Severity.CRITICAL)
    _check("cost_centre", "cost_centres", Severity.ERROR)
    _check("profit_centre", "cost_centres", Severity.WARNING)  # optional in many implementations

    # Date format checks
    for date_field in ("invoice_date", "posting_date", "baseline_date"):
        raw = row.get(date_field, "").strip()
        if raw:
            try:
                datetime.strptime(raw, "%Y-%m-%d")
            except ValueError:
                issues.append(ValidationIssue(
                    severity=Severity.ERROR,
                    invoice_ref=inv,
                    field_name=date_field,
                    supplied_value=raw,
                    message=f"Date '{raw}' is not in YYYY-MM-DD format.",
                    resolution="Correct the date format to YYYY-MM-DD in the source data.",
                    responsible_team="Data Migration Team",
                ))

    # Amount sanity
    for amt_field in ("gross_amount", "tax_amount"):
        raw = row.get(amt_field, "").strip()
        if raw:
            try:
                val = float(raw)
                if val < 0:
                    issues.append(ValidationIssue(
                        severity=Severity.WARNING,
                        invoice_ref=inv,
                        field_name=amt_field,
                        supplied_value=raw,
                        message=f"Negative amount detected ({raw}). Verify this is a credit memo.",
                        resolution="Confirm whether this should be posted as a credit memo (transaction type).",
                        responsible_team="AP / Finance Team",
                    ))
            except ValueError:
                issues.append(ValidationIssue(
                    severity=Severity.ERROR,
                    invoice_ref=inv,
                    field_name=amt_field,
                    supplied_value=raw,
                    message=f"Amount '{raw}' is not a valid number.",
                    resolution="Correct the amount to a valid decimal number.",
                    responsible_team="Data Migration Team",
                ))

    return issues


# ---------------------------------------------------------------------------
# 5.  LSMW FLAT-FILE GENERATOR  (FB60 recording structure)
# ---------------------------------------------------------------------------

LSMW_HEADER = (
    "INVOICE_REF|BUKRS|LIFNR|KUNNR|BLDAT|BUDAT|ZFBDT|WAERS|WRBTR|WMWST|"
    "MWSKZ|HKONT|KOSTL|PRCTR|ZTERM|XBLNR|BKTXT|ZUONR|SGTXT"
)


def to_sap_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to SAP's DD.MM.YYYY."""
    try:
        dt = datetime.strptime(iso_date.strip(), "%Y-%m-%d")
        return dt.strftime("%d.%m.%Y")
    except ValueError:
        return iso_date


def format_lsmw_line(row: dict[str, str]) -> str:
    """Format one invoice row as a pipe-delimited LSMW record."""
    return "|".join([
        row.get("invoice_ref", ""),
        row.get("company_code", ""),
        row.get("vendor_id", ""),
        row.get("customer_id", ""),
        to_sap_date(row.get("invoice_date", "")),
        to_sap_date(row.get("posting_date", "")),
        to_sap_date(row.get("baseline_date", "")),
        row.get("currency", ""),
        row.get("gross_amount", ""),
        row.get("tax_amount", ""),
        row.get("tax_code", ""),
        row.get("gl_account", ""),
        row.get("cost_centre", ""),
        row.get("profit_centre", ""),
        row.get("payment_terms", ""),
        row.get("reference", ""),
        row.get("header_text", ""),
        row.get("assignment", ""),
        row.get("item_text", ""),
    ])


# ---------------------------------------------------------------------------
# 6.  REPORT BUILDER
# ---------------------------------------------------------------------------

def build_report(
    all_issues: list[ValidationIssue],
    total: int,
    failed_refs: set[str],
) -> MigrationReport:
    """Compile all validation issues into a structured remediation report."""
    report = MigrationReport(
        run_timestamp=datetime.utcnow().isoformat() + "Z",
        total_records=total,
        passed=total - len(failed_refs),
        failed=len(failed_refs),
        warnings=sum(1 for i in all_issues if i.severity == Severity.WARNING),
        issues=[{**asdict(i), "severity": i.severity.value} for i in all_issues],
    )

    # --- Summary by severity ---
    for issue in all_issues:
        sev = issue.severity.value
        report.summary_by_severity[sev] = report.summary_by_severity.get(sev, 0) + 1

    # --- Summary by field ---
    for issue in all_issues:
        report.summary_by_field[issue.field_name] = (
            report.summary_by_field.get(issue.field_name, 0) + 1
        )

    # --- Summary by responsible team ---
    for issue in all_issues:
        team = issue.responsible_team or "Unassigned"
        report.summary_by_responsible_team[team] = (
            report.summary_by_responsible_team.get(team, 0) + 1
        )

    # --- Deduplicated remediation steps ---
    seen: set[str] = set()
    for issue in all_issues:
        if issue.severity in (Severity.CRITICAL, Severity.ERROR):
            key = f"{issue.field_name}|{issue.supplied_value}"
            if key not in seen:
                seen.add(key)
                report.remediation_steps.append({
                    "field": issue.field_name,
                    "value": issue.supplied_value,
                    "action": issue.resolution,
                    "sap_transaction": issue.sap_transaction,
                    "owner": issue.responsible_team,
                    "affected_invoices": ", ".join(
                        i.invoice_ref for i in all_issues
                        if i.field_name == issue.field_name
                        and i.supplied_value == issue.supplied_value
                    ),
                })

    return report


# ---------------------------------------------------------------------------
# 7.  CONSOLE PRINTER
# ---------------------------------------------------------------------------

SEVERITY_COLOURS = {
    "CRITICAL": "\033[91m",  # red
    "ERROR":    "\033[93m",  # yellow
    "WARNING":  "\033[33m",  # dark yellow
    "INFO":     "\033[36m",  # cyan
}
RESET = "\033[0m"


def print_report(report: MigrationReport) -> None:
    """Pretty-print the migration report to stdout."""
    print("\n" + "=" * 80)
    print("  SAP LSMW INVOICE MIGRATION — VALIDATION REPORT")
    print("=" * 80)
    print(f"  Run           : {report.run_timestamp}")
    print(f"  Total records : {report.total_records}")
    print(f"  Passed        : {report.passed}")
    print(f"  Failed        : {report.failed}")
    print(f"  Warnings      : {report.warnings}")
    print("-" * 80)

    # -- Severity summary --
    print("\n  ISSUES BY SEVERITY")
    print("  " + "-" * 40)
    for sev in ("CRITICAL", "ERROR", "WARNING", "INFO"):
        count = report.summary_by_severity.get(sev, 0)
        if count:
            colour = SEVERITY_COLOURS.get(sev, "")
            print(f"  {colour}{sev:<12}{RESET}  {count}")

    # -- Field summary --
    print("\n  ISSUES BY FIELD")
    print("  " + "-" * 40)
    for fld, cnt in sorted(report.summary_by_field.items(), key=lambda x: -x[1]):
        print(f"  {fld:<20}  {cnt}")

    # -- Team summary --
    print("\n  ISSUES BY RESPONSIBLE TEAM")
    print("  " + "-" * 40)
    for team, cnt in sorted(report.summary_by_responsible_team.items(), key=lambda x: -x[1]):
        print(f"  {team:<48}  {cnt}")

    # -- Detailed remediation --
    if report.remediation_steps:
        print("\n" + "=" * 80)
        print("  REMEDIATION STEPS (action required before re-run)")
        print("=" * 80)
        for idx, step in enumerate(report.remediation_steps, 1):
            print(f"\n  [{idx}] Field: {step['field']}  |  Value: {step['value']}")
            print(f"      SAP Tcode : {step['sap_transaction']}")
            print(f"      Owner     : {step['owner']}")
            print(f"      Invoices  : {step['affected_invoices']}")
            print(f"      Action    : {step['action']}")

    # -- Individual issues --
    if report.issues:
        print("\n" + "=" * 80)
        print("  ALL ISSUES (detailed)")
        print("=" * 80)
        for issue in report.issues:
            sev = issue["severity"]
            colour = SEVERITY_COLOURS.get(sev, "")
            print(
                f"\n  {colour}[{sev}]{RESET}  Invoice: {issue['invoice_ref']}"
                f"  |  Field: {issue['field_name']}"
                f"  |  Value: {issue['supplied_value']}"
            )
            print(f"    Message    : {issue['message']}")
            print(f"    Resolution : {issue['resolution']}")
            if issue.get("sap_transaction"):
                print(f"    SAP Tcode  : {issue['sap_transaction']}")
            if issue.get("responsible_team"):
                print(f"    Owner      : {issue['responsible_team']}")

    print("\n" + "=" * 80 + "\n")


# ---------------------------------------------------------------------------
# 8.  MAIN ORCHESTRATION
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SAP LSMW AP Invoice Migration Test Script",
    )
    parser.add_argument("--ap-data", default=None, help="Path to AP invoices CSV")
    parser.add_argument("--customers", default=None, help="Path to customer master CSV")
    parser.add_argument("--cost-centres", default=None, help="Path to cost-centre master CSV")
    parser.add_argument("--vendors", default=None, help="Path to vendor master CSV")
    parser.add_argument("--gl-accounts", default=None, help="Path to GL account master CSV")
    parser.add_argument("--company-codes", default=None, help="Path to company-code master CSV")
    parser.add_argument("--output", default="lsmw_invoices.txt", help="LSMW output file")
    parser.add_argument("--report", default="migration_report.json", help="JSON report output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print("\n[1/5] Loading master data...")
    master_data = load_master_data(args)
    for domain, values in master_data.items():
        print(f"      {domain}: {len(values)} entries loaded")

    print("[2/5] Loading AP invoice data...")
    ap_rows = load_ap_data(args.ap_data)
    print(f"      {len(ap_rows)} invoice(s) loaded")

    print("[3/5] Validating invoices against master data...")
    all_issues: list[ValidationIssue] = []
    failed_refs: set[str] = set()
    passed_rows: list[dict[str, str]] = []

    for row in ap_rows:
        issues = validate_invoice(row, master_data)
        all_issues.extend(issues)
        has_blocking = any(
            i.severity in (Severity.CRITICAL, Severity.ERROR) for i in issues
        )
        if has_blocking:
            failed_refs.add(row.get("invoice_ref", "UNKNOWN"))
        else:
            passed_rows.append(row)

    print(f"      {len(passed_rows)} passed  |  {len(failed_refs)} failed  |  {len(all_issues)} total issues")

    print("[4/5] Generating LSMW flat file for valid records...")
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(LSMW_HEADER + "\n")
        for row in passed_rows:
            fh.write(format_lsmw_line(row) + "\n")
    print(f"      Written {len(passed_rows)} record(s) to {args.output}")

    print("[5/5] Building remediation report...")
    report = build_report(all_issues, len(ap_rows), failed_refs)

    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(asdict(report), fh, indent=2)
    print(f"      Report saved to {args.report}")

    print_report(report)

    # Return non-zero if any records failed
    return 1 if failed_refs else 0


if __name__ == "__main__":
    sys.exit(main())
