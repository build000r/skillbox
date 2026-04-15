from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
SHARED_JAM_SCRIPT = ROOT_DIR / "scripts" / "03-shared-jam.sh"


class SharedJamScriptTests(unittest.TestCase):
    def test_invite_prints_register_handoff_for_collaborator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bin_dir = root / "bin"
            bin_dir.mkdir(parents=True)
            sshd_config = root / "99-skillbox-tailnet.conf"
            sshd_config.write_text("Match User sandbox\n", encoding="utf-8")

            (bin_dir / "tailscale").write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"$1\" == \"share\" && \"$2\" == \"list\" ]]; then\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$1\" == \"share\" && \"$2\" == \"create\" && \"$3\" == \"--with\" ]]; then\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$1\" == \"status\" && \"$2\" == \"--json\" ]]; then\n"
                "  printf '{}\\n'\n"
                "  exit 0\n"
                "fi\n"
                "echo \"unexpected tailscale invocation: $*\" >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            (bin_dir / "jq").write_text(
                "#!/usr/bin/env bash\n"
                "printf 'skillbox-shared.tailnet.ts.net\\n'\n",
                encoding="utf-8",
            )
            (bin_dir / "tailscale").chmod(0o755)
            (bin_dir / "jq").chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["SKILLBOX_SHARED_JAM_SSHD_CONFIG"] = str(sshd_config)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'source "{SHARED_JAM_SCRIPT}"; cmd_invite "alice@example.com"',
                ],
                cwd=ROOT_DIR,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Shared with alice@example.com.", result.stdout)
            self.assertIn("They should run: ssh sandbox@skillbox-shared.tailnet.ts.net", result.stdout)
            self.assertIn(
                "python3 scripts/box.py register alice --host skillbox-shared.tailnet.ts.net --ssh-user sandbox",
                result.stdout,
            )


if __name__ == "__main__":
    unittest.main()
