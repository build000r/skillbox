from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SBP = ROOT / "scripts" / "sbp"
EXPECTED_ENCODING = {
    "array_order": "preserved",
    "charset": "utf-8",
    "line_termination": "LF",
    "non_finite_numbers": "rejected",
    "object_keys": "sorted",
}
EXPECTED_COMMANDS = [
    "status", "cwd", "locate", "list", "host", "doctor", "explain",
    "capabilities",
]
EXPECTED_OPTIONS = ["--json", "--timeout-seconds N"]
EXPECTED_SELECTORS = ["repo:", "host:", "origin:", "path:", "cwd"]
EXPECTED_EXIT_CODES = {
    "blocked": 1,
    "clean": 0,
    "partial_or_unreachable": 3,
    "usage_or_config": 2,
}
EXPECTED_REASONS = [
    "ABSENT", "AMP_AUTHORITY_UNREACHABLE", "AMP_SNAPSHOT_UNAVAILABLE",
    "CAPSULE_INDETERMINATE", "CHECKOUT_ROOT_UNKNOWN", "COMMAND_TIMEOUT",
    "DECLARED_AVAILABLE_MISSING", "DECLARED_NOT_INSTALLED", "DECLARED_UNKNOWN",
    "DISCOVERED_UNREGISTERED", "HOST_COLLECTION_FAILED", "HOST_UNREACHABLE",
    "IDENTITY_AMBIGUOUS", "IDENTITY_CONFLICT", "IDENTITY_NOT_FOUND",
    "IDENTITY_UNKNOWN", "IDENTITY_UNREACHABLE", "LINKED_WORKTREE",
    "LIVE_ORIGIN_FAILED", "NOT_APPLICABLE", "PROJECT_ABSENT", "REGISTRY_DRIFT",
    "REGISTRY_INDETERMINATE", "RUNTIME_AUTHORITY_UNKNOWN",
    "STALE_AMP_THREAD_SNAPSHOT", "STALE_EVIDENCE", "TOTAL_BUDGET_EXCEEDED",
    "WRITER_BLOCKED",
]
EXPECTED_ROUTING_CAPABILITIES = [
    "repo.read.doctor", "repo.read.explain", "repo.read.host",
    "repo.read.list", "repo.read.status",
]
KNOWN_FIELDS = [
    ("schema_version",), ("exit_code",), ("encoding",), ("payload",),
    ("payload", "schema"), ("payload", "command_schema_version"),
    ("payload", "encoding"), ("payload", "commands"),
    ("payload", "global_options"), ("payload", "selectors"),
    ("payload", "exit_codes"), ("payload", "reason_codes"),
    ("payload", "reason_routing_capabilities"), ("payload", "default"),
    ("payload", "read_only"), ("payload", "fetch"),
]
ORDERED_FIELDS = [
    ("payload", "commands"), ("payload", "global_options"),
    ("payload", "selectors"), ("payload", "reason_codes"),
    ("payload", "reason_routing_capabilities"),
]


