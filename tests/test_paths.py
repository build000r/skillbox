from __future__ import annotations

import sys
import unittest
from pathlib import Path, PurePosixPath


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.paths import (  # noqa: E402
    PATH_UNMAPPED,
    BoxPath,
    HostPath,
    PathTranslationError,
    PathTranslator,
)
from lib.runtime_model import compile_persistence_summary  # noqa: E402


def _persistence_env() -> dict[str, str]:
    return {
        "SKILLBOX_STORAGE_PROVIDER": "local",
        "SKILLBOX_STATE_ROOT": "./.skillbox-state",
    }


def _storage_summary() -> dict[str, object]:
    return compile_persistence_summary(ROOT_DIR, _persistence_env())


class PathTranslatorTests(unittest.TestCase):
    def test_wrappers_print_plain_paths(self) -> None:
        host = HostPath("/tmp/skillbox-host")
        box = BoxPath("/workspace/logs")

        self.assertEqual(str(host), "/tmp/skillbox-host")
        self.assertEqual(repr(host), "/tmp/skillbox-host")
        self.assertEqual(str(box), "/workspace/logs")
        self.assertEqual(repr(box), "/workspace/logs")
        self.assertEqual(Path(host), Path("/tmp/skillbox-host"))

    def test_unmapped_paths_raise_typed_error(self) -> None:
        translator = PathTranslator.from_storage(_storage_summary())

        with self.assertRaises(PathTranslationError) as box_ctx:
            translator.to_host(BoxPath("/not-mounted/in-box"))
        self.assertEqual(box_ctx.exception.code, PATH_UNMAPPED)
        self.assertEqual(box_ctx.exception.direction, "box-to-host")

        with self.assertRaises(PathTranslationError) as host_ctx:
            translator.to_box(HostPath("/not-mounted/on-host"))
        self.assertEqual(host_ctx.exception.code, PATH_UNMAPPED)
        self.assertEqual(host_ctx.exception.direction, "host-to-box")

    def test_round_trips_every_persistence_binding(self) -> None:
        storage = _storage_summary()
        bindings = storage["bindings"]
        self.assertGreater(len(bindings), 0)
        translator = PathTranslator.from_storage(storage)

        for binding in bindings:
            with self.subTest(binding=binding["id"]):
                box_root = PurePosixPath(str(binding["runtime_path"]))
                host_root = Path(str(binding["resolved_host_path"]))

                self.assertEqual(Path(translator.to_host(BoxPath(box_root))), host_root)
                self.assertEqual(str(translator.to_box(HostPath(host_root))), str(box_root))

                box_child = box_root / "nested" / "file.txt"
                host_child = host_root / "nested" / "file.txt"
                self.assertEqual(Path(translator.to_host(BoxPath(box_child))), host_child)
                self.assertEqual(str(translator.to_box(HostPath(host_child))), str(box_child))

    def test_k3_ingress_box_path_must_translate_before_host_use(self) -> None:
        translator = PathTranslator.from_persistence(ROOT_DIR, _persistence_env())
        box_route_file = BoxPath("/workspace/logs/runtime/ingress-routes.json")

        host_route_file = Path(translator.to_host(box_route_file))

        self.assertEqual(
            host_route_file,
            ROOT_DIR / ".skillbox-state" / "logs" / "runtime" / "ingress-routes.json",
        )
        self.assertNotEqual(host_route_file, Path(str(box_route_file)))
        with self.assertRaises(PathTranslationError) as ctx:
            translator.to_box(HostPath(str(box_route_file)))
        self.assertEqual(ctx.exception.code, PATH_UNMAPPED)


if __name__ == "__main__":
    unittest.main()
