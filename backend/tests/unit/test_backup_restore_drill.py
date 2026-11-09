"""Safety checks for the disposable synthetic PostgreSQL restore drill."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.ops.backup_restore_drill import (
    BackupRestoreDrillError,
    DatabaseSnapshot,
    DisposableVerifier,
    require_demo_sandbox_environment,
    restore_and_verify_new_database,
    validate_restore_database_name,
)


@dataclass
class FakeVerifier:
    """In-memory adapter proving the restore orchestration never mutates source."""

    source_rows: dict[str, int] = field(default_factory=lambda: {"shipments": 1})
    existing_databases: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_restore: bool = False

    def database_exists(self, database_name: str) -> bool:
        return database_name in self.existing_databases

    def create_database(self, database_name: str) -> None:
        self.calls.append(("create", database_name))
        self.existing_databases.add(database_name)

    def verify_test_database(self, database_name: str) -> None:
        self.calls.append(("verify-target", database_name))

    def restore_archive(self, database_name: str, archive_path: Path) -> None:
        self.calls.append(("restore", database_name))
        if self.fail_restore:
            raise RuntimeError("simulated restore failure")

    def verify_restored_state(self, database_name: str, expected: object) -> None:
        self.calls.append(("verify-restore", database_name))

    def drop_database(self, database_name: str) -> None:
        self.calls.append(("drop", database_name))
        self.existing_databases.remove(database_name)


def safe_environment() -> dict[str, str]:
    return {
        "APP_ENV": "demo",
        "DEMO_SEED_ENABLED": "true",
        "POSTGRES_DB": "labtrack_demo_sandbox",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5433",
        "POSTGRES_USER": "labtrack_owner",
        "POSTGRES_PASSWORD": "synthetic-owner-secret",
        "APP_POSTGRES_USER": "labtrack_app",
        "APP_POSTGRES_PASSWORD": "synthetic-runtime-secret",
    }


def expected_snapshot() -> DatabaseSnapshot:
    return DatabaseSnapshot("0011_http_request_scope", {}, 1, 0, True, 0)


def test_restore_destination_must_be_a_generated_test_database() -> None:
    validate_restore_database_name("labtrack_restore_test_0123456789ab")

    for database_name in (
        "postgres",
        "labtrack",
        "labtrack_demo_sandbox",
        "production",
        "labtrack_restore_test_not_a_hex_id",
    ):
        with pytest.raises(BackupRestoreDrillError):
            validate_restore_database_name(database_name)


def test_disposable_restore_succeeds_and_always_drops_its_target() -> None:
    verifier = FakeVerifier()
    target_name = "labtrack_restore_test_0123456789ab"

    restore_and_verify_new_database(
        verifier, target_name, Path("synthetic.dump"), expected_snapshot()
    )

    assert verifier.calls == [
        ("create", target_name),
        ("verify-target", target_name),
        ("restore", target_name),
        ("verify-restore", target_name),
        ("drop", target_name),
    ]
    assert verifier.existing_databases == set()
    assert verifier.source_rows == {"shipments": 1}


def test_existing_restore_target_is_refused_without_overwrite_or_cleanup() -> None:
    target_name = "labtrack_restore_test_0123456789ab"
    verifier = FakeVerifier(existing_databases={target_name})

    with pytest.raises(BackupRestoreDrillError, match="already exists"):
        restore_and_verify_new_database(
            verifier, target_name, Path("synthetic.dump"), expected_snapshot()
        )

    assert verifier.calls == []
    assert verifier.existing_databases == {target_name}


def test_simulated_restore_failure_cleans_target_and_preserves_source() -> None:
    target_name = "labtrack_restore_test_0123456789ab"
    verifier = FakeVerifier(fail_restore=True)
    source_snapshot = verifier.source_rows.copy()

    with pytest.raises(RuntimeError, match="simulated restore failure"):
        restore_and_verify_new_database(
            verifier, target_name, Path("synthetic.dump"), expected_snapshot()
        )

    assert verifier.calls[-1] == ("drop", target_name)
    assert verifier.existing_databases == set()
    assert verifier.source_rows == source_snapshot


@pytest.mark.parametrize(
    "overrides",
    [
        {"APP_ENV": "production"},
        {"DEMO_SEED_ENABLED": "false"},
        {"POSTGRES_DB": "labtrack"},
        {"POSTGRES_DB": "labtrack_prod"},
        {"POSTGRES_HOST": "198.51.100.20"},
        {"POSTGRES_HOST": "/var/run/postgresql"},
    ],
)
def test_environment_guard_rejects_default_production_or_remote_database(
    overrides: dict[str, str],
) -> None:
    values = safe_environment() | overrides

    with pytest.raises(BackupRestoreDrillError):
        require_demo_sandbox_environment(values)


def test_environment_guard_accepts_only_the_local_opted_in_sandbox() -> None:
    require_demo_sandbox_environment(safe_environment())


def test_runtime_role_password_is_sent_over_stdin_not_command_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-runtime-secret"
    verifier = DisposableVerifier(
        {"APP_POSTGRES_USER": "labtrack_app", "APP_POSTGRES_PASSWORD": secret}
    )
    captured: dict[str, object] = {}

    def capture_command(command: list[str], **kwargs: object) -> bytes:
        captured["command"] = command
        captured["input"] = kwargs.get("input_bytes")
        return b""

    monkeypatch.setattr("app.ops.backup_restore_drill._run", capture_command)
    verifier._create_runtime_role("labtrack_restore_test_0123456789ab")

    command = captured["command"]
    assert isinstance(command, list)
    assert secret not in " ".join(command)
    assert captured["input"] == f"{secret}\n".encode()


def test_container_cleanup_fails_closed_when_docker_daemon_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = DisposableVerifier({})
    verifier.started = True
    commands: list[list[str]] = []

    def unavailable_docker(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"daemon unavailable")

    monkeypatch.setattr("app.ops.backup_restore_drill.subprocess.run", unavailable_docker)

    with pytest.raises(BackupRestoreDrillError, match="Could not verify"):
        verifier.close()

    assert len(commands) == 2
    assert commands[1][1:3] == ["ps", "--all"]
    assert verifier.started
