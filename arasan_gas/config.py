"""
Configuration loader for Arasan Gas Middleware
Handles multi-company Tally configuration
"""
import os
import sys
import shutil
from pathlib import Path
from dotenv import load_dotenv

# Determine base directory (works both as script and as frozen .exe)
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

# Load .env file
env_path = BASE_DIR / '.env'
env_example_path = BASE_DIR / '.env.example'

if not env_path.exists() and env_example_path.exists():
    try:
        shutil.copyfile(env_example_path, env_path)
        print(f"[INFO] Created .env from template at: {env_path}")
    except Exception as e:
        print(f"[WARNING] Could not create .env from .env.example: {e}")

load_dotenv(env_path)


def _safe_db_name(name: str) -> str:
    """Sanitize a string for use as a filename (no spaces/special chars)."""
    name = name.strip().lower()
    name = name.replace(" ", "_")
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


def _normalize_sqlite_db_path(raw: str | None) -> str:
    """
    Resolve SQLITE_DB_PATH from .env into an absolute path.

    Rules (same as CO_middleware / bol_company):
      - If the value is empty / not set -> default to {ENTITY_NAME}.sqlite in BASE_DIR
      - If the value is a directory (or ends with / or \\) -> append {ENTITY_NAME}.sqlite
      - If the value has no file extension -> treat as directory, append {ENTITY_NAME}.sqlite
      - Relative paths are resolved against BASE_DIR
      - Parent directories are created if missing
      - If the file does not exist yet, it is created (empty) so that Database() can open it
    """
    entity_name = os.getenv('ENTITY_NAME', 'arasan_gas')
    safe_name = _safe_db_name(entity_name) + ".sqlite"

    value = (raw or "").strip()
    if not value:
        return str(BASE_DIR / safe_name)

    ends_with_sep = value.endswith("/") or value.endswith("\\")

    p = Path(value)
    if not p.is_absolute():
        p = BASE_DIR / value

    try:
        if ends_with_sep or (p.exists() and p.is_dir()):
            p = p / safe_name
    except Exception:
        if ends_with_sep:
            p = p / safe_name

    if p.suffix == "":
        p = p / safe_name

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.touch()
    except Exception:
        pass

    return str(p)


def _normalize_tally_db_path(raw: str | None) -> str:
    '''Normalize TALLY_DB_PATH overrides (mirrors CO_middleware behaviour).'''
    value = (raw or "").strip()
    if not value:
        return ""

    company_name = os.getenv('TALLY_COMPANY') or os.getenv('ENTITY_NAME', 'arasan_gas')
    safe_name = _safe_db_name(company_name) + ".sqlite"

    ends_with_sep = value.endswith('/') or value.endswith('\\')
    p = Path(value)
    if not p.is_absolute():
        p = BASE_DIR / value

    try:
        if ends_with_sep or (p.exists() and p.is_dir()):
            p = p / safe_name
    except Exception:
        if ends_with_sep:
            p = p / safe_name

    if p.suffix == "":
        p = p / safe_name

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.touch()
    except Exception:
        pass

    return str(p)


def _resolve_database_paths() -> tuple[str, str]:
    '''Return the paths used for SQLITE_DB_PATH and TALLY_DB_PATH.'''
    sqlite_path = _normalize_sqlite_db_path(os.getenv('SQLITE_DB_PATH'))
    tally_path = _normalize_tally_db_path(os.getenv('TALLY_DB_PATH'))
    if tally_path:
        sqlite_path = tally_path
    else:
        tally_path = sqlite_path
    return sqlite_path, tally_path


