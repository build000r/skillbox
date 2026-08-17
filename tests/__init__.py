"""Pin tempfile roots to the real path.

macOS exposes /var/folders as a symlink to /private/var/folders. Tests that
compare tempfile.TemporaryDirectory() strings to Path.resolve() output then
fail as /var/... != /private/var/.... Forcing TMPDIR onto the resolved path
makes both sides use the same prefix without changing production code.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_real_tmp = Path(tempfile.gettempdir()).resolve()
os.environ["TMPDIR"] = str(_real_tmp)
tempfile.tempdir = str(_real_tmp)
