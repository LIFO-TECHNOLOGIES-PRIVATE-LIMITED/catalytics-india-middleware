import os
from typing import Optional


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
    import sys
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


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


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


import sys
from pathlib import Path


if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

_env_path = BASE_DIR / '.env'
load_env_file(str(_env_path))


class Config:
    """Configuration for CO Middleware — multi-company Tally support"""

    ENTITY_NAME = os.getenv('ENTITY_NAME', 'Chennai Oxygen')
    ENTITY_ID = int(os.getenv('ENTITY_ID', '1'))

    CATALYTICS_API_BASE = os.getenv('CATALYTICS_API_BASE_URL', 'http://localhost:8000/')
    CATALYTICS_API_KEY = os.getenv('CATALYTICS_API_KEY', '')

    TALLY_URL = os.getenv('TALLY_URL', 'http://localhost:9000/')

    TALLY_COMPANIES = {
        'COMPANY_1': os.getenv('TALLY_COMPANY_1', ''),
        'COMPANY_2': os.getenv('TALLY_COMPANY_2', ''),
        'COMPANY_3': os.getenv('TALLY_COMPANY_3', ''),
        'COMPANY_4': os.getenv('TALLY_COMPANY_4', ''),
    }

    TALLY_COMPANY_ACTIVE = os.getenv('TALLY_COMPANY_ACTIVE', 'COMPANY_1').split(',')

    FETCH_CUSTOMERS_INTERVAL_MINUTES = int(os.getenv('FETCH_CUSTOMERS_INTERVAL_MINUTES', '10'))
    FETCH_PRODUCTS_INTERVAL_MINUTES = int(os.getenv('FETCH_PRODUCTS_INTERVAL_MINUTES', '10'))

    SQLITE_DB_PATH = str(BASE_DIR / os.getenv('SQLITE_DB_PATH', 'chennai.sqlite'))

    WEB_UI_HOST = os.getenv('WEB_UI_HOST', 'localhost')
    WEB_UI_PORT = int(os.getenv('WEB_UI_PORT', '8787'))

    DEFAULT_FILLING_STATION = os.getenv('DEFAULT_FILLING_STATION', '')

    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

    # Product Type Mapping: CYL:CYLINDER,PLT:PALLET,...
    PRODUCT_TYPE_MAP: dict = {}
    _raw_type_map = os.getenv('PRODUCT_TYPE_MAP', 'CYL:CYLINDER,PLT:PALLET,TNK:TANK,CON:CONTAINER')
    for _pair in _raw_type_map.split(','):
        _pair = _pair.strip()
        if ':' in _pair:
            _code, _full = _pair.split(':', 1)
            PRODUCT_TYPE_MAP[_code.strip().upper()] = _full.strip()

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


config = Config()
