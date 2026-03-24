import os
import sys
import shutil
from typing import Optional
from pathlib import Path


def load_env_file(path: Optional[str]) -> None:
    if not path:
        return
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if not key:
                continue
            os.environ[key] = value


def resolve_env_path(default_dir: str) -> str:
    # Priority: explicit env var -> EXE dir (frozen) -> default_dir -> cwd
    env_override = os.environ.get("TALLY_ENV_PATH")
    if env_override:
        return env_override

    # When running as a PyInstaller frozen EXE, always check the EXE's directory first
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        exe_env = os.path.join(exe_dir, ".env")
        if os.path.exists(exe_env):
            return exe_env

    # Then try the passed-in directory
    default_env = os.path.join(default_dir, ".env")
    if os.path.exists(default_env):
        return default_env

    # Then try current working directory
    cwd_env = os.path.join(os.getcwd(), ".env")
    if os.path.exists(cwd_env):
        return cwd_env

    # Final fallback: EXE dir (frozen) or default_dir
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), ".env")
    return default_env


def _safe_db_name(value: Optional[str]) -> str:
    name = (value or "tally_dc").strip()
    if not name:
        name = "tally_dc"
    name = name.replace(" ", "_")
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


def _normalize_tally_db_path(raw: Optional[str]) -> str:
    value = raw or ""
    if not value:
        return ""

    company_name = os.getenv("TALLY_COMPANY") or os.getenv("ENTITY_NAME") or "tally_dc"
    safe_name = _safe_db_name(company_name) + ".sqlite"

    raw_str = str(value)
    ends_with_sep = raw_str.endswith("/") or raw_str.endswith("\\")

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


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    if value is None:
        return default
    if name == "TALLY_DB_PATH":
        normalized = _normalize_tally_db_path(value)
        if normalized:
            os.environ[name] = normalized
        return normalized
    return value


def get_env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def get_env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

_env_path = BASE_DIR / '.env'
_env_example_path = BASE_DIR / '.env.example'

# Create .env from template if missing
if not _env_path.exists() and _env_example_path.exists():
    try:
        shutil.copyfile(_env_example_path, _env_path)
        print(f"[INFO] Created .env from template at: {_env_path}")
    except Exception as exc:
        print(f"[WARNING] Could not create .env from .env.example: {exc}")

load_env_file(str(_env_path))

if os.environ.get("TALLY_DB_PATH"):
    os.environ["TALLY_DB_PATH"] = _normalize_tally_db_path(os.environ.get("TALLY_DB_PATH"))

# Aliases for compatibility
if os.environ.get('ENTITY_ID') and not os.environ.get('CATALYTICS_ENTITY_ID'):
    os.environ['CATALYTICS_ENTITY_ID'] = os.environ['ENTITY_ID']
if os.environ.get('CATALYTICS_ENTITY_ID') and not os.environ.get('ENTITY_ID'):
    os.environ['ENTITY_ID'] = os.environ['CATALYTICS_ENTITY_ID']

if os.environ.get('CATALYTICS_API_BASE') and not os.environ.get('CATALYTICS_API_BASE_URL'):
    os.environ['CATALYTICS_API_BASE_URL'] = os.environ['CATALYTICS_API_BASE']
if os.environ.get('CATALYTICS_API_BASE_URL') and not os.environ.get('CATALYTICS_API_BASE'):
    os.environ['CATALYTICS_API_BASE'] = os.environ['CATALYTICS_API_BASE_URL']