def write_fake_engine(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            f"""\
            from __future__ import annotations

            import base64
            import json
            import os
            from pathlib import Path
            import subprocess
            import sys
            import time

            ENCODING = {EXPECTED_ENCODING!r}
            COMMANDS = {EXPECTED_COMMANDS!r}
            OPTIONS = {EXPECTED_OPTIONS!r}
            SELECTORS = {EXPECTED_SELECTORS!r}
            EXIT_CODES = {EXPECTED_EXIT_CODES!r}
            REASONS = {EXPECTED_REASONS!r}
            ROUTING = {EXPECTED_ROUTING_CAPABILITIES!r}

            def capability() -> dict[str, object]:
                return {{
                    "encoding": dict(ENCODING),
                    "exit_code": 0,
                    "payload": {{
                        "schema": "repo-atlas-cli/v1",
                        "command_schema_version": "repo-atlas-command/v1",
                        "encoding": dict(ENCODING),
                        "commands": list(COMMANDS),
                        "global_options": list(OPTIONS),
                        "selectors": list(SELECTORS),
                        "exit_codes": dict(EXIT_CODES),
                        "default": {{"command": "status", "selector": "."}},
                        "reason_codes": list(REASONS),
                        "reason_routing_capabilities": list(ROUTING),
                        "read_only": True,
                        "fetch": False,
                    }},
                    "schema_version": "repo-atlas-command/v1",
                }}

            args = sys.argv[1:]
            if args == ["capabilities", "--json"]:
                mode = os.environ.get("FAKE_CAP_MODE", "valid")
                if mode == "missing":
                    raise SystemExit(7)
                if mode == "malformed":
                    sys.stdout.write("not-json\\n")
                    raise SystemExit(0)
                if mode == "duplicate":
                    sys.stdout.write('{{"schema_version":"repo-atlas-command/v1","schema_version":"repo-atlas-command/v2"}}')
                    raise SystemExit(0)
                if mode == "nonfinite":
                    sys.stdout.write('{{"schema_version":"repo-atlas-command/v1","future":1e999}}')
                    raise SystemExit(0)
                value = capability()
                if mode in {{"exact-max", "max-plus-one"}}:
                    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    limit = 262_144 + (1 if mode == "max-plus-one" else 0)
                    if len(raw) > limit:
                        raise SystemExit(91)
                    sys.stdout.buffer.write(raw + b" " * (limit - len(raw)))
                    sys.stdout.buffer.flush()
                    raise SystemExit(0)
                if mode in {{"oversize-sleep", "timeout"}}:
                    pid_file = os.environ.get("FAKE_CAP_PID_FILE")
                    if pid_file:
                        Path(pid_file).write_text(str(os.getpid()), encoding="ascii")
                    if mode == "oversize-sleep":
                        sys.stdout.buffer.write(b"x" * 262_145)
                        sys.stdout.buffer.flush()
                    sys.stderr.write("ghp_PROBE_STDERR_MUST_NOT_ECHO\\n")
                    sys.stderr.flush()
                    time.sleep(30)
                    raise SystemExit(0)
                if mode in {{"group-overflow", "group-timeout", "early-exit", "success-daemon"}}:
                    leader_file = os.environ.get("FAKE_CAP_PID_FILE")
                    child_file = os.environ.get("FAKE_CAP_CHILD_PID_FILE")
                    if leader_file:
                        Path(leader_file).write_text(str(os.getpid()), encoding="ascii")
                    if not child_file:
                        raise SystemExit(92)
                    child_code = (
                        "import os,signal,sys,time;from pathlib import Path;"
                        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                        "Path(sys.argv[1]).write_text(str(os.getpid()),encoding='ascii');"
                        "time.sleep(30)"
                    )
                    subprocess.Popen([sys.executable, "-c", child_code, child_file])
                    deadline = time.monotonic() + 2
                    while not Path(child_file).exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if mode == "group-overflow":
                        sys.stdout.buffer.write(b"x" * 262_145)
                        sys.stdout.buffer.flush()
                        time.sleep(30)
                    if mode == "group-timeout":
                        time.sleep(30)
                    if mode == "early-exit":
                        raise SystemExit(0)
                    print(json.dumps(value, sort_keys=True, separators=(",", ":")), flush=True)
                    raise SystemExit(0)
                if mode == "skew":
                    value["schema_version"] = "repo-atlas-command/v2"
                elif mode == "wrong-encoding":
                    value["payload"]["encoding"]["charset"] = "latin-1"
                elif mode == "missing-command":
                    value["payload"]["commands"].remove("doctor")
                elif mode == "hostile":
                    value["future"] = "ghp_SECRET\\u0000"
                elif mode == "additive":
                    value["future"] = {{"safe": ["additive", 1, True]}}
                    value["payload"]["future"] = {{"compatible": True}}
                elif mode in {{"too-deep", "deep-hostile"}}:
                    depth = 40 if mode == "too-deep" else 1500
                    atom = '0' if mode == "too-deep" else '"ghp_DEEP_HOSTILE_DO_NOT_ECHO"'
                    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
                    sys.stdout.write(raw[:-1] + ',"future":' + '[' * depth + atom + ']' * depth + '}}')
                    raise SystemExit(0)
                elif mode == "too-many":
                    value["future"] = list(range(5000))
                elif mode == "oversized":
                    value["future"] = "x" * 300000
                elif mode == "nested-duplicate":
                    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
                    sys.stdout.write(raw.replace('"default":{{', '"default":{{"command":"status",', 1))
                    raise SystemExit(0)
                mutation = os.environ.get("FAKE_CAP_MUTATION")
                if mutation:
                    spec = json.loads(mutation)
                    target = value
                    for part in spec["path"][:-1]:
                        target = target[part]
                    key = spec["path"][-1]
                    operation = spec["operation"]
                    if operation == "missing":
                        target.pop(key)
                    elif operation == "wrong-type":
                        target[key] = None
                    elif operation == "wrong-value":
                        current = target[key]
                        if type(current) is bool:
                            target[key] = not current
                        elif type(current) is int:
                            target[key] = current + 1
                        elif type(current) is str:
                            target[key] = current + "-skew"
                        elif type(current) is list:
                            target[key] = ["wrong", *current[1:]]
                        elif type(current) is dict:
                            changed = dict(current)
                            first = next(iter(changed))
                            changed[first] = changed[first] + 1 if type(changed[first]) is int else str(changed[first]) + "-skew"
                            target[key] = changed
                    elif operation == "reordered":
                        target[key] = list(reversed(target[key]))
                    elif operation == "duplicate":
                        target[key] = [*target[key], target[key][0]]
                print(json.dumps(value, sort_keys=True, separators=(",", ":")))
                raise SystemExit(0)

            exit_code = int(os.environ.get("FAKE_EXIT", "0"))
            marker = os.environ.get("FAKE_DELEGATION_MARKER")
            if marker:
                Path(marker).write_text("delegated", encoding="ascii")
            if os.environ.get("FAKE_RAW") == "1":
                sys.stdout.buffer.write(b"stdout-\\xff\\n")
                sys.stderr.buffer.write(b"stderr-\\xfe\\n")
                raise SystemExit(exit_code)
            payload = {{
                "argv": args,
                "cwd": str(Path.cwd()),
                "env": {{
                    key: os.environ.get(key)
                    for key in (
                        "PYTHONDONTWRITEBYTECODE", "GIT_OPTIONAL_LOCKS",
                        "GIT_TERMINAL_PROMPT", "GCM_INTERACTIVE",
                        "SSH_ASKPASS_REQUIRE",
                    )
                }},
                "stdin_b64": base64.b64encode(sys.stdin.buffer.read()).decode("ascii"),
            }}
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            raise SystemExit(exit_code)
            """
        ),
        encoding="utf-8",
    )
    return path


def run_sbp(
    *args: str,
    cwd: Path,
    engine: Path | None,
    input_bytes: bytes = b"",
    extra_env: dict[str, str] | None = None,
    monoserver_root: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    env = {
        **os.environ,
        "SKILLBOX_ROOT": str(ROOT),
        "SKILLBOX_MONOSERVER_ROOT": str(monoserver_root or cwd),
    }
    if engine is not None:
        env["SKILLBOX_REPO_ATLAS_CLI"] = str(engine)
    else:
        env.pop("SKILLBOX_REPO_ATLAS_CLI", None)
    env.update(extra_env or {})
    return subprocess.run(
        [str(SBP), *args],
        cwd=cwd,
        env=env,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def assert_generic_failure(result: subprocess.CompletedProcess[bytes]) -> None:
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"sbp repo: private Repo Atlas engine unavailable or incompatible\n"


@pytest.mark.parametrize("cwd_name", ["CFO checkout", "HTMA", "Sweet Potato café"])
def test_bare_repo_preserves_cwd_stdin_and_safe_environment(tmp_path: Path, cwd_name: str) -> None:
    engine = write_fake_engine(tmp_path / "private engine" / "repo atlas.py")
    cwd = tmp_path / cwd_name
    cwd.mkdir()
    stdin = b"core,backend\nprofile words\n"
    result = run_sbp("repo", cwd=cwd, engine=engine, input_bytes=stdin)
    assert result.returncode == 0 and result.stderr == b""
    payload = json.loads(result.stdout)
    assert payload["argv"] == ["status", "."]
    assert payload["cwd"] == os.path.realpath(cwd)
    assert base64.b64decode(payload["stdin_b64"]) == stdin
    assert payload["env"] == {
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "SSH_ASKPASS_REQUIRE": "never",
    }


def test_repo_forwards_literal_argv_without_profile_or_shell_interpretation(tmp_path: Path) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    cwd = tmp_path / "caller"
    cwd.mkdir()
    args = (
        "repo", "--json", "status", "repo:space value", ";$(touch nope)",
        "café", "core", "local-all",
    )
    result = run_sbp(*args, cwd=cwd, engine=engine)
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout)["argv"] == list(args[1:])
    assert not (cwd / "nope").exists()


def test_explicit_override_never_falls_back_and_never_echoes_hostile_path(tmp_path: Path) -> None:
    write_fake_engine(
        tmp_path / "skills-private" / "reconcile" / "scripts" / "repo_atlas_cli.py"
    )
    hostile = tmp_path / "missing-ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd.py"
    result = run_sbp(
        "repo", "--json", cwd=tmp_path, engine=hostile,
        monoserver_root=tmp_path,
    )
    assert result.returncode == 2 and result.stdout == b""
    assert result.stderr == b"sbp repo: private Repo Atlas engine unavailable or incompatible\n"
    assert str(hostile).encode() not in result.stderr


def test_default_engine_is_resolved_only_under_monoserver_root(tmp_path: Path) -> None:
    engine = write_fake_engine(
        tmp_path / "skills-private" / "reconcile" / "scripts" / "repo_atlas_cli.py"
    )
    result = run_sbp("repo", "list", cwd=tmp_path, engine=None, monoserver_root=tmp_path)
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout)["argv"] == ["list"]
    assert engine.is_file()


@pytest.mark.parametrize(
    "mode",
    ["missing", "malformed", "duplicate", "nonfinite", "skew", "wrong-encoding", "missing-command", "hostile"],
)
def test_missing_malformed_or_skewed_capabilities_fail_generically(
    tmp_path: Path, mode: str
) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    result = run_sbp(
        "repo", "status", ".", cwd=tmp_path, engine=engine,
        extra_env={"FAKE_CAP_MODE": mode},
    )
    assert result.returncode == 2 and result.stdout == b""
    assert result.stderr == b"sbp repo: private Repo Atlas engine unavailable or incompatible\n"
    assert b"ghp_" not in result.stderr


@pytest.mark.parametrize("operation", ["missing", "wrong-type", "wrong-value"])
@pytest.mark.parametrize("path", KNOWN_FIELDS, ids=lambda value: ".".join(value))
def test_every_known_capability_field_is_required_and_type_strict(
    tmp_path: Path, path: tuple[str, ...], operation: str
) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    mutation = json.dumps({"path": path, "operation": operation})
    result = run_sbp(
        "repo", "list", cwd=tmp_path, engine=engine,
        extra_env={"FAKE_CAP_MUTATION": mutation},
    )
    assert_generic_failure(result)


@pytest.mark.parametrize("operation", ["reordered", "duplicate"])
@pytest.mark.parametrize("path", ORDERED_FIELDS, ids=lambda value: ".".join(value))
def test_every_known_ordered_collection_is_exact_and_unique(
    tmp_path: Path, path: tuple[str, ...], operation: str
) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    mutation = json.dumps({"path": path, "operation": operation})
    result = run_sbp(
        "repo", "status", ".", cwd=tmp_path, engine=engine,
        extra_env={"FAKE_CAP_MUTATION": mutation},
    )
    assert_generic_failure(result)


@pytest.mark.parametrize(
    "mode", ["nested-duplicate", "too-deep", "too-many", "oversized"]
)
def test_duplicate_or_unbounded_additive_capabilities_fail_generically(
    tmp_path: Path, mode: str
) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    result = run_sbp(
        "repo", "list", cwd=tmp_path, engine=engine,
        extra_env={"FAKE_CAP_MODE": mode},
    )
    assert_generic_failure(result)


def test_exact_maximum_capability_response_is_admitted(tmp_path: Path) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    direct = subprocess.run(
        [sys.executable, str(engine), "capabilities", "--json"],
        env={**os.environ, "FAKE_CAP_MODE": "exact-max"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    assert len(direct.stdout) == 262_144
    result = run_sbp(
        "repo", "list", cwd=tmp_path, engine=engine,
        extra_env={"FAKE_CAP_MODE": "exact-max"},
    )
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout)["argv"] == ["list"]


def test_maximum_plus_one_is_rejected_before_delegation(tmp_path: Path) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    marker = tmp_path / "must-not-delegate"
    result = run_sbp(
        "repo", "list", cwd=tmp_path, engine=engine,
        extra_env={
            "FAKE_CAP_MODE": "max-plus-one",
            "FAKE_DELEGATION_MARKER": str(marker),
        },
    )
    assert_generic_failure(result)
    assert not marker.exists()


def assert_process_reaped(pid_file: Path) -> None:
    pid = int(pid_file.read_text(encoding="ascii"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    pytest.fail(f"process survived: {pid}")


def test_oversize_then_sleep_rejects_promptly_and_reaps(tmp_path: Path) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    pid_file = tmp_path / "probe.pid"
    marker = tmp_path / "must-not-delegate"
    started = time.monotonic()
    result = run_sbp(
        "repo", "list", cwd=tmp_path, engine=engine,
        extra_env={
            "FAKE_CAP_MODE": "oversize-sleep",
            "FAKE_CAP_PID_FILE": str(pid_file),
            "FAKE_DELEGATION_MARKER": str(marker),
        },
    )
    elapsed = time.monotonic() - started
    assert_generic_failure(result)
    assert elapsed < 3
    assert not marker.exists()
    assert_process_reaped(pid_file)


def test_capability_timeout_terminates_and_reaps_without_echo(tmp_path: Path) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    pid_file = tmp_path / "timeout-ghp_SECRET.pid"
    started = time.monotonic()
    result = run_sbp(
        "repo", "list", cwd=tmp_path, engine=engine,
        extra_env={
            "FAKE_CAP_MODE": "timeout",
            "FAKE_CAP_PID_FILE": str(pid_file),
        },
    )
    elapsed = time.monotonic() - started
    assert_generic_failure(result)
    assert 9 <= elapsed < 13
    assert b"ghp_" not in result.stderr
    assert_process_reaped(pid_file)


@pytest.mark.parametrize(
    "mode", ["group-overflow", "group-timeout", "early-exit", "success-daemon"],
)
def test_capability_process_group_is_reaped_before_rejection(
    tmp_path: Path, mode: str,
) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    leader_file = tmp_path / f"{mode}-leader.pid"
    child_file = tmp_path / f"{mode}-child.pid"
    marker = tmp_path / "must-not-delegate"
    started = time.monotonic()
    result = run_sbp(
        "repo", "list", cwd=tmp_path, engine=engine,
        extra_env={
            "FAKE_CAP_MODE": mode,
            "FAKE_CAP_PID_FILE": str(leader_file),
            "FAKE_CAP_CHILD_PID_FILE": str(child_file),
            "FAKE_DELEGATION_MARKER": str(marker),
        },
    )
    elapsed = time.monotonic() - started
    assert_generic_failure(result)
    if mode == "group-timeout":
        assert 9 <= elapsed < 13
    else:
        assert elapsed < 3
    assert leader_file.exists() and child_file.exists()
    assert_process_reaped(leader_file)
    assert_process_reaped(child_file)
    assert not marker.exists()
    assert b"Traceback" not in result.stderr


def test_capability_preflight_uses_bounded_incremental_pipe_reader() -> None:
    source = SBP.read_text(encoding="utf-8")
    start = source.index("repo_atlas_preflight()")
    end = source.index("\nprint_help()", start)
    preflight = source[start:end]
    assert "subprocess.run(" not in preflight
    assert "subprocess.Popen(" in preflight
    assert "os.read(" in preflight
    assert "select.select(" in preflight
    assert "MAX_CAPABILITY_BYTES + 1 - len(raw)" in preflight
    assert "start_new_session=True" in preflight
    assert "os.killpg(" in preflight
    assert "signal.SIGTERM" in preflight
    assert "signal.SIGKILL" in preflight
    assert "terminate_group_and_reap(process)" in preflight


def test_1500_level_hostile_capability_is_generic_without_echo_or_traceback(
    tmp_path: Path,
) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    result = run_sbp(
        "repo", "list", cwd=tmp_path, engine=engine,
        extra_env={"FAKE_CAP_MODE": "deep-hostile"},
    )
    assert_generic_failure(result)
    assert b"ghp_" not in result.stderr
    assert b"Traceback" not in result.stderr


def test_fake_fixture_is_the_complete_canonical_v1_contract(tmp_path: Path) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    completed = subprocess.run(
        [sys.executable, str(engine), "capabilities", "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    value = json.loads(completed.stdout)
    payload = value["payload"]
    assert value == {
        "encoding": EXPECTED_ENCODING,
        "exit_code": 0,
        "payload": payload,
        "schema_version": "repo-atlas-command/v1",
    }
    assert payload == {
        "schema": "repo-atlas-cli/v1",
        "command_schema_version": "repo-atlas-command/v1",
        "encoding": EXPECTED_ENCODING,
        "commands": EXPECTED_COMMANDS,
        "global_options": EXPECTED_OPTIONS,
        "selectors": EXPECTED_SELECTORS,
        "exit_codes": EXPECTED_EXIT_CODES,
        "default": {"command": "status", "selector": "."},
        "reason_codes": EXPECTED_REASONS,
        "reason_routing_capabilities": EXPECTED_ROUTING_CAPABILITIES,
        "read_only": True,
        "fetch": False,
    }


def test_additive_safe_capability_fields_remain_compatible(tmp_path: Path) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    result = run_sbp(
        "repo", "list", cwd=tmp_path, engine=engine,
        extra_env={"FAKE_CAP_MODE": "additive"},
    )
    assert result.returncode == 0 and json.loads(result.stdout)["argv"] == ["list"]


@pytest.mark.parametrize("exit_code", [0, 1, 2, 3])
def test_engine_stdout_stderr_bytes_and_exit_are_exactly_preserved(
    tmp_path: Path, exit_code: int
) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    result = run_sbp(
        "repo", "status", ".", cwd=tmp_path, engine=engine,
        extra_env={"FAKE_RAW": "1", "FAKE_EXIT": str(exit_code)},
    )
    assert result.returncode == exit_code
    assert result.stdout == b"stdout-\xff\n"
    assert result.stderr == b"stderr-\xfe\n"


def test_top_level_help_and_capabilities_advertise_static_repo_entry(tmp_path: Path) -> None:
    missing = tmp_path / "must-not-run.py"
    help_result = run_sbp("help", cwd=tmp_path, engine=missing)
    assert help_result.returncode == 0
    assert b"sbp repo [args...]" in help_result.stdout
    capabilities = run_sbp("capabilities", "--json", cwd=tmp_path, engine=missing)
    assert capabilities.returncode == 0 and capabilities.stderr == b""
    commands = {entry["name"]: entry for entry in json.loads(capabilities.stdout)["commands"]}
    assert commands["repo"] == {
        "name": "repo", "json": True,
        "safe_first_try": "sbp repo status . --json",
    }


@pytest.mark.parametrize("alias", ["registry", "repos", "repo-registry"])
def test_legacy_registry_aliases_do_not_route_to_repo_atlas(
    tmp_path: Path, alias: str
) -> None:
    engine = write_fake_engine(tmp_path / "repo atlas.py")
    config = tmp_path / "config"
    doctor = config / "scripts" / "registry_doctor.py"
    doctor.parent.mkdir(parents=True)
    doctor.write_text(
        "import json,sys; print(json.dumps({'registry_argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    result = run_sbp(
        alias, "doctor", "--json", cwd=tmp_path, engine=engine,
        extra_env={"SKILLBOX_CONFIG_ROOT": str(config)},
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"registry_argv": ["--json"]}


def test_script_contains_no_repo_observation_or_shell_eval_implementation() -> None:
    source = SBP.read_text(encoding="utf-8")
    start = source.rindex("  repo-atlas)\n")
    repo_case = source[start:source.index("  repo-registry)\n", start)]
    assert "repo_atlas_preflight" in repo_case
    assert "python3 \"${repo_atlas_cli}\" \"${repo_atlas_args[@]}\"" in repo_case
    assert "eval" not in repo_case
    assert "git " not in repo_case
    assert "ssh " not in repo_case
