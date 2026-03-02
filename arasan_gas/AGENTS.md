# Repository Guidelines

## Project Structure & Module Organization
- Core Python modules live at the repo root: `config.py`, `db.py`, `tally_client.py`, `fetch_*.py`, `sync_to_catalytics.py`, `dashboard.py`, and `automation_manager.py`.
- `templates/` contains Flask UI templates (`templates/dashboard.html`).
- `automation/` contains Windows `.bat` scripts for service control, scheduling, and diagnostics.
- Generated/runtime artifacts are in `logs/`, `arasan_gas.sqlite`, `test_arasan_gas.sqlite`, and `__pycache__/`; treat these as environment state, not feature code.
- Script-style checks follow `test_*.py` naming in the repository root.

## Build, Test, and Development Commands
```powershell
py -m venv .venv
.\.venv\Scripts\activate
py -m pip install -r requirements.txt
copy .env.example .env
py db.py
```
- `py fetch_customers.py`, `py fetch_products.py`, `py fetch_invoices.py`: pull data from Tally.
- `py sync_to_catalytics.py`: sync unsynced data and run verification logic.
- `py dashboard.py`: start the Flask dashboard (same flow as `automation\start_dashboard.bat`).
- `automation\test_all_components.bat`: run connectivity + fetch/sync smoke checks.

## Coding Style & Naming Conventions
- Use Python conventions already present: 4-space indentation, `snake_case` for functions/modules, `PascalCase` for classes.
- Keep modules focused on one responsibility (fetch, sync, DB, dashboard, automation).
- Prefer `logging` for operational modules; reserve `print` for ad-hoc test scripts.
- No repository-level formatter/linter config is currently defined; keep edits consistent with surrounding code.

## Testing Guidelines
- There is no configured `pytest`/`unittest` suite; tests are executable integration scripts.
- Name new tests `test_<feature>.py` and keep them runnable via `py test_<feature>.py`.
- Before opening a PR, run:
```powershell
py config.py
py fetch_customers.py
py sync_to_catalytics.py
automation\test_all_components.bat
```
- Document required local services in PR notes (Tally on `localhost:9000`, Catalytics on `localhost:8000`).

## Commit & Pull Request Guidelines
- Current history uses short, direct commit subjects (for example: `middleware update`, `co middleware setup`).
- Prefer imperative, present-tense subjects and keep one logical change per commit.
- PRs should include: summary, changed scripts/modules, `.env` or migration impact, manual verification steps, and UI screenshots for dashboard/template changes.

## Security & Configuration Tips
- Use `.env.example` as the baseline and never commit real API keys or credentials.
- Avoid committing generated state unless intentional: `.sqlite` files, `logs/*.log`, and `__pycache__/`.
