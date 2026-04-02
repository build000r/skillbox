#!/usr/bin/env python3
from __future__ import annotations

import manage_impl as _manage_impl


for _name in dir(_manage_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_manage_impl, _name)


if __name__ == "__main__":
    raise SystemExit(main())
