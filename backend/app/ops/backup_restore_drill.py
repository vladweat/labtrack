"""Back up and restore only the local, isolated LabTrack synthetic demo sandbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
COMPOSE_FILE = REPOSITORY_ROOT / "compose.yaml"
SANDBOX_ENV_FILE = REPOSITORY_ROOT / ".env.demo.sandbox"
RUNTIME_ROLE_SCRIPT = BACKEND_ROOT / "database" / "init" / "10-runtime-role.sh"
COMPOSE_PROJECT = "labtrack-demo-sandbox"
SOURCE_DATABASE = "labtrack_demo_sandbox"
SOURCE_VOLUME = "labtrack_demo_sandbox_postgres_data"
POSTGRES_IMAGE = "postgres:15"
RESTORE_DATABASE_PREFIX = "labtrack_restore_test_"
RESTORE_DATABASE_PATTERN = re.compile(r"^labtrack_restore_test_[0-9a-f]{12}$")
ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TABLES_TO_COMPARE = (
    "regions",
    "organizations",
    "users",
    "samples",
    "shipments",
    "transport_containers",
    "container_items",
    "handoffs",
    "audit_events",
)


class BackupRestoreDrillError(RuntimeError):
    """An operational error safe to print without command output or database rows."""


@dataclass(frozen=True)
class DatabaseSnapshot:
    schema_version: str
    table_counts: dict[str, int]
    workflow_rows: int
    nonsynthetic_users: int
    has_demo_sender: bool
    runtime_connections: int


class RestoreTarget(Protocol):
    def database_exists(self, database_name: str) -> bool: ...

    def create_database(self, database_name: str) -> None: ...

    def verify_test_database(self, database_name: str) -> None: ...

    def restore_archive(self, database_name: str, archive_path: Path) -> None: ...

    def verify_restored_state(self, database_name: str, expected: DatabaseSnapshot) -> None: ...

    def drop_database(self, database_name: str) -> None: ...


def validate_restore_database_name(database_name: str) -> None:
    """Allow only the generated name shape for a disposable restore target."""

    if not RESTORE_DATABASE_PATTERN.fullmatch(database_name):
        raise BackupRestoreDrillError(
            "Restore target must be labtrack_restore_test_<12 lowercase hex>."
        )


def require_demo_sandbox_environment(environment: dict[str, str]) -> None:
    """Reject defaults, production names, remote hosts, and non-opted-in sources."""

    required = (
        "APP_ENV",
        "DEMO_SEED_ENABLED",
        "POSTGRES_DB",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "APP_POSTGRES_USER",
        "APP_POSTGRES_PASSWORD",
    )
    missing = [key for key in required if not environment.get(key)]
    if missing:
        raise BackupRestoreDrillError("Missing sandbox environment keys: " + ", ".join(missing))
    if environment["APP_ENV"].casefold() != "demo":
        raise BackupRestoreDrillError("APP_ENV must be demo.")
    if environment["DEMO_SEED_ENABLED"].casefold() != "true":
        raise BackupRestoreDrillError("DEMO_SEED_ENABLED must be true.")
    if environment["POSTGRES_DB"] != SOURCE_DATABASE:
        raise BackupRestoreDrillError(f"Source must be the {SOURCE_DATABASE} database.")
    if environment["POSTGRES_HOST"] not in {"127.0.0.1", "localhost", "::1"}:
        raise BackupRestoreDrillError("Source PostgreSQL host must be loopback.")
    try:
        port = int(environment["POSTGRES_PORT"])
    except ValueError:
        raise BackupRestoreDrillError("POSTGRES_PORT must be a local TCP port.") from None
    if not 1 <= port <= 65535:
        raise BackupRestoreDrillError("POSTGRES_PORT must be a local TCP port.")
    for key in ("POSTGRES_USER", "APP_POSTGRES_USER"):
        if not ROLE_PATTERN.fullmatch(environment[key]):
            raise BackupRestoreDrillError(f"{key} must be a simple PostgreSQL role name.")
    if environment["POSTGRES_USER"] == environment["APP_POSTGRES_USER"]:
        raise BackupRestoreDrillError("Owner and runtime roles must be different.")
    if environment["POSTGRES_PASSWORD"] == environment["APP_POSTGRES_PASSWORD"]:
        raise BackupRestoreDrillError("Owner and runtime credentials must be distinct.")


def read_private_environment(path: Path = SANDBOX_ENV_FILE) -> dict[str, str]:
    """Read the documented KEY=value file without evaluating it as shell code."""

    if path.is_symlink() or not path.is_file():
        raise BackupRestoreDrillError("Private .env.demo.sandbox file is required.")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise BackupRestoreDrillError(".env.demo.sandbox must have mode 0600 or stricter.")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise BackupRestoreDrillError("Could not read .env.demo.sandbox.") from None
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or "\x00" in value:
            raise BackupRestoreDrillError(f"Unsupported sandbox env syntax on line {number}.")
        if key in values:
            raise BackupRestoreDrillError(f"Duplicate sandbox environment key {key}.")
        values[key] = value
    return values


def _run(
    command: list[str],
    *,
    label: str,
    input_bytes: bytes | None = None,
    timeout: int = 60,
) -> bytes:
    try:
        result = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = "timed out" if isinstance(exc, subprocess.TimeoutExpired) else "unavailable"
        raise BackupRestoreDrillError(f"{label} {detail}.") from None
    if result.returncode:
        raise BackupRestoreDrillError(f"{label} failed; command output was suppressed.")
    return result.stdout


def _compose(env_file: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        COMPOSE_PROJECT,
        "--env-file",
        str(env_file),
        "--file",
        str(COMPOSE_FILE),
    ]


def _verify_source_container(env_file: Path, environment: dict[str, str]) -> str:
    if not COMPOSE_FILE.is_file():
        raise BackupRestoreDrillError("Repository compose.yaml is required.")
    compose = _compose(env_file)
    raw_ids = _run([*compose, "ps", "--quiet", "postgres"], label="Compose target check")
    container_ids = [line for line in raw_ids.decode("ascii").splitlines() if line]
    if len(container_ids) != 1 or not re.fullmatch(r"[0-9a-fA-F]{12,64}", container_ids[0]):
        raise BackupRestoreDrillError("Expected one PostgreSQL container in the sandbox project.")
    running = _run([*compose, "ps", "--quiet"], label="Sandbox service check")
    if {line for line in running.decode("ascii").splitlines() if line} != {container_ids[0]}:
        raise BackupRestoreDrillError(
            "Stop sandbox API/worker services; PostgreSQL must be the only running Compose service."
        )

    inspected = json.loads(_run(["docker", "inspect", container_ids[0]], label="Container check"))
    if len(inspected) != 1:
        raise BackupRestoreDrillError("Could not verify the local PostgreSQL container.")
    container = inspected[0]
    labels = container.get("Config", {}).get("Labels") or {}
    if (
        labels.get("com.docker.compose.project") != COMPOSE_PROJECT
        or labels.get("com.docker.compose.service") != "postgres"
        or container.get("Config", {}).get("Image") != POSTGRES_IMAGE
        or not container.get("State", {}).get("Running")
    ):
        raise BackupRestoreDrillError("Selected container is not the running synthetic sandbox DB.")

    configured = dict(
        item.partition("=")[::2]
        for item in container.get("Config", {}).get("Env", [])
        if "=" in item
    )
    for key in (
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "APP_POSTGRES_USER",
        "APP_POSTGRES_PASSWORD",
    ):
        if configured.get(key) != environment.get(key):
            raise BackupRestoreDrillError(
                "Container configuration differs from the private sandbox env."
            )

    bindings = container.get("HostConfig", {}).get("PortBindings") or {}
    postgres_bindings = bindings.get("5432/tcp") or []
    if (
        set(bindings) != {"5432/tcp"}
        or len(postgres_bindings) != 1
        or postgres_bindings[0].get("HostIp") != "127.0.0.1"
        or postgres_bindings[0].get("HostPort") != environment["POSTGRES_PORT"]
    ):
        raise BackupRestoreDrillError(
            "Sandbox PostgreSQL must bind only to its configured loopback port."
        )

    mounts = container.get("Mounts", [])
    data_mounts = [m for m in mounts if m.get("Destination") == "/var/lib/postgresql/data"]
    init_mounts = [m for m in mounts if m.get("Destination") == "/docker-entrypoint-initdb.d"]
    if (
        len(mounts) != 2
        or len(data_mounts) != 1
        or data_mounts[0].get("Type") != "volume"
        or data_mounts[0].get("Name") != SOURCE_VOLUME
        or not data_mounts[0].get("RW")
        or len(init_mounts) != 1
        or init_mounts[0].get("Type") != "bind"
        or init_mounts[0].get("RW")
    ):
        raise BackupRestoreDrillError(
            "Sandbox PostgreSQL volume layout does not match the isolated setup."
        )
    init_path = Path(init_mounts[0].get("Source", "")).resolve()
    if init_path != (BACKEND_ROOT / "database" / "init").resolve():
        raise BackupRestoreDrillError("Unexpected database initialization mount in the sandbox.")

    networks = container.get("NetworkSettings", {}).get("Networks") or {}
    if len(networks) != 1:
        raise BackupRestoreDrillError("Sandbox PostgreSQL must use one internal project network.")
    network_name = next(iter(networks))
    network_info = json.loads(
        _run(["docker", "network", "inspect", network_name], label="Network check")
    )
    if len(network_info) != 1:
        raise BackupRestoreDrillError("Could not verify the sandbox network.")
    network = network_info[0]
    network_labels = network.get("Labels") or {}
    if (
        network.get("Internal") is not True
        or network_labels.get("com.docker.compose.project") != COMPOSE_PROJECT
    ):
        raise BackupRestoreDrillError("Sandbox PostgreSQL network is not isolated.")
    return container_ids[0]


def _snapshot_query(runtime_role: str) -> str:
    if not ROLE_PATTERN.fullmatch(runtime_role):
        raise BackupRestoreDrillError("Runtime PostgreSQL role name is invalid.")
    table_pairs = ", ".join(
        f"'{table}', (SELECT count(*) FROM public.\"{table}\")" for table in TABLES_TO_COMPARE
    )
    return f"""
        SELECT json_build_object(
            'schema_version', (SELECT version_num FROM alembic_version),
            'table_counts', json_build_object({table_pairs}),
            'workflow_rows', (
                SELECT count(*) FROM shipments s
                JOIN transport_containers c ON c.shipment_id = s.id
            ),
            'nonsynthetic_users', (
                SELECT count(*) FROM users WHERE email NOT LIKE '%@%.example.test'
            ),
            'has_demo_sender', EXISTS (
                SELECT 1 FROM organizations WHERE code = 'DEMO_SENDER'
            ),
            'runtime_connections', (
                SELECT count(*) FROM pg_stat_activity
                WHERE datname = current_database() AND pid <> pg_backend_pid()
                  AND usename = '{runtime_role}'
            )
        )::text
    """


def _parse_snapshot(output: bytes, expected_head: str) -> DatabaseSnapshot:
    try:
        raw = json.loads(output.decode("utf-8").strip())
        counts = {name: int(raw["table_counts"][name]) for name in TABLES_TO_COMPARE}
        snapshot = DatabaseSnapshot(
            schema_version=str(raw["schema_version"]),
            table_counts=counts,
            workflow_rows=int(raw["workflow_rows"]),
            nonsynthetic_users=int(raw["nonsynthetic_users"]),
            has_demo_sender=bool(raw["has_demo_sender"]),
            runtime_connections=int(raw["runtime_connections"]),
        )
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise BackupRestoreDrillError(
            "Database snapshot verification returned invalid data."
        ) from None
    if (
        snapshot.schema_version != expected_head
        or snapshot.nonsynthetic_users != 0
        or not snapshot.has_demo_sender
        or snapshot.workflow_rows <= 0
        or snapshot.runtime_connections != 0
    ):
        raise BackupRestoreDrillError(
            "Stop runtime writers; source must be at the current schema head "
            "with the seeded synthetic workflow."
        )
    return snapshot


def _source_snapshot(env_file: Path, expected_head: str, runtime_role: str) -> DatabaseSnapshot:
    query = _snapshot_query(runtime_role)
    script = (
        'PGPASSWORD="$POSTGRES_PASSWORD" exec psql --no-password --host=127.0.0.1 '
        '--username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-psqlrc '
        '--tuples-only --no-align --command "$1"'
    )
    result = _run(
        [*_compose(env_file), "exec", "-T", "postgres", "sh", "-ec", script, "sh", query],
        label="Read-only source snapshot",
    )
    return _parse_snapshot(result, expected_head)


def _write_backup(container_id: str, archive_path: Path) -> None:
    compose = _compose(SANDBOX_ENV_FILE)
    current = (
        _run([*compose, "ps", "--quiet", "postgres"], label="Sandbox identity recheck")
        .decode("ascii")
        .strip()
    )
    if current != container_id:
        raise BackupRestoreDrillError("Sandbox PostgreSQL changed during preflight.")
    script = (
        'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump --no-password --host=127.0.0.1 '
        '--format=custom --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'
    )
    command = [*_compose(SANDBOX_ENV_FILE), "exec", "-T", "postgres", "sh", "-ec", script]
    with tempfile.TemporaryFile() as error_file:
        try:
            with archive_path.open("wb") as archive:
                process = subprocess.Popen(command, stdout=archive, stderr=error_file)
                try:
                    result = process.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise BackupRestoreDrillError(
                        "pg_dump timed out; incomplete output was removed."
                    ) from None
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
                archive.flush()
                os.fsync(archive.fileno())
        except OSError:
            raise BackupRestoreDrillError("Could not write the private backup archive.") from None
    if result or archive_path.stat().st_size == 0:
        raise BackupRestoreDrillError("pg_dump failed; incomplete output was removed.")


class DisposableVerifier:
    """A new no-network PostgreSQL container with data on temporary storage."""

    def __init__(self, environment: dict[str, str]) -> None:
        self.environment = environment
        self.name = f"labtrack-restore-drill-{uuid.uuid4().hex}"
        self.started = False

    def start(self) -> None:
        # docker run may create the container before its CLI times out or loses
        # the daemon response, so cleanup must be armed before starting it.
        self.started = True
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                self.name,
                "--label",
                "com.labtrack.backup-restore-drill=true",
                "--pull=never",
                "--network",
                "none",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,noexec,nosuid,size=1g",
                "--volume",
                f"{RUNTIME_ROLE_SCRIPT}:/tmp/10-runtime-role.sh:ro",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                "POSTGRES_USER=restore_owner",
                "--env",
                "POSTGRES_DB=postgres",
                POSTGRES_IMAGE,
            ],
            label="Disposable PostgreSQL startup",
            timeout=120,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    self.name,
                    "pg_isready",
                    "-U",
                    "restore_owner",
                    "-d",
                    "postgres",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            if result.returncode == 0:
                return
            time.sleep(0.25)
        raise BackupRestoreDrillError("Disposable PostgreSQL did not become ready.")

    def close(self) -> None:
        if not self.started:
            return
        result = subprocess.run(
            ["docker", "rm", "--force", self.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        if result.returncode:
            remains = subprocess.run(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    f"name=^{self.name}$",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            if remains.returncode:
                raise BackupRestoreDrillError(
                    "Could not verify that the disposable restore container was removed."
                )
            if remains.stdout.strip():
                raise BackupRestoreDrillError("Could not remove the disposable restore container.")
        self.started = False

    def _psql(self, database_name: str, query: str) -> str:
        output = _run(
            [
                "docker",
                "exec",
                self.name,
                "psql",
                "--no-psqlrc",
                "--set=ON_ERROR_STOP=1",
                "--tuples-only",
                "--no-align",
                "--username=restore_owner",
                f"--dbname={database_name}",
                "--command",
                query,
            ],
            label="Restore database query",
        )
        return output.decode("utf-8").strip()

    def database_exists(self, database_name: str) -> bool:
        return (
            self._psql(
                "postgres",
                f"SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = '{database_name}')",
            )
            == "t"
        )

    def create_database(self, database_name: str) -> None:
        validate_restore_database_name(database_name)
        _run(
            [
                "docker",
                "exec",
                self.name,
                "createdb",
                "--username=restore_owner",
                "--owner=restore_owner",
                "--maintenance-db=postgres",
                database_name,
            ],
            label="Restore test database creation",
        )

    def verify_test_database(self, database_name: str) -> None:
        validate_restore_database_name(database_name)
        details = self._psql(
            "postgres",
            "SELECT datname || '|' || datistemplate || '|' || datallowconn || '|' || "
            "pg_get_userbyid(datdba) FROM pg_database WHERE datname = "
            f"'{database_name}'",
        )
        if details != f"{database_name}|false|true|restore_owner":
            raise BackupRestoreDrillError("Restore target is not a newly created test database.")

    def restore_archive(self, database_name: str, archive_path: Path) -> None:
        validate_restore_database_name(database_name)
        command = [
            "docker",
            "exec",
            "--interactive",
            self.name,
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "--username=restore_owner",
            f"--dbname={database_name}",
        ]
        with tempfile.TemporaryFile() as error_file:
            try:
                with archive_path.open("rb") as archive:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=error_file,
                    )
                    try:
                        assert process.stdin is not None
                        shutil.copyfileobj(archive, process.stdin, length=1024 * 1024)
                        process.stdin.close()
                        result = process.wait(timeout=300)
                    except (BrokenPipeError, OSError):
                        process.kill()
                        process.wait()
                        raise BackupRestoreDrillError(
                            "pg_restore failed; restore output was suppressed."
                        ) from None
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        raise BackupRestoreDrillError(
                            "pg_restore timed out; restore output was suppressed."
                        ) from None
                    except BaseException:
                        process.kill()
                        process.wait()
                        raise
            except OSError:
                raise BackupRestoreDrillError(
                    "Could not read the private backup archive."
                ) from None
        if result:
            raise BackupRestoreDrillError("pg_restore failed; restore output was suppressed.")

    def verify_restored_state(self, database_name: str, expected: DatabaseSnapshot) -> None:
        self._create_runtime_role(database_name)
        restored = _parse_snapshot(
            self._psql(
                database_name, _snapshot_query(self.environment["APP_POSTGRES_USER"])
            ).encode("utf-8"),
            expected.schema_version,
        )
        if restored != expected:
            raise BackupRestoreDrillError(
                "Restored schema version, selected table counts or invariants differ from source."
            )
        self._verify_runtime_privileges(database_name)
        self._enable_runtime_password_authentication()
        self._read_only_workflow_check(database_name)

    def _create_runtime_role(self, database_name: str) -> None:
        command = [
            "docker",
            "exec",
            "--interactive",
            "--env",
            f"POSTGRES_DB={database_name}",
            "--env",
            "POSTGRES_USER=restore_owner",
            "--env",
            f"APP_POSTGRES_USER={self.environment['APP_POSTGRES_USER']}",
            self.name,
            "sh",
            "-ec",
            "IFS= read -r APP_POSTGRES_PASSWORD || exit 1; "
            "export APP_POSTGRES_PASSWORD; exec sh /tmp/10-runtime-role.sh",
        ]
        # No secret is included in argv: the child reads the password from stdin.
        _run(
            command,
            label="Runtime-role setup in the restore target",
            input_bytes=(self.environment["APP_POSTGRES_PASSWORD"] + "\n").encode(),
        )

    def _verify_runtime_privileges(self, database_name: str) -> None:
        role = self.environment["APP_POSTGRES_USER"]
        if not ROLE_PATTERN.fullmatch(role):
            raise BackupRestoreDrillError("Runtime PostgreSQL role name is invalid.")
        query = f"""
            SELECT json_build_object(
                'login', r.rolcanlogin,
                'superuser', r.rolsuper,
                'create_role', r.rolcreaterole,
                'create_database', r.rolcreatedb,
                'replication', r.rolreplication,
                'bypass_rls', r.rolbypassrls,
                'database_owner', pg_get_userbyid(d.datdba) = r.rolname,
                'member_of_owner', pg_has_role(r.rolname, pg_get_userbyid(d.datdba), 'MEMBER'),
                'connect', has_database_privilege(r.rolname, d.datname, 'CONNECT'),
                'database_create', has_database_privilege(r.rolname, d.datname, 'CREATE'),
                'schema_usage', has_schema_privilege(r.rolname, 'public', 'USAGE'),
                'schema_create', has_schema_privilege(r.rolname, 'public', 'CREATE'),
                'select', has_table_privilege(r.rolname, 'shipments', 'SELECT'),
                'insert', has_table_privilege(r.rolname, 'shipments', 'INSERT'),
                'update', has_table_privilege(r.rolname, 'shipments', 'UPDATE'),
                'delete', has_table_privilege(r.rolname, 'shipments', 'DELETE'),
                'truncate', has_table_privilege(r.rolname, 'shipments', 'TRUNCATE'),
                'trigger', has_table_privilege(r.rolname, 'shipments', 'TRIGGER')
            )::text
            FROM pg_roles r CROSS JOIN pg_database d
            WHERE r.rolname = '{role}' AND d.datname = current_database()
        """
        try:
            privileges = json.loads(self._psql(database_name, query))
        except (json.JSONDecodeError, TypeError):
            raise BackupRestoreDrillError(
                "Runtime privilege check returned invalid data."
            ) from None
        allowed = all(privileges.get(key) for key in ("login", "connect", "schema_usage", "select"))
        forbidden = any(
            privileges.get(key)
            for key in (
                "superuser",
                "create_role",
                "create_database",
                "replication",
                "bypass_rls",
                "database_owner",
                "member_of_owner",
                "database_create",
                "schema_create",
                "truncate",
                "trigger",
            )
        )
        if (
            not allowed
            or forbidden
            or not all(privileges.get(key) for key in ("insert", "update", "delete"))
        ):
            raise BackupRestoreDrillError(
                "Restored runtime role privileges are not restricted as expected."
            )

    def _enable_runtime_password_authentication(self) -> None:
        role = self.environment["APP_POSTGRES_USER"]
        if not ROLE_PATTERN.fullmatch(role):
            raise BackupRestoreDrillError("Runtime PostgreSQL role name is invalid.")
        script = (
            'set -eu; file="$PGDATA/pg_hba.conf"; '
            "count=$(grep -Ec "
            "'^host[[:space:]]+all[[:space:]]+all[[:space:]]+all[[:space:]]+trust$' "
            '"$file" || true); '
            '[ "$count" = 1 ]; '
            "sed -i '/^host[[:space:]]\\+all[[:space:]]\\+all[[:space:]]\\+all"
            "[[:space:]]\\+trust$/i "
            f'host all {role} 127.0.0.1/32 scram-sha-256\' "$file"; '
            'pg_ctl -D "$PGDATA" reload >/dev/null'
        )
        _run(
            ["docker", "exec", "--user", "postgres", self.name, "sh", "-ec", script],
            label="Runtime-role password authentication setup",
        )

    def _read_only_workflow_check(self, database_name: str) -> None:
        role = self.environment["APP_POSTGRES_USER"]
        query = (
            "BEGIN READ ONLY; "
            "SELECT current_user || '|' || count(*) FROM shipments s "
            "JOIN transport_containers c ON c.shipment_id = s.id; COMMIT"
        )
        script = (
            "IFS= read -r PGPASSWORD || exit 1; export PGPASSWORD; "
            "exec psql --no-password --host=127.0.0.1 --no-psqlrc "
            '--quiet --tuples-only --no-align --username="$1" --dbname="$2" '
            '--command="$3"'
        )
        output = (
            _run(
                [
                    "docker",
                    "exec",
                    "--interactive",
                    self.name,
                    "sh",
                    "-ec",
                    script,
                    "sh",
                    role,
                    database_name,
                    query,
                ],
                label="Runtime-role read-only workflow check",
                input_bytes=(self.environment["APP_POSTGRES_PASSWORD"] + "\n").encode(),
            )
            .decode("utf-8")
            .strip()
        )
        try:
            user, count = output.split("|", maxsplit=1)
        except ValueError:
            raise BackupRestoreDrillError(
                "Read-only workflow check returned invalid data."
            ) from None
        if user != role or not count.isdigit() or int(count) <= 0:
            raise BackupRestoreDrillError(
                "Runtime role could not read a restored shipment workflow."
            )

    def drop_database(self, database_name: str) -> None:
        validate_restore_database_name(database_name)
        self.verify_test_database(database_name)
        _run(
            [
                "docker",
                "exec",
                self.name,
                "dropdb",
                "--force",
                "--if-exists",
                "--username=restore_owner",
                "--maintenance-db=postgres",
                database_name,
            ],
            label="Restore target cleanup",
        )


def restore_and_verify_new_database(
    target: RestoreTarget,
    database_name: str,
    archive_path: Path,
    expected: DatabaseSnapshot,
) -> None:
    """Never overwrite a database; dispose of the new target in every outcome."""

    validate_restore_database_name(database_name)
    if target.database_exists(database_name):
        raise BackupRestoreDrillError("Restore target already exists; refusing to overwrite it.")
    created = False
    try:
        target.create_database(database_name)
        created = True
        target.verify_test_database(database_name)
        target.restore_archive(database_name, archive_path)
        target.verify_restored_state(database_name, expected)
    finally:
        if created:
            target.drop_database(database_name)


def _private_archive_paths(requested: Path) -> tuple[Path, Path]:
    archive = requested.expanduser().absolute()
    parent = archive.parent
    if not parent.is_dir() or parent.is_symlink():
        raise BackupRestoreDrillError("Archive directory must exist and must not be a symlink.")
    if stat.S_IMODE(parent.stat().st_mode) & 0o077:
        raise BackupRestoreDrillError("Archive directory must be private (mode 0700 or stricter).")
    checksum = Path(f"{archive}.sha256")
    if any(path.exists() or path.is_symlink() for path in (archive, checksum)):
        raise BackupRestoreDrillError("Archive or checksum exists; refusing to overwrite it.")
    return archive, checksum


def _publish(temporary: Path, destination: Path) -> None:
    try:
        os.link(temporary, destination)
        temporary.unlink()
    except OSError:
        raise BackupRestoreDrillError("Could not publish a complete private backup file.") from None


def _current_alembic_head() -> str:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        heads = ScriptDirectory.from_config(config).get_heads()
    except Exception as exc:
        raise BackupRestoreDrillError(
            f"Could not inspect migrations ({type(exc).__name__})."
        ) from None
    if len(heads) != 1:
        raise BackupRestoreDrillError("Backup drill requires one Alembic schema head.")
    return heads[0]


def run_drill(requested_archive: Path, requested_target: str | None) -> None:
    environment = read_private_environment()
    require_demo_sandbox_environment(environment)
    if "JWT_SECRET_KEY" not in environment or len(environment["JWT_SECRET_KEY"]) < 32:
        raise BackupRestoreDrillError(
            "A private JWT_SECRET_KEY of at least 32 characters is required."
        )
    target_name = requested_target or (RESTORE_DATABASE_PREFIX + uuid.uuid4().hex[:12])
    validate_restore_database_name(target_name)
    archive_path, checksum_path = _private_archive_paths(requested_archive)
    source_container_id = _verify_source_container(SANDBOX_ENV_FILE, environment)
    schema_head = _current_alembic_head()
    runtime_role = environment["APP_POSTGRES_USER"]
    source_before = _source_snapshot(SANDBOX_ENV_FILE, schema_head, runtime_role)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive_path.name}.tmp.", dir=archive_path.parent
    )
    os.chmod(temporary_name, 0o600)
    os.close(descriptor)
    temporary_archive = Path(temporary_name)
    temporary_checksum: Path | None = None
    archive_published = False
    checksum_published = False
    verifier = DisposableVerifier(environment)
    try:
        _write_backup(source_container_id, temporary_archive)
        source_after = _source_snapshot(SANDBOX_ENV_FILE, schema_head, runtime_role)
        if source_after != source_before:
            raise BackupRestoreDrillError(
                "Sandbox data changed during pg_dump; stop writers and retry the drill."
            )

        digest = hashlib.sha256()
        with temporary_archive.open("rb") as archive:
            for chunk in iter(lambda: archive.read(1024 * 1024), b""):
                digest.update(chunk)
        checksum_value = digest.hexdigest()

        verifier.start()
        restore_and_verify_new_database(verifier, target_name, temporary_archive, source_before)
        verifier.close()

        checksum_fd, temporary_checksum_name = tempfile.mkstemp(
            prefix=f".{checksum_path.name}.tmp.", dir=checksum_path.parent
        )
        temporary_checksum = Path(temporary_checksum_name)
        with os.fdopen(checksum_fd, "w", encoding="ascii") as checksum_file:
            checksum_file.write(f"{checksum_value}  {archive_path.name}\n")
            checksum_file.flush()
            os.fsync(checksum_file.fileno())
        _publish(temporary_archive, archive_path)
        archive_published = True
        _publish(temporary_checksum, checksum_path)
        temporary_checksum = None
        checksum_published = True
    finally:
        cleanup_error: Exception | None = None
        if verifier.started:
            try:
                verifier.close()
            except Exception as exc:
                cleanup_error = exc
        if temporary_archive.exists():
            temporary_archive.unlink()
        if temporary_checksum is not None and temporary_checksum.exists():
            temporary_checksum.unlink()
        if not checksum_published and archive_published and archive_path.exists():
            archive_path.unlink()
        if cleanup_error:
            if archive_published and archive_path.exists():
                archive_path.unlink()
            if checksum_published and checksum_path.exists():
                checksum_path.unlink()
            raise cleanup_error

    print(f"Backup archive: {archive_path}")
    print(f"SHA-256 sidecar: {checksum_path}")
    print(f"Schema version: {source_before.schema_version}")
    print("Selected synthetic table counts:")
    for table, count in source_before.table_counts.items():
        print(f"  {table}: {count}")
    print(f"Shipment/container workflow rows: {source_before.workflow_rows}")
    print("Runtime-role privilege restrictions: verified")
    print("Read-only workflow query: verified")
    print("Disposable restore target: removed")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Back up and restore only the local LabTrack synthetic demo sandbox."
    )
    parser.add_argument(
        "--archive",
        type=Path,
        required=True,
        help="new output path in an existing private directory; existing files are refused",
    )
    parser.add_argument(
        "--restore-database",
        help="optional target name matching labtrack_restore_test_<12 lowercase hex>",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        run_drill(args.archive, args.restore_database)
    except BackupRestoreDrillError as exc:
        print(f"Backup/restore drill failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Backup/restore drill interrupted; temporary resources were removed.", file=sys.stderr
        )
        return 130
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
