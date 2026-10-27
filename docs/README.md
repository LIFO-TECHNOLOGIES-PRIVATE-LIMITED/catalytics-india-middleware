# CATalytics India Middleware - Documentation

Architecture documentation for `catalytics-india-middleware`, the on-site Tally-to-CATalytics sync layer of the CATalytics industrial gas / LPG cylinder tracking product (LIFO Technologies).

- Written: 16-Oct-2026, from the source files in this repo (latest commit seen: 22-May-2026).
- Statements marked **(inferred)** are deductions, not things read directly in code.
- Secrets, API keys, DB credentials and host IPs are deliberately left out. Settings are named by variable only.

## Documents

| File | What it covers |
|------|----------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Purpose, the common fetch -> SQLite -> sync pattern, tech stack, high-level diagram, shared design |
| [CO_MIDDLEWARE.md](CO_MIDDLEWARE.md) | `CO_middleware/`: Chennai Oxygen. Oldest codebase, delivery-note (DC) based, two SQLite DBs |
| [BHOX_MIDDLEWARE.md](BHOX_MIDDLEWARE.md) | `BHOX_Middleware/`: multi-company, sales-invoice based, GUID batch DC sync |
| [ARASAN_GAS.md](ARASAN_GAS.md) | `arasan_gas/`: multi-company, name-based dedup, "simple" name-based DC API, 4 automation threads |
| [BOL_COMPANY.md](BOL_COMPANY.md) | `bol_company/`: multi-company, vehicle-master rule for delivery vs pickup, GUID DC API |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Every CATalytics backend endpoint the apps call, how they authenticate, direct PostgreSQL access, link to the frontend and mobile apps |
| [DEPLOYMENT.md](DEPLOYMENT.md) | How the apps are built (PyInstaller), packaged, installed and auto-started at customer sites |

Existing root docs, still useful for background: `../CO_MIDDLEWARE_COMPLETE_GUIDE.md` (CO walkthrough; parts are older than the code) and `../ARASAN_GAS_MIDDLEWARE_PLAN.md` (the design plan the multi-company apps came from; some file names in it were never created).

## The four apps compared

| Aspect | CO_middleware | BHOX_Middleware | arasan_gas | bol_company |
|--------|---------------|-----------------|------------|-------------|
| Customer site / entity | Chennai Oxygen (`ENTITY_NAME` default) | "bhox" (Bharath Oxygen, inferred from company name examples) | Arasan Gas | "bol" |
| Default `ENTITY_ID` | 1 | 25 | 25 | 25 |
| Source | Tally XML API (`TALLY_URL`) | Tally XML API | Tally XML API | Tally XML API |
| Tally companies | `TALLY_COMPANY` (single) + `TALLY_COMPANY_1..4` slots | Up to 4 (`TALLY_COMPANY_ACTIVE`) | Up to 4 | Up to 4 |
| Transaction source in Tally | Delivery Notes (DC vouchers) from the Day Book | Sales invoices, Day Book (yesterday to tomorrow) | Sales invoices from `INVOICE_FETCH_START_DATE` onward | Sales invoices, Day Book (yesterday to tomorrow) |
| DC filter | Voucher type keywords; optional `DC_REFERENCE_KEYWORDS` | "Other Reference" = D / C / delivery / pickup (`REQUIRE_DELIVERY_REF`) | "Other Reference" keywords (always strict) | Other Ref C/D, else vehicle no. checked against backend vehicle master |
| Master dedup | Name, first-come-first-served | Customers: Tally GUID create/update. Products: GUID, then canonical name | Name, first-come-first-served (`duplicate_log`) | Customers and products: Tally GUID create/update |
| Product endpoint | `/import/tally-product_name-payload/` | `/import/tally-product-payload/` | `/import/tally-product_name-payload/` | `/import/tally-product-payload/` |
| DC endpoint | `/import/tally-delivery-challan-payload/` | `/import/tally-delivery-challan-payload/` | `/import/tally-dc-name-payload/` (automation); `/import/tally-delivery-challan-payload/` (dashboard retry / sync-now) | `/import/tally-dc-guid-payload/` |
| Instant DC matching | Yes (`ENABLE_INSTANT_DC_MATCHING`, default true) | Code present, switched off (`_INSTANT_DC_ENABLED = False`) | No | Code present only in `sync_invoices_simple`, which nothing calls (inferred: inactive) |
| Automation threads | 2: daily master + invoice fetch/sync loop | 2: daily master + invoice fetch/sync loop | 4: fetch master, fetch invoices, sync master, sync invoices | 2: daily master + invoice fetch/sync loop |
| Local DB | SQLite: `TALLY_DB_PATH` (DCs) + `SQLITE_DB_PATH` (masters) | SQLite `SQLITE_DB_PATH` | SQLite `arasan_gas.sqlite` | SQLite `SQLITE_DB_PATH` |
| Dashboard | Flask `dashboard.py` + legacy Flask `web_ui.py` | Flask `dashboard.py` | Flask `dashboard.py` | Flask `dashboard.py` |
| Default port | 8787 | 8787 | 8787 | 8787 |
| Packaged EXE | `co_middleware_dashboard.exe` (+ legacy `tally_*.exe`) | `bol_dashboard.exe` (name copied from BOL) | `arasan_gas_dashboard.exe` | `bol_dashboard.exe` |
| Tests | `tests/` (unittest) + ad-hoc `test_*.py` | none | none | none |

## Across all four repos

- [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md): how the web portal, backend, middleware and mobile app connect, shared environments, and cross-repo issues.
