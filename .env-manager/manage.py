#!/usr/bin/env python3
from __future__ import annotations

import runtime_manager as _runtime_manager


for _name in dir(_runtime_manager):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_runtime_manager, _name)


if __name__ == "__main__":
    raise SystemExit(main())