class Config:
    """Configuration for CO Middleware"""

    # Entity Configuration
    ENTITY_NAME = os.getenv('ENTITY_NAME', 'Chennai Oxygen')
    ENTITY_ID = int(os.getenv('ENTITY_ID', '1'))

    # Default filling station
    DEFAULT_FILLING_STATION = os.getenv('DEFAULT_FILLING_STATION', '')
    DEFAULT_FILLING_STATION_ID = os.getenv('DEFAULT_FILLING_STATION_ID', '')

    # Catalytics Backend API
    _CAT_BASE = os.getenv('CATALYTICS_API_BASE') or os.getenv('CATALYTICS_API_BASE_URL', 'http://localhost:8000/')
    CATALYTICS_API_BASE = _CAT_BASE
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

    TALLY_COMPANY_ACTIVE = os.getenv('TALLY_COMPANY_ACTIVE', 'COMPANY_1').split(',')

    # Sync Configuration
    SYNC_INTERVAL_SECONDS = int(os.getenv('SYNC_INTERVAL_SECONDS', '60'))
    FETCH_CUSTOMERS_INTERVAL_MINUTES = int(os.getenv('FETCH_CUSTOMERS_INTERVAL_MINUTES', '10'))
    FETCH_PRODUCTS_INTERVAL_MINUTES = int(os.getenv('FETCH_PRODUCTS_INTERVAL_MINUTES', '10'))
    FETCH_MASTER_INTERVAL_MINUTES = int(os.getenv('FETCH_MASTER_INTERVAL_MINUTES', os.getenv('FETCH_CUSTOMERS_INTERVAL_MINUTES', '10')))
    FETCH_INVOICES_INTERVAL_MINUTES = int(os.getenv('FETCH_INVOICES_INTERVAL_MINUTES', '10'))
    FETCH_INVOICES_INTERVAL_SECONDS = int(os.getenv('FETCH_INVOICES_INTERVAL_SECONDS', str(FETCH_INVOICES_INTERVAL_MINUTES * 60)))
    SYNC_INVOICES_INTERVAL_SECONDS = int(os.getenv('SYNC_INVOICES_INTERVAL_SECONDS', os.getenv('SYNC_INTERVAL_SECONDS', '60')))
    SYNC_MASTER_INTERVAL_MINUTES = int(os.getenv('SYNC_MASTER_INTERVAL_MINUTES', '5'))
    SYNC_BATCH_SIZE = int(os.getenv('SYNC_BATCH_SIZE', '10'))
    CUSTOMER_SYNC_WORKERS = int(os.getenv('CUSTOMER_SYNC_WORKERS', '5'))

    # Invoice Fetch Configuration
    INVOICE_FETCH_START_DATE = os.getenv('INVOICE_FETCH_START_DATE', '')

    @classmethod
    def reload_from_env(cls):
        load_env_file(str(_env_path))
        if os.environ.get("TALLY_DB_PATH"):
            os.environ["TALLY_DB_PATH"] = _normalize_tally_db_path(os.environ.get("TALLY_DB_PATH"))
        cls.INVOICE_FETCH_START_DATE = os.getenv('INVOICE_FETCH_START_DATE', '')
        cls.SYNC_INTERVAL_SECONDS = int(os.getenv('SYNC_INTERVAL_SECONDS', '60'))
        cls.FETCH_CUSTOMERS_INTERVAL_MINUTES = int(os.getenv('FETCH_CUSTOMERS_INTERVAL_MINUTES', '10'))
        cls.FETCH_PRODUCTS_INTERVAL_MINUTES = int(os.getenv('FETCH_PRODUCTS_INTERVAL_MINUTES', '10'))
        cls.FETCH_MASTER_INTERVAL_MINUTES = int(os.getenv('FETCH_MASTER_INTERVAL_MINUTES', os.getenv('FETCH_CUSTOMERS_INTERVAL_MINUTES', '10')))
        cls.FETCH_INVOICES_INTERVAL_MINUTES = int(os.getenv('FETCH_INVOICES_INTERVAL_MINUTES', '10'))
        cls.FETCH_INVOICES_INTERVAL_SECONDS = int(os.getenv('FETCH_INVOICES_INTERVAL_SECONDS', str(cls.FETCH_INVOICES_INTERVAL_MINUTES * 60)))
        cls.SYNC_INVOICES_INTERVAL_SECONDS = int(os.getenv('SYNC_INVOICES_INTERVAL_SECONDS', os.getenv('SYNC_INTERVAL_SECONDS', '60')))
        cls.SYNC_MASTER_INTERVAL_MINUTES = int(os.getenv('SYNC_MASTER_INTERVAL_MINUTES', '5'))
        cls.SYNC_BATCH_SIZE = int(os.getenv('SYNC_BATCH_SIZE', '10'))
        cls.CUSTOMER_SYNC_WORKERS = int(os.getenv('CUSTOMER_SYNC_WORKERS', '5'))
        cls.DEFAULT_FILLING_STATION = os.getenv('DEFAULT_FILLING_STATION', '')
        cls.DEFAULT_FILLING_STATION_ID = os.getenv('DEFAULT_FILLING_STATION_ID', '')
        cls.SQLITE_DB_PATH = os.getenv('SQLITE_DB_PATH', 'chennai.sqlite')
        cls.TALLY_DB_PATH = _normalize_tally_db_path(os.getenv('TALLY_DB_PATH', ''))
        return cls.INVOICE_FETCH_START_DATE

    @classmethod
    def get_invoice_fetch_start_date(cls):
        load_env_file(str(_env_path))
        return os.getenv('INVOICE_FETCH_START_DATE', '')

    # Verification Settings
    VERIFY_AFTER_SYNC = os.getenv('VERIFY_AFTER_SYNC', 'true').lower() == 'true'
    VERIFY_RETRY_COUNT = int(os.getenv('VERIFY_RETRY_COUNT', '3'))
    VERIFY_RETRY_DELAY_SECONDS = float(os.getenv('VERIFY_RETRY_DELAY_SECONDS', '1'))
    VERIFY_TIMEOUT_SECONDS = int(os.getenv('VERIFY_TIMEOUT_SECONDS', '10'))
    RUN_DAILY_AUDIT = os.getenv('RUN_DAILY_AUDIT', 'true').lower() == 'true'
    AUDIT_TIME = os.getenv('AUDIT_TIME', '02:00')

    # Database
    SQLITE_DB_PATH = os.getenv('SQLITE_DB_PATH', 'chennai.sqlite')
    TALLY_DB_PATH = _normalize_tally_db_path(os.getenv('TALLY_DB_PATH', ''))

    # PostgreSQL (optional)
    POSTGRES_HOST = os.getenv('POSTGRES_HOST', 'localhost')
    POSTGRES_PORT = int(os.getenv('POSTGRES_PORT', '5432'))
    POSTGRES_DB = os.getenv('POSTGRES_DB', 'catalytics')
    POSTGRES_USER = os.getenv('POSTGRES_USER', 'postgres')
    POSTGRES_PASSWORD = os.getenv('POSTGRES_PASSWORD', '')

    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = str(BASE_DIR / os.getenv('LOG_FILE', 'logs/app.log'))
    LOG_MAX_BYTES = int(os.getenv('LOG_MAX_BYTES', '10485760'))
    LOG_BACKUP_COUNT = int(os.getenv('LOG_BACKUP_COUNT', '5'))

    # Product Type Mapping
    PRODUCT_TYPE_MAP: dict = {}
    _raw_type_map = os.getenv('PRODUCT_TYPE_MAP', 'CYL:CYLINDER,PLT:PALLET,TNK:TANK,CON:CONTAINER')
    for _pair in _raw_type_map.split(','):
        _pair = _pair.strip()
        if ':' in _pair:
            _code, _full = _pair.split(':', 1)
            PRODUCT_TYPE_MAP[_code.strip().upper()] = _full.strip()

    # Web UI
    WEB_UI_HOST = os.getenv('WEB_UI_HOST', 'localhost')
    WEB_UI_PORT = int(os.getenv('WEB_UI_PORT', '8787'))

    @classmethod
    def get_active_companies(cls):
        active = []
        for key in cls.TALLY_COMPANY_ACTIVE:
            key = key.strip()
            if not key or key not in cls.TALLY_COMPANIES:
                continue
            name = cls.TALLY_COMPANIES[key]
            if name:
                active.append(name)
        return active

    @classmethod
    def get_company_key(cls, company_name):
        for key, name in cls.TALLY_COMPANIES.items():
            if name == company_name:
                return key
        return None

    @classmethod
    def get_product_type_codes(cls):
        return list(cls.PRODUCT_TYPE_MAP.keys())

    @classmethod
    def get_product_type_name(cls, code):
        return cls.PRODUCT_TYPE_MAP.get(code.strip().upper(), None)


config = Config()





