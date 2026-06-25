"""Copy-pasteable CLI command hints for agent-facing brain payloads."""
from __future__ import annotations

MANAGE_PY = "python3 .env-manager/manage.py"


def manage_py_command(*parts: str) -> str:
    """Return a stable command hint for the public manage.py entrypoint."""
    return " ".join([MANAGE_PY, *(str(part) for part in parts if str(part))])

