"""
Check whether the packaged dashboard is reachable on the network.
"""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path


RULE_PREFIX = "CO Middleware Dashboard"
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = "8787"


def read_env_settings() -> tuple[str, str]:
    host = DEFAULT_HOST
    port = DEFAULT_PORT

    if not ENV_PATH.exists():
        return host, port

    for raw_line in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().upper()
        value = value.strip()
        if key == "WEB_UI_HOST" and value:
            host = value
        elif key == "WEB_UI_PORT" and value:
            port = value

    return host, port


def print_ip_addresses(port: str) -> None:
    print("Your IP addresses:")
    print("-" * 70)
    try:
        hostname = socket.gethostname()
        print(f"Hostname: {hostname}")
        ip_addresses = socket.gethostbyname_ex(hostname)[2]
        seen: set[str] = set()
        for ip in ip_addresses:
            if ip.startswith("127.") or ip in seen:
                continue
            seen.add(ip)
            print(f"  -> {ip}:{port}")
            print(f"     Access from other computers: http://{ip}:{port}")
        if not seen:
            print("  No non-loopback IPv4 addresses detected.")
    except Exception as exc:
        print(f"Error getting IP addresses: {exc}")


def firewall_rule_exists(rule_name: str) -> bool:
    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule_name}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and "No rules match" not in result.stdout
    except Exception:
        return False


def print_firewall_status(port: str) -> None:
    print("=" * 70)
    print("FIREWALL CHECK")
    print("=" * 70)
    print()

    port_rule = f"{RULE_PREFIX} Port {port}"
    inbound_rule = f"{RULE_PREFIX} Inbound EXE"
    outbound_rule = f"{RULE_PREFIX} Outbound EXE"

    found_any = False
    for label, rule_name in (
        ("Inbound port rule", port_rule),
        ("Inbound EXE rule", inbound_rule),
        ("Outbound EXE rule", outbound_rule),
    ):
        exists = firewall_rule_exists(rule_name)
        status = "OK" if exists else "MISSING"
        print(f"{label}: {status} ({rule_name})")
        found_any = found_any or exists

    print()
    if not found_any:
        print("Run allow_firewall.bat as Administrator to add the firewall rules.")
    else:
        print("If remote access still fails, confirm the EXE is running and the port is correct.")
    print()


def print_configuration_status(host: str, port: str) -> None:
    print("=" * 70)
    print("CONFIGURATION CHECK")
    print("=" * 70)
    print()

    if not ENV_PATH.exists():
        print(f"MISSING: {ENV_PATH}")
        print("Launch the EXE once so it can create .env automatically.")
        print()
        return

    print(f".env path: {ENV_PATH}")
    print(f"WEB_UI_HOST={host}")
    print(f"WEB_UI_PORT={port}")

    if host == "0.0.0.0":
        print("OK: WEB_UI_HOST allows network access.")
    elif host in {"localhost", "127.0.0.1"}:
        print("MISSING: WEB_UI_HOST is local-only.")
        print("Change it to WEB_UI_HOST=0.0.0.0 for network/server access.")
    else:
        print("INFO: WEB_UI_HOST uses a custom bind address.")
    print()


def main() -> None:
    host, port = read_env_settings()

    print("=" * 70)
    print("CO MIDDLEWARE - NETWORK ACCESS CHECK")
    print("=" * 70)
    print()

    print_ip_addresses(port)
    print()
    print_firewall_status(port)
    print_configuration_status(host, port)

    exe_path = BASE_DIR / "co_middleware_dashboard.exe"
    print("=" * 70)
    print("EXE CHECK")
    print("=" * 70)
    print()
    print(f"EXE path: {exe_path}")
    print("OK" if exe_path.exists() else "MISSING: co_middleware_dashboard.exe")
    print()

    input("Press Enter to exit...")


if __name__ == "__main__":
    main()