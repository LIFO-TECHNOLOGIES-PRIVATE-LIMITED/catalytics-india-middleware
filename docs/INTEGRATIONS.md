# Integrations

All outbound calls go to the CATalytics Django backend (`catalytics-india-backend`). The base URL comes from `CATALYTICS_API_BASE` (CO: `CATALYTICS_API_BASE_URL`, which may end in `/import`; CO strips or adds `/import` as needed). The local Tally instance is the only other system called.

## 1. Tally (source)

| Item | Detail |
|------|--------|
| Protocol | HTTP POST of Tally XML envelopes (`<COLLECTION>` requests for Company, Ledger, StockItem, Voucher) to `TALLY_URL` (default port 9000, per scripts and docs) |
| Auth | None |
| Throttling | Global lock + `TALLY_REQUEST_COOLDOWN` sleep after each request (CO guide: 10 s; BHOX code default 2 s) |
| Timeouts | 60 s default. BHOX: `TALLY_TIMEOUT_LEDGER`, `TALLY_TIMEOUT_VOUCHER` |
| Filtering | Done in Python. The CO guide notes that Tally `FILTERS` in collections return empty data |

## 2. CATalytics backend endpoints called

The "Used by" column lists the apps whose code calls the endpoint. Paths are relative to the backend base URL.

### Import (write) endpoints

| Method + path | Purpose | CO | BHOX | Arasan | BOL |
|---------------|---------|----|------|--------|-----|
| `POST /import/tally-customer-payload/` | Create/update customers from Tally ledgers (`ledger` or `ledgers[]`, `entity_id` / `company_name`) | yes | yes (batches of 100) | yes | yes (batches of 100) |
| `POST /import/tally-product-payload/` | Create/update products from stock items (GUID-based) | legacy `web_ui.py` only | yes (batches of 100) | - | yes (batches of 100) |
| `POST /import/tally-product_name-payload/` | Name-based product create/update | yes | dashboard per-product refetch (`_sync_product_to_catalytics`) | yes | dashboard per-product refetch |
| `POST /import/tally-delivery-challan-payload/` | Full DC payload: `voucher(s)` + optional `ledgers` / `stock_items` maps | yes (main DC path) | yes (main DC path, batches of 50, plus Instant-DC reconcile) | dashboard retry / resync / sync-now | - |
| `POST /import/tally-dc-name-payload/` | Lightweight DC: `entity_id`, `company_name`, `voucher`, `filling_station_id`; backend matches by names | - | - | yes (automation) | unused method only |
| `POST /import/tally-dc-guid-payload/` | Batch DC payload matched by Tally GUIDs | - | - | - | yes (automation, batches of 50) |

Also referenced but **not called** by the main code: `/import/tally-product-delete/` (`CO_middleware/sync_products_old.py`) and `/import/fetch-missing-data/` (`CO_middleware/AUTO_FETCH_IMPLEMENTATION.py`, a design snippet).

### Lookup / update endpoints

| Method + path | Purpose | Used by |
|---------------|---------|---------|
| `GET /master/gas_filling_station` (params incl. `entity_id`) | Resolve a DC fill station name to its id | all four |
| `GET /master/vehicle` (`limit_start`, `limit_end`) | Vehicle master, for the D/C rule | BOL |
| `GET /transaction/delivery_challan/instant/unsynced` | Instant DCs created on the portal that have no Tally voucher yet | CO (active), BHOX (disabled), BOL (unused path) |
| `GET /transaction/delivery_challan/{id}` | Instant DC detail when the list lacks fields | CO, BHOX, BOL |
| `POST /transaction/delivery_challan/instant/{id}/mark-synced` | Mark the portal Instant DC as matched to a Tally voucher | CO, BHOX, BOL |
| `GET /transaction/delivery_challan?dc_no=...` | Find a DC id by number | CO |
| `PATCH` / `PUT` / `POST /transaction/delivery_challan/{id}` | Set `fill_station` on a DC (tries each method in turn) | CO |
| `PATCH` / `POST /api/transaction/delivery-challan/{id}/`, `PATCH /api/delivery-challan/{id}/` | Set `fill_station`; several payload shapes tried in turn | BHOX, Arasan, BOL |
| `GET /api/master/customer/{id}/`, `GET /api/master/product/{id}/`, `GET /api/transaction/delivery-challan/{id}/` | Post-sync verification (`verify_sync.py`, `data_matcher.py`) | all four |
| `GET /api/health/` and `GET {base}` | Connectivity check on the dashboard Diagnostics panel | BHOX, Arasan, BOL (dashboard) |

