"""
Configuration loader for BOL Middleware
Handles multi-company Tally configuration
"""
import os
import sys
import shutil
from pathlib import Path
from dotenv import load_dotenv

# Determine base directory (works both as script and as frozen .exe)
if getattr(sys, 'frozen', False):
    # Running as PyInstaller .exe ??? files live next to the executable
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


class Config:
    """Configuration class for BOL Middleware"""

    # Entity Configuration
    ENTITY_NAME = os.getenv('ENTITY_NAME', 'bol')
    ENTITY_ID = int(os.getenv('ENTITY_ID', '25'))

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
    SYNC_INTERVAL_SECONDS = int(os.getenv('SYNC_INTERVAL_SECONDS', '30'))
    FETCH_CUSTOMERS_INTERVAL_MINUTES = int(os.getenv('FETCH_CUSTOMERS_INTERVAL_MINUTES', '10'))
    FETCH_PRODUCTS_INTERVAL_MINUTES = int(os.getenv('FETCH_PRODUCTS_INTERVAL_MINUTES', '10'))
    # New automation intervals
    FETCH_MASTER_INTERVAL_MINUTES = int(os.getenv('FETCH_MASTER_INTERVAL_MINUTES', os.getenv('FETCH_CUSTOMERS_INTERVAL_MINUTES', '10')))
    FETCH_INVOICES_INTERVAL_MINUTES = int(os.getenv('FETCH_INVOICES_INTERVAL_MINUTES', '10'))
    SYNC_INVOICES_INTERVAL_SECONDS = int(os.getenv('SYNC_INVOICES_INTERVAL_SECONDS', os.getenv('SYNC_INTERVAL_SECONDS', '30')))
    SYNC_MASTER_INTERVAL_MINUTES = int(os.getenv('SYNC_MASTER_INTERVAL_MINUTES', '5'))
    SYNC_BATCH_SIZE = int(os.getenv('SYNC_BATCH_SIZE', '50'))
    CUSTOMER_SYNC_WORKERS = int(os.getenv('CUSTOMER_SYNC_WORKERS', '10'))  # concurrent threads for customer sync


    # Invoice Fetch Configuration
    # Start date for invoice fetching (YYYYMMDD format)
    # If empty, defaults to today
    INVOICE_FETCH_START_DATE = os.getenv('INVOICE_FETCH_START_DATE', '')

    @classmethod
    def reload_from_env(cls):
        """
        Re-read .env file and refresh runtime-configurable settings.
        Call this at the start of any long-running operation (fetch, sync)
        so that changes to .env take effect without restarting the dashboard.
        """
        load_dotenv(env_path, override=True)
        cls.INVOICE_FETCH_START_DATE = os.getenv('INVOICE_FETCH_START_DATE', '')
        cls.SYNC_INTERVAL_SECONDS = int(os.getenv('SYNC_INTERVAL_SECONDS', '30'))
        cls.FETCH_CUSTOMERS_INTERVAL_MINUTES = int(os.getenv('FETCH_CUSTOMERS_INTERVAL_MINUTES', '10'))
        cls.FETCH_PRODUCTS_INTERVAL_MINUTES = int(os.getenv('FETCH_PRODUCTS_INTERVAL_MINUTES', '10'))
        cls.FETCH_MASTER_INTERVAL_MINUTES = int(os.getenv('FETCH_MASTER_INTERVAL_MINUTES', os.getenv('FETCH_CUSTOMERS_INTERVAL_MINUTES', '10')))
        cls.FETCH_INVOICES_INTERVAL_MINUTES = int(os.getenv('FETCH_INVOICES_INTERVAL_MINUTES', '10'))
        cls.SYNC_INVOICES_INTERVAL_SECONDS = int(os.getenv('SYNC_INVOICES_INTERVAL_SECONDS', os.getenv('SYNC_INTERVAL_SECONDS', '30')))
        cls.SYNC_MASTER_INTERVAL_MINUTES = int(os.getenv('SYNC_MASTER_INTERVAL_MINUTES', '5'))
        cls.SYNC_BATCH_SIZE = int(os.getenv('SYNC_BATCH_SIZE', '50'))
        cls.CUSTOMER_SYNC_WORKERS = int(os.getenv('CUSTOMER_SYNC_WORKERS', '10'))
        cls.DEFAULT_FILLING_STATION = os.getenv('DEFAULT_FILLING_STATION', 'Main Warehouse')
        cls.DEFAULT_FILLING_STATION_ID = os.getenv('DEFAULT_FILLING_STATION_ID', '1')
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

    # Database
    SQLITE_DB_PATH = str(BASE_DIR / os.getenv('SQLITE_DB_PATH', 'bol.sqlite'))

    # PostgreSQL (optional)
    POSTGRES_HOST = os.getenv('POSTGRES_HOST', 'localhost')
    POSTGRES_PORT = int(os.getenv('POSTGRES_PORT', '5432'))
    POSTGRES_DB = os.getenv('POSTGRES_DB', 'bol')
    POSTGRES_USER = os.getenv('POSTGRES_USER', 'postgres')
    POSTGRES_PASSWORD = os.getenv('POSTGRES_PASSWORD', '')

    # Logging
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = str(BASE_DIR / os.getenv('LOG_FILE', 'logs/bol.log'))
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
        """
        Get list of active Tally company names

        Returns:
            list: List of active company names
        """
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
        """
        Get company key (COMPANY_1, COMPANY_2, etc.) from company name

        Args:
            company_name: Full company name

        Returns:
            str: Company key or None
        """
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

        if not cls.CATALYTICS_API_KEY:
            errors.append("CATALYTICS_API_KEY is not set")

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
    print("=== BOL Middleware Configuration ===")
    print(f"Entity: {config.ENTITY_NAME} (ID: {config.ENTITY_ID})")
    print(f"Catalytics API: {config.CATALYTICS_API_BASE}")
    print(f"Tally URL: {config.TALLY_URL}")
    print(f"\nConfigured Companies:")
    for key, name in config.TALLY_COMPANIES.items():
        active = "ACTIVE" if key in config.TALLY_COMPANY_ACTIVE else "INACTIVE"
        print(f"  {key}: {name} [{active}]")
    print(f"\nActive Companies: {config.get_active_companies()}")
    print(f"\nSync Configuration:")
    print(f"  Sync Interval: {config.SYNC_INTERVAL_SECONDS}s")
    print(f"  Batch Size: {config.SYNC_BATCH_SIZE}")
    print(f"  Invoice Fetch Start Date: {config.INVOICE_FETCH_START_DATE or 'Today (not configured)'}")
    print(f"\nProduct Type Mapping ({len(config.PRODUCT_TYPE_MAP)} types):")
    for code, full_name in config.PRODUCT_TYPE_MAP.items():
        print(f"  ({code}) -> {full_name}")
    print(f"\nVerify After Sync: {config.VERIFY_AFTER_SYNC}")
    print(f"SQLite DB: {config.SQLITE_DB_PATH}")
