# Data Models & JavaScript Reference

## Overview

Opus is a SAP LSMW (Legacy System Migration Workbench) invoice migration platform for validating and transforming Accounts Payable (AP) invoice data before importing into SAP. It consists of two components that share the same data model:

- **`sap_lsmw_invoice_migration_test.py`** — Python CLI for batch validation and LSMW file generation
- **`index.html`** — Browser-based SPA (single-page application) with the same validation logic plus a full UI for managing master data, hierarchies, rules, and environments

---

## Table of Contents

1. [Python Data Models](#1-python-data-models)
2. [JavaScript State & Constants](#2-javascript-state--constants)
3. [Master Data Models](#3-master-data-models)
4. [Master Data Dependencies](#4-master-data-dependencies)
5. [Hierarchy Models](#5-hierarchy-models)
6. [Validation Models](#6-validation-models)
7. [LSMW Output Format](#7-lsmw-output-format)
8. [Dependencies](#8-dependencies)
9. [Processing Pipeline](#9-processing-pipeline)

---

## 1. Python Data Models

Defined in `sap_lsmw_invoice_migration_test.py`, lines 43–99.

### `Severity` (Enum)

Controls how validation findings are classified and whether they block migration.

```python
class Severity(str, Enum):
    CRITICAL = "CRITICAL"   # Blocks migration — missing required master data
    ERROR    = "ERROR"      # Blocks migration — invalid format or value
    WARNING  = "WARNING"    # Does not block — optional/advisory issue
    INFO     = "INFO"       # Informational only
```

**Blocking rule:** Any issue with severity `CRITICAL` or `ERROR` prevents that invoice from being written to the LSMW output file.

---

### `ValidationIssue` (dataclass)

Represents a single finding from validating one field on one invoice.

```python
@dataclass
class ValidationIssue:
    severity:         Severity  # CRITICAL | ERROR | WARNING | INFO
    invoice_ref:      str       # e.g. "INV-2026-0001"
    field_name:       str       # e.g. "vendor_id"
    supplied_value:   str       # The value that failed, or "(blank)"
    message:          str       # Human-readable description of the problem
    resolution:       str       # Step-by-step fix, may include {company_code}
    sap_transaction:  str = ""  # SAP t-code, e.g. "XK01 / MK01"
    responsible_team: str = ""  # Team who owns the fix
```

**Where it appears:**
- Created by `validate_invoice()` (`sap_lsmw_invoice_migration_test.py:336`)
- Collected into `MigrationReport.issues` via `build_report()` (`line 474`)

---

### `InvoiceRecord` (dataclass)

Represents one AP invoice row from the source CSV. All fields are strings as they come directly from CSV parsing.

```python
@dataclass
class InvoiceRecord:
    invoice_ref:   str  # Unique invoice identifier
    vendor_id:     str  # SAP vendor master key — validated CRITICAL
    customer_id:   str  # SAP customer master key — validated CRITICAL
    company_code:  str  # SAP company code — validated CRITICAL
    invoice_date:  str  # YYYY-MM-DD — format validated
    posting_date:  str  # YYYY-MM-DD — format validated
    baseline_date: str  # YYYY-MM-DD (payment due base) — format validated
    currency:      str  # ISO 4217 code, e.g. "USD"
    gross_amount:  str  # Decimal string, e.g. "15000.00"
    tax_amount:    str  # Decimal string — negative triggers WARNING
    tax_code:      str  # SAP tax code, e.g. "V1"
    gl_account:    str  # G/L account number — validated CRITICAL
    cost_centre:   str  # SAP cost centre — validated ERROR
    profit_centre: str  # SAP profit centre — validated WARNING (optional)
    payment_terms: str  # SAP payment terms key, e.g. "ZN30"
    reference:     str  # Purchase order or external reference
    header_text:   str  # Invoice header description
    assignment:    str  # Assignment field (ZUONR)
    item_text:     str  # Line item description
```

> **Note:** `InvoiceRecord` is used as documentation/type reference. In practice, the script works with raw `dict[str, str]` rows from `csv.DictReader`, which map directly to these field names.

---

### `MigrationReport` (dataclass)

The output of `build_report()`. Captures all findings and is serialised to JSON.

```python
@dataclass
class MigrationReport:
    run_timestamp:                str               # ISO 8601 UTC, e.g. "2026-03-25T10:00:00Z"
    total_records:                int               # Total invoices processed
    passed:                       int               # Records with no blocking issues
    failed:                       int               # Records blocked from LSMW output
    warnings:                     int               # Total WARNING-severity issues
    issues:                       list[dict]        # All ValidationIssue instances as dicts
    summary_by_severity:          dict[str, int]    # { "CRITICAL": 3, "ERROR": 2, ... }
    summary_by_field:             dict[str, int]    # { "vendor_id": 4, "cost_centre": 2, ... }
    summary_by_responsible_team:  dict[str, int]    # { "Master Data – Vendor Management": 4, ... }
    remediation_steps:            list[dict[str, str]]  # Deduplicated action items
```

**`remediation_steps` entry shape:**

| Key | Type | Description |
|-----|------|-------------|
| `field` | str | The field with the problem, e.g. `"vendor_id"` |
| `value` | str | The bad value, e.g. `"V_UNKNOWN"` |
| `action` | str | Full resolution text |
| `sap_transaction` | str | SAP t-code, e.g. `"XK01 / MK01"` |
| `owner` | str | Responsible team |
| `affected_invoices` | str | Comma-separated list of invoice refs |

---

## 2. JavaScript State & Constants

Defined at the top of the `<script>` block in `index.html`, line 3124 onwards.

### Global Constants

```javascript
const ENVS       = ['dev', 'sit', 'uat', 'prod'];
const ENV_LABELS = { dev: 'DEV', sit: 'SIT', uat: 'UAT', prod: 'PROD' };

const FILE_KEYS  = [
  'ap',              // AP invoice data
  'customers',
  'vendors',
  'gl_accounts',
  'profit_centres',
  'cost_centres',
  'company_codes',
  'business_entities',
  'rental_objects',
  'settlement_units'
];
```

### Runtime State Variables

```javascript
let activeEnv = 'dev';      // Currently selected environment
let lastReport = null;      // Most recent MigrationReport object
let lastLSMW   = '';        // Most recent LSMW flat-file string
```

### Per-Environment File Storage

Files are stored per environment in memory. Each uploaded CSV is stored under its `FILE_KEYS` key.

```javascript
// Shape: envFiles[env][fileKey] = FileEntry
const envFiles = {};

// FileEntry shape:
{
  name:     string,   // Original filename
  rows:     object[], // Parsed CSV rows (array of objects)
  version:  number,   // Incremented on each upload
  loadedAt: string    // ISO timestamp
}
```

Access the current environment's files via:

```javascript
function getFiles() { return envFiles[activeEnv]; }
```

### Config Versioning

```javascript
const configVersions = { source: 1, target: 1, mapping: 1 };
let configMeta = {
  name:        string,  // User-defined project name
  description: string,  // Project description
  loadedFrom:  string | null  // Source filename if imported
};
```

---

## 3. Master Data Models

### `SAMPLE_MD` — Lookup Sets

Simple `Set<string>` per master data type. Used for validation (membership check only).

```javascript
const SAMPLE_MD = {
  customers:        Set { 'C2001', 'C2002', 'C2003' },
  vendors:          Set { 'V1001', 'V1002', 'V1003' },
  gl_accounts:      Set { '400100', '400200', '400300', '500100' },
  profit_centres:   Set { 'PC1000', 'PC2000', 'PC3000' },
  cost_centres:     Set { 'CC1010', 'CC1020', 'CC1030' },
  company_codes:    Set { '1000', '2000' },
  business_entities:Set { 'BE001', 'BE002', 'BE003' },
  rental_objects:   Set { 'RO001', 'RO002', 'RO003' },
  settlement_units: Set { 'SU001', 'SU002', 'SU003' },
};
```

### `SAMPLE_MD_ROWS` — Rich Row Data

Full row objects used for display in the Master Data Explorer. Each type has typed columns.

#### `customers`
| Field | Type | Example |
|-------|------|---------|
| `customer_id` | string (key) | `"C2001"` |
| `customer_name` | string | `"Acme Corp"` |
| `country` | string (ISO) | `"US"` |
| `status` | string | `"Active"` |

#### `vendors`
| Field | Type | Example |
|-------|------|---------|
| `vendor_id` | string (key) | `"V1001"` |
| `vendor_name` | string | `"Tech Solutions Inc"` |
| `country` | string (ISO) | `"US"` |
| `status` | string | `"Active"` |

#### `gl_accounts`
| Field | Type | Example |
|-------|------|---------|
| `gl_account` | string (key) | `"400100"` |
| `account_name` | string | `"Consulting Expenses"` |
| `account_type` | string | `"P&L"` |
| `account_group` | string | `"Expenses"` |
| `status` | string | `"Active"` |

#### `profit_centres`
| Field | Type | Example |
|-------|------|---------|
| `profit_centre_id` | string (key) | `"PC1000"` |
| `description` | string | `"Division A - Consulting"` |
| `company_code` | string | `"1000"` |
| `controlling_area` | string | `"CA01"` |
| `status` | string | `"Active"` |

#### `cost_centres`
| Field | Type | Example |
|-------|------|---------|
| `cost_centre_id` | string (key) | `"CC1010"` |
| `description` | string | `"Admin - Head Office"` |
| `company_code` | string | `"1000"` |
| `profit_centre` | string | `"PC1000"` |
| `controlling_area` | string | `"CA01"` |
| `status` | string | `"Active"` |

#### `company_codes`
| Field | Type | Example |
|-------|------|---------|
| `company_code` | string (key) | `"1000"` |
| `company_name` | string | `"Global HQ"` |
| `currency` | string (ISO) | `"USD"` |
| `country` | string (ISO) | `"US"` |

#### `business_entities`
| Field | Type | Example |
|-------|------|---------|
| `business_entity_id` | string (key) | `"BE001"` |
| `description` | string | `"Commercial Property A"` |
| `company_code` | string (FK) | `"1000"` |
| `profit_centre` | string (FK) | `"PC1000"` |
| `cost_centre` | string (FK) | `"CC1010"` |
| `gl_account` | string (FK) | `"400100"` |
| `status` | string | `"Active"` |

#### `rental_objects`
| Field | Type | Example |
|-------|------|---------|
| `rental_object_id` | string (key) | `"RO001"` |
| `description` | string | `"Office Suite 101"` |
| `business_entity` | string (FK) | `"BE001"` |
| `company_code` | string (FK) | `"1000"` |
| `profit_centre` | string (FK) | `"PC1000"` |
| `cost_centre` | string (FK) | `"CC1010"` |
| `gl_account` | string (FK) | `"400100"` |
| `status` | string | `"Active"` |

#### `settlement_units`
| Field | Type | Example |
|-------|------|---------|
| `settlement_unit_id` | string (key) | `"SU001"` |
| `description` | string | `"Service Charge Pool A"` |
| `business_entity` | string (FK) | `"BE001"` |
| `rental_object` | string (FK) | `"RO001"` |
| `company_code` | string (FK) | `"1000"` |
| `profit_centre` | string (FK) | `"PC1000"` |
| `cost_centre` | string (FK) | `"CC1010"` |
| `gl_account` | string (FK) | `"400100"` |
| `status` | string | `"Active"` |

---

## 4. Master Data Dependencies

The `MD_TYPES` constant (`index.html:3539`) defines the schema and dependency graph for each master data type. The `dependencies` object maps a field name in the current type to the master data type it must reference.

```javascript
const MD_TYPES = {
  customers:         { key: 'customer_id',        ... },
  vendors:           { key: 'vendor_id',          ... },
  gl_accounts:       { key: 'gl_account',         ..., hasHierarchy: true },
  profit_centres:    { key: 'profit_centre_id',   ..., hasHierarchy: true },
  cost_centres:      { key: 'cost_centre_id',     ..., hasHierarchy: true,
                       dependencies: {
                         company_code:  'company_codes',
                         profit_centre: 'profit_centres'
                       }},
  company_codes:     { key: 'company_code',       ... },
  business_entities: { key: 'business_entity_id', ...,
                       dependencies: {
                         company_code:  'company_codes',
                         profit_centre: 'profit_centres',
                         cost_centre:   'cost_centres',
                         gl_account:    'gl_accounts'
                       }},
  rental_objects:    { key: 'rental_object_id',   ...,
                       dependencies: {
                         business_entity: 'business_entities',
                         company_code:    'company_codes',
                         profit_centre:   'profit_centres',
                         cost_centre:     'cost_centres',
                         gl_account:      'gl_accounts'
                       }},
  settlement_units:  { key: 'settlement_unit_id', ...,
                       dependencies: {
                         business_entity: 'business_entities',
                         rental_object:   'rental_objects',
                         company_code:    'company_codes',
                         profit_centre:   'profit_centres',
                         cost_centre:     'cost_centres',
                         gl_account:      'gl_accounts'
                       }},
};
```

### Dependency Hierarchy Diagram

```
company_codes
    └── profit_centres
    └── cost_centres ──────────── depends on: company_codes, profit_centres
            └── gl_accounts
                    └── business_entities ── depends on: company_codes, profit_centres,
                            │                              cost_centres, gl_accounts
                            └── rental_objects ── depends on: business_entities + above
                                    └── settlement_units ── depends on: rental_objects + above
```

Types without a `dependencies` key (`customers`, `vendors`, `company_codes`) are root-level — they have no foreign-key relationships within this data model.

---

## 5. Hierarchy Models

Three master data types support a tree hierarchy editor: `gl_accounts`, `profit_centres`, and `cost_centres`. These are flagged with `hasHierarchy: true` in `MD_TYPES`.

Hierarchy data is stored in `SAMPLE_HIERARCHIES` and in per-environment storage alongside the flat row data.

### Hierarchy Object Shape

```javascript
{
  label: string,     // Display name for the root, e.g. "Chart of Accounts"
  nodes: HierarchyNode[]
}
```

### `HierarchyNode`

```javascript
{
  id:     string,         // Unique node ID, e.g. "EXPENSES" or "400100"
  name:   string,         // Display label, e.g. "Operating Expenses"
  parent: string | null   // ID of parent node, or null for root
}
```

**Example — GL Accounts hierarchy:**

```
ROOT (Chart of Accounts)
 ├── ASSETS (Asset Accounts)
 │    └── 500100 (Revenue - Services)
 └── EXPENSES (Operating Expenses)
      ├── 400100 (Consulting Expenses)
      ├── 400200 (Hardware Expenses)
      └── 400300 (Travel Expenses)
```

Hierarchy nodes are stored and rendered as a flat array with parent references. The UI builds the tree display from this flat list at render time.

---

## 6. Validation Models

### Validation Rule (Custom Rules Builder)

User-defined rules extend the built-in validation. Rules are stored in the browser and applied during `runMigration()`.

```javascript
{
  id:        string,    // UUID, auto-generated
  name:      string,    // Human-readable label
  field:     string,    // AP invoice field to check, e.g. "currency"
  condition: string,    // One of: equals, not_equals, contains, not_contains,
                        //   starts_with, ends_with, is_blank, is_not_blank,
                        //   greater_than, less_than, matches_regex
  value:     string,    // Comparison value (not used for is_blank / is_not_blank)
  severity:  string,    // "CRITICAL" | "ERROR" | "WARNING" | "INFO"
  message:   string,    // Message shown when the rule fails
  enabled:   boolean    // Whether the rule is active
}
```

### `RESOLUTIONS` — Field Resolution Map

JavaScript counterpart to Python's `FIELD_RESOLUTION_MAP`. Provides per-field remediation text for validation errors shown in the UI.

```javascript
const RESOLUTIONS = {
  vendor_id:    { message, resolution, sap_transaction, responsible_team },
  customer_id:  { message, resolution, sap_transaction, responsible_team },
  cost_centre:  { message, resolution, sap_transaction, responsible_team },
  gl_account:   { message, resolution, sap_transaction, responsible_team },
  company_code: { message, resolution, sap_transaction, responsible_team },
  profit_centre:{ message, resolution, sap_transaction, responsible_team },
};
```

| Field | SAP Transaction | Responsible Team |
|-------|----------------|-----------------|
| `vendor_id` | XK01 / MK01 | Master Data – Vendor Management |
| `customer_id` | XD01 / FD01 | Master Data – Customer Management |
| `cost_centre` | KS01 / OKEON | Master Data – Controlling / Cost Centre Owners |
| `gl_account` | FS00 | Master Data – General Ledger / Chart of Accounts |
| `company_code` | OX02 (IMG) | SAP Basis / Configuration |
| `profit_centre` | KE51 / KS02 | Master Data – Controlling / Profit Centre Owners |

### Validation Severity and Blocking Behaviour

| Severity | Blocks LSMW output? | Used for |
|----------|--------------------|-|
| CRITICAL | Yes | Missing/invalid required master data |
| ERROR | Yes | Invalid date format, non-numeric amount |
| WARNING | No | Optional fields (profit_centre), negative amounts |
| INFO | No | Informational notes |

---

## 7. LSMW Output Format

Valid invoices are written as a pipe-delimited flat file for import into SAP via the LSMW FB60 recording.

### Header

```
INVOICE_REF|BUKRS|LIFNR|KUNNR|BLDAT|BUDAT|ZFBDT|WAERS|WRBTR|WMWST|MWSKZ|HKONT|KOSTL|PRCTR|ZTERM|XBLNR|BKTXT|ZUONR|SGTXT
```

### Field Mapping

| LSMW Field | SAP Field Name | Source Field | Format |
|------------|---------------|-------------|--------|
| `INVOICE_REF` | Invoice Reference | `invoice_ref` | String |
| `BUKRS` | Company Code | `company_code` | String |
| `LIFNR` | Vendor | `vendor_id` | String |
| `KUNNR` | Customer | `customer_id` | String |
| `BLDAT` | Invoice Date | `invoice_date` | DD.MM.YYYY |
| `BUDAT` | Posting Date | `posting_date` | DD.MM.YYYY |
| `ZFBDT` | Baseline Date | `baseline_date` | DD.MM.YYYY |
| `WAERS` | Currency | `currency` | ISO 4217 |
| `WRBTR` | Gross Amount | `gross_amount` | Decimal |
| `WMWST` | Tax Amount | `tax_amount` | Decimal |
| `MWSKZ` | Tax Code | `tax_code` | String |
| `HKONT` | G/L Account | `gl_account` | String |
| `KOSTL` | Cost Centre | `cost_centre` | String |
| `PRCTR` | Profit Centre | `profit_centre` | String |
| `ZTERM` | Payment Terms | `payment_terms` | String |
| `XBLNR` | Reference | `reference` | String |
| `BKTXT` | Header Text | `header_text` | String |
| `ZUONR` | Assignment | `assignment` | String |
| `SGTXT` | Item Text | `item_text` | String |

> Dates are converted from `YYYY-MM-DD` (source) to `DD.MM.YYYY` (SAP) by `to_sap_date()` (`sap_lsmw_invoice_migration_test.py:436`).

---

## 8. Dependencies

### Python (`sap_lsmw_invoice_migration_test.py`)

Standard library only — no third-party packages required.

| Module | Used for |
|--------|---------|
| `argparse` | CLI argument parsing |
| `csv` | Reading AP and master data CSV files |
| `json` | Serialising `MigrationReport` to JSON |
| `os` | File existence checks |
| `sys` | Exit code, `sys.exit()` |
| `dataclasses` | `@dataclass`, `field()`, `asdict()` |
| `datetime` | Date format validation, report timestamp |
| `enum` | `Severity` enum |
| `typing` | Type hints (`Any`) |

**Minimum Python version:** 3.10 (uses `list[str] | None` union syntax)

### JavaScript (`index.html`)

No external libraries. The application uses only native browser APIs.

| API | Used for |
|-----|---------|
| `FileReader` | Reading uploaded CSV files from disk |
| `localStorage` | Persisting project state across sessions |
| DOM API | All UI rendering and event handling |
| `JSON.stringify / parse` | State serialisation for import/export |
| CSS Grid / Flexbox | Layout |

---

## 9. Processing Pipeline

### Python CLI Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│  main()                                                             │
│                                                                     │
│  1. parse_args()          Parse --ap-data, --vendors, etc.         │
│  2. load_master_data()    Build Set<string> per domain             │
│       └─ _load_set_from_csv()  or fall back to SAMPLE_* constants  │
│  3. load_ap_data()        Read invoices CSV or SAMPLE_AP_DATA       │
│  4. validate_invoice()    Per invoice: check each field             │
│       └─ _check()         Membership test + blank check            │
│       └─ Date format check (strptime)                               │
│       └─ Amount sanity check (float conversion)                    │
│  5. Write LSMW flat file  format_lsmw_line() per passing row        │
│  6. build_report()        Aggregate all ValidationIssues           │
│  7. print_report()        Colour-coded console output              │
│  8. Write JSON report     json.dump(asdict(report))                │
└─────────────────────────────────────────────────────────────────────┘
```

### JavaScript Browser Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│  User Actions → State → Render                                      │
│                                                                     │
│  1. File Upload           handleFile() → parseCSV() → envFiles     │
│  2. Column Reconciliation renderColumnReconciliation()              │
│       Map source columns to schema                                  │
│  3. Data Quality Review   runDataQualityReview()                    │
│       Field completeness, anomaly detection, trend analysis         │
│  4. Validation            validate() / _origValidate()              │
│       RESOLUTIONS lookup, custom rules (applyRules())               │
│  5. Environment Compare   Compare dev / sit / uat / prod data       │
│  6. Hierarchy Editor      renderHierarchy(), hierAddNode(), etc.    │
│  7. Audit Trail           Log all changes with timestamp            │
│  8. Export                exportLSMW() / exportJSON()               │
└─────────────────────────────────────────────────────────────────────┘
```

### Key Function Reference

| Function | File | Line | Purpose |
|----------|------|------|---------|
| `main()` | Python | 630 | Top-level orchestration |
| `validate_invoice()` | Python | 336 | Validate one invoice row |
| `build_report()` | Python | 474 | Aggregate issues into report |
| `format_lsmw_line()` | Python | 445 | Format one row for LSMW output |
| `to_sap_date()` | Python | 436 | Convert date to SAP format |
| `print_report()` | Python | 543 | Console output |
| `getFiles()` | JS | 3135 | Get current env file store |
| `getMdRows()` | JS | 3562 | Get rows for active MD type |
| `switchEnv()` | JS | — | Switch active environment |
| `validate()` | JS | — | Run full validation in browser |
| `runMigration()` | JS | — | Execute migration and report |
| `renderMdTable()` | JS | 3617 | Render master data grid |
| `renderHierarchy()` | JS | — | Render hierarchy tree |
| `exportLSMW()` | JS | — | Download LSMW flat file |
| `exportJSON()` | JS | — | Download JSON report |