Whether every one of these routes exists on the backend could not be checked from this repo. The fill-station code tries up to six endpoint/payload variants, which suggests the correct one was unknown when it was written (inferred).

## 3. Authentication (names only)

| Call type | How it authenticates |
|-----------|---------------------|
| `/import/...-payload/` endpoints | **No auth header** in Arasan, BOL and CO (`Content-Type` only). Code comments: payload endpoints are `AllowAny` on the backend and check a `TALLY_MIDDLEWARE_API_KEY` only if that is configured in Django settings |
| BHOX, all API calls | `CATALYTICS_USE_AUTH`: `false` = never send a token; `true` = always send `Authorization: Bearer <CATALYTICS_API_KEY>`; `auto` (default) = send anonymously, retry once with the Bearer token on 401/403 |
| CO Instant-DC, lookup and fill-station calls | `Authorization: Bearer <CATALYTICS_API_KEY>` when the key is set (`_build_optional_auth_headers`) |
| Verification / data match (`verify_sync.py`, `data_matcher.py`) | `Entity-Id: <ENTITY_ID>` header. Arasan also sends a Bearer token. BHOX follows `CATALYTICS_USE_AUTH` |
| BOL vehicle master | Anonymous |
| Entity scoping | `entity_id` in payloads / params (`ENTITY_ID` / `CATALYTICS_ENTITY_ID`), plus `company_name` (the Tally company) |
| Created-by | `DEFAULT_ADMIN_USER_ID` is placed in some payloads as `created_by` |

The legacy CO `web_ui.py` protects its own local API with an optional `X-API-Key` header (`UI_API_KEY`). The current dashboards have no auth.

## 4. Direct PostgreSQL access (bypasses the API)

`psycopg2` connects to the backend database using `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD`. That means the site PC needs network access to the backend DB, not only to the API (inferred).

| Operation | Tables | Apps |
|-----------|--------|------|
| `UPDATE ... SET fill_station = %s WHERE id = %s` (fallback when the API patch fails) | `transaction.delivery_challan` | BHOX, Arasan, BOL |
| DC verification by number (`verify_dc_by_number`) | `transaction.delivery_challan` | all multi-company apps; CO `verify_sync.py` |
| Vehicle list fallback | `master.vehicle` | BOL |
| Product groups for Instant DC matching | `master.product_group` | BOL (inactive path) |
| Dashboard "Data Verification" counts | customer / product / DC tables | CO dashboard; `data_matcher.py` in the others |

Release notes (v1.2.0, 03-Mar-2026) record a backend table-name case fix (`master.Customer` to `master.customer`), so this code is coupled to the backend schema.

## 5. Link to frontend and mobile

- No code in this repo calls or references `catalytics-india` (Angular) or `catalytics-mobile` (Flutter).
- The only cross-app workflow visible is **Instant DCs**. In code comments, an "incharge" creates an Instant DC on the "portal"; the middleware later matches it to the Tally invoice (customer, date, products/quantities) and re-sends the payload with `matched_dc_id` so the backend updates the existing DC instead of creating a duplicate. Which client creates Instant DCs (web portal or mobile app) is not stated (inferred: the CATalytics frontend and/or mobile app).
- DCs, customers and products that the middleware creates appear in the frontend and mobile apps through the shared backend (inferred).
- Each middleware dashboard is a separate local web UI, not part of the Angular frontend.
