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
            existing = os.environ.get(key)
            if existing is None or str(existing).strip() == "":
                os.environ[key] = value


def resolve_env_path(default_dir: str) -> str:
    # Priority: explicit env var -> cwd/.env -> default_dir/.env
    env_override = os.environ.get("TALLY_ENV_PATH")
    if env_override:
        return env_override
    cwd_env = os.path.join(os.getcwd(), ".env")
    if os.path.exists(cwd_env):
        return cwd_env
    return os.path.join(default_dir, ".env")


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