class Config:
    """Configuration class for Arasan Gas Middleware"""

    # Entity Configuration
    ENTITY_NAME = os.getenv('ENTITY_NAME', 'arasan_gas')
    ENTITY_ID = int(os.getenv('ENTITY_ID', '25'))

    # Default admin user ID for created_by / modified_by in Catalytics backend
    DEFAULT_ADMIN_USER_ID = int(os.getenv('DEFAULT_ADMIN_USER_ID', '55'))

    # Default filling station (used when invoice has empty godown/location)
    DEFAULT_FILLING_STATION = os.getenv('DEFAULT_FILLING_STATION', 'Main Warehouse')
    DEFAULT_FILLING_STATION_ID = os.getenv('DEFAULT_FILLING_STATION_ID', '1')

    # Catalytics Backend API
    CATALYTICS_API_BASE = os.getenv('CATALYTICS_API_BASE', 'http://localhost:8000')
    CATALYTICS_API_KEY = os.getenv('CATALYTICS_API_KEY', '')

    # Tally Configuration
    TALLY_URL = os.getenv('TALLY_URL', 'http://localhost:9000/')

    # Multi-Company Configuration
    TALLY_COMPANIES = {
        'COMPANY_1': os.getenv('TALLY_COMPANY_1', ''),
        'COMPANY_2': os.getenv('TALLY_COMPANY_2', ''),
        'COMPANY_3': os.getenv('TALLY_COMPANY_3', ''),
        'COMPANY_4': os.getenv('TALLY_COMPANY_4', ''),
    }

    # Active Companies
    TALLY_COMPANY_ACTIVE = os.getenv('TALLY_COMPANY_ACTIVE', 'COMPANY_1').split(',')

    # Sync Configuration
    FETCH_SYNC_INTERVAL_SECONDS = int(os.getenv('FETCH_SYNC_INTERVAL_SECONDS', '10'))
    MASTER_SYNC_TIME = os.getenv('MASTER_SYNC_TIME', '02:00')
    MASTER_SYNC_FAILURE_RETRY_MINUTES = int(os.getenv('MASTER_SYNC_FAILURE_RETRY_MINUTES', '30'))
    CUSTOMER_SYNC_WORKERS = int(os.getenv('CUSTOMER_SYNC_WORKERS', '10'))

    # Invoice Fetch Configuration
    INVOICE_FETCH_START_DATE = os.getenv('INVOICE_FETCH_START_DATE', '')
    REQUIRE_DELIVERY_REF = os.getenv('REQUIRE_DELIVERY_REF', 'false').lower() == 'true'

    @classmethod
    def reload_from_env(cls):
        """
        Re-read .env file and refresh runtime-configurable settings.
        Call this at the start of any long-running operation (fetch, sync)
        so that changes to .env take effect without restarting the dashboard.
        """
        load_dotenv(env_path, override=True)

        # Refresh sensitive API and DB settings
        cls.CATALYTICS_API_KEY = os.getenv('CATALYTICS_API_KEY', cls.CATALYTICS_API_KEY)
        cls.CATALYTICS_API_BASE = os.getenv('CATALYTICS_API_BASE', cls.CATALYTICS_API_BASE)
        cls.POSTGRES_DB = os.getenv('POSTGRES_DB', cls.POSTGRES_DB)
        cls.ENTITY_ID = int(os.getenv('ENTITY_ID', str(cls.ENTITY_ID)))
        cls.ENTITY_NAME = os.getenv('ENTITY_NAME', cls.ENTITY_NAME)
        sql_path, tally_path = _resolve_database_paths()
        cls.SQLITE_DB_PATH = sql_path
        cls.TALLY_DB_PATH = tally_path

        cls.INVOICE_FETCH_START_DATE = os.getenv('INVOICE_FETCH_START_DATE', '')
        cls.REQUIRE_DELIVERY_REF = os.getenv('REQUIRE_DELIVERY_REF', 'false').lower() == 'true'
        cls.FETCH_SYNC_INTERVAL_SECONDS = int(os.getenv('FETCH_SYNC_INTERVAL_SECONDS', '10'))
        cls.CUSTOMER_SYNC_WORKERS = int(os.getenv('CUSTOMER_SYNC_WORKERS', '10'))
        cls.DEFAULT_ADMIN_USER_ID = int(os.getenv('DEFAULT_ADMIN_USER_ID', '55'))
        cls.DEFAULT_FILLING_STATION = os.getenv('DEFAULT_FILLING_STATION', 'Main Warehouse')
        cls.DEFAULT_FILLING_STATION_ID = os.getenv('DEFAULT_FILLING_STATION_ID', '1')
        cls.MASTER_SYNC_TIME = os.getenv('MASTER_SYNC_TIME', cls.MASTER_SYNC_TIME)
        cls.MASTER_SYNC_FAILURE_RETRY_MINUTES = int(os.getenv('MASTER_SYNC_FAILURE_RETRY_MINUTES', str(cls.MASTER_SYNC_FAILURE_RETRY_MINUTES)))
        return cls.INVOICE_FETCH_START_DATE

    @classmethod
    def get_invoice_fetch_start_date(cls):
        """
        Always read INVOICE_FETCH_START_DATE fresh from .env.
        Returns the date string (YYYYMMDD) or empty string.
        """
        load_dotenv(env_path, override=True)
        return os.getenv('INVOICE_FETCH_START_DATE', '')

    # Verification Settings
    VERIFY_AFTER_SYNC = os.getenv('VERIFY_AFTER_SYNC', 'true').lower() == 'true'
    VERIFY_RETRY_COUNT = int(os.getenv('VERIFY_RETRY_COUNT', '3'))
    VERIFY_RETRY_DELAY_SECONDS = float(os.getenv('VERIFY_RETRY_DELAY_SECONDS', '1'))
    VERIFY_TIMEOUT_SECONDS = int(os.getenv('VERIFY_TIMEOUT_SECONDS', '10'))
    RUN_DAILY_AUDIT = os.getenv('RUN_DAILY_AUDIT', 'true').lower() == 'true'
    AUDIT_TIME = os.getenv('AUDIT_TIME', '02:00')

    # Database — resolved via _normalize_sqlite_db_path (see CO_middleware pattern)
    _sqlite_path, _tally_path = _resolve_database_paths()
    SQLITE_DB_PATH = _sqlite_path
    TALLY_DB_PATH = _tally_path

    # PostgreSQL (optional)
    POSTGRES_HOST = os.getenv('POSTGRES_HOST', 'localhost')
    POSTGRES_PORT = int(os.getenv('POSTGRES_PORT', '5432'))
    POSTGRES_DB = os.getenv('POSTGRES_DB', 'arasan_gas')
    POSTGRES_USER = os.getenv('POSTGRES_USER', 'postgres')
    POSTGRES_PASSWORD = os.getenv('POSTGRES_PASSWORD', '')

    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = str(BASE_DIR / os.getenv('LOG_FILE', 'logs/arasan_gas.log'))
    LOG_MAX_BYTES = int(os.getenv('LOG_MAX_BYTES', '10485760'))
    LOG_BACKUP_COUNT = int(os.getenv('LOG_BACKUP_COUNT', '5'))

    # Product Type Mapping (stock item name parsing)
    # Format in .env: CYL:CYLINDER,PLT:PALLET,TNK:TANK,CON:CONTAINER
    PRODUCT_TYPE_MAP = {}
    _raw_type_map = os.getenv('PRODUCT_TYPE_MAP', 'CYL:CYLINDER,PLT:PALLET,TNK:TANK,CON:CONTAINER')
    for _pair in _raw_type_map.split(','):
        _pair = _pair.strip()
        if ':' in _pair:
            _code, _full = _pair.split(':', 1)
            PRODUCT_TYPE_MAP[_code.strip().upper()] = _full.strip()

    # Web UI
    WEB_UI_PORT = int(os.getenv('WEB_UI_PORT', '8787'))
    WEB_UI_HOST = os.getenv('WEB_UI_HOST', 'localhost')

    @classmethod
    def get_active_companies(cls):
        """Get list of active Tally company names"""
        active_companies = []

        for key in cls.TALLY_COMPANY_ACTIVE:
            key = key.strip()
            if not key:
                continue
            if key not in cls.TALLY_COMPANIES:
                import logging
                logging.getLogger(__name__).warning(
                    f"[CONFIG] Company key '{key}' is listed in TALLY_COMPANY_ACTIVE "
                    f"but is not a valid slot (expected COMPANY_1..COMPANY_4). Skipping."
                )
                continue
            company_name = cls.TALLY_COMPANIES[key]
            if not company_name:
                import logging
                logging.getLogger(__name__).warning(
                    f"[CONFIG] Company key '{key}' is listed in TALLY_COMPANY_ACTIVE "
                    f"but TALLY_{key} is blank or not set in .env. Skipping."
                )
                continue
            active_companies.append(company_name)

        return active_companies

    @classmethod
    def get_company_key(cls, company_name):
        """Get company key (COMPANY_1, COMPANY_2, etc.) from company name"""
        for key, name in cls.TALLY_COMPANIES.items():
            if name == company_name:
                return key
        return None

    @classmethod
    def get_product_type_codes(cls):
        """Get list of recognized product type codes, e.g. ['CYL', 'PLT', 'TNK', 'CON']"""
        return list(cls.PRODUCT_TYPE_MAP.keys())

    @classmethod
    def get_product_type_name(cls, code):
        """Get full product type name from code, e.g. 'CYL' -> 'CYLINDER'"""
        return cls.PRODUCT_TYPE_MAP.get(code.strip().upper(), None)

    @classmethod
    def validate(cls):
        """Validate configuration"""
        errors = []

        if not cls.get_active_companies():
            errors.append("No active Tally companies configured")

        if not cls.PRODUCT_TYPE_MAP:
            errors.append("PRODUCT_TYPE_MAP is empty — no product types configured")

        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")

        return True


# Create global config instance
config = Config()


if __name__ == '__main__':
    # Test configuration
    print("=== Arasan Gas Middleware Configuration ===")
    print(f"Entity: {config.ENTITY_NAME} (ID: {config.ENTITY_ID})")
    print(f"Catalytics API: {config.CATALYTICS_API_BASE}")
    print(f"Tally URL: {config.TALLY_URL}")
    print(f"\nConfigured Companies:")
    for key, name in config.TALLY_COMPANIES.items():
        active = "ACTIVE" if key in config.TALLY_COMPANY_ACTIVE else "INACTIVE"
        print(f"  {key}: {name} [{active}]")
    print(f"\nActive Companies: {config.get_active_companies()}")
    print(f"\nSync Configuration:")
    print(f"  Fetch+Sync Interval: {config.FETCH_SYNC_INTERVAL_SECONDS}s")
    print(f"  Master Sync Time: {config.MASTER_SYNC_TIME}")
    print(f"  Invoice Fetch Start Date: {config.INVOICE_FETCH_START_DATE or 'Day Book (not configured)'}")
    print(f"\nProduct Type Mapping ({len(config.PRODUCT_TYPE_MAP)} types):")
    for code, full_name in config.PRODUCT_TYPE_MAP.items():
        print(f"  ({code}) -> {full_name}")
    print(f"\nVerify After Sync: {config.VERIFY_AFTER_SYNC}")
    print(f"SQLite DB: {config.SQLITE_DB_PATH}")
