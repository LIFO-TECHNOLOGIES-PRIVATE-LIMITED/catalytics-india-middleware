import importlib
from typing import Any, Dict, List, Optional


def _load_client():
    # "import" is a reserved keyword; load module dynamically.
    return importlib.import_module("import.tally_client")


def get_companies(url: Optional[str] = None) -> List[Dict[str, Any]]:
    client = _load_client()
    return client.get_companies(url)


def get_ledgers(company_name: str, url: Optional[str] = None) -> List[Dict[str, Any]]:
    client = _load_client()
    return client.get_ledgers(company_name, url)


def get_stock_items(company_name: str, url: Optional[str] = None) -> List[Dict[str, Any]]:
    client = _load_client()
    return client.get_stock_items(company_name, url)


def get_delivery_notes(
    company_name: str,
    url: Optional[str] = None,
    from_date: str = "20240101",
    to_date: str = "20991231",
) -> List[Dict[str, Any]]:
    client = _load_client()
    return client.get_delivery_notes(company_name, url, from_date, to_date)


def get_sales_invoices(
    company_name: str,
    url: Optional[str] = None,
    from_date: str = "20240101",
    to_date: str = "20991231",
) -> List[Dict[str, Any]]:
    client = _load_client()
    return client.get_sales_invoices(company_name, url, from_date, to_date)


def get_ledger_by_name(company_name: str, ledger_name: str, url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    client = _load_client()
    return client.get_ledger_by_name(company_name, ledger_name, url)


def get_stock_item_by_name(company_name: str, item_name: str, url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    client = _load_client()
    return client.get_stock_item_by_name(company_name, item_name, url)
