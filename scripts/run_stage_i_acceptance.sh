#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
backend_dir="$repo_root/backend"
web_dir="$repo_root/web"
database_name="labtrack_stagei_test"
postgres_image="postgres:15"
web_node_modules="${LABTRACK_WEB_NODE_MODULES:-$web_dir/node_modules}"

for command_name in docker python3 setsid; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Local Stage I acceptance requires %s.\n' "$command_name" >&2
    exit 1
  fi
done
if [[ ! -x "$web_node_modules/.bin/playwright" ]]; then
  printf 'Set LABTRACK_WEB_NODE_MODULES to an installed web/node_modules directory, or install web dependencies first.\n' >&2
  exit 1
fi

python_bin="${LABTRACK_PYTHON_BIN:-}"
if [[ -z "$python_bin" && -x "$backend_dir/.venv/bin/python" ]]; then
  python_bin="$backend_dir/.venv/bin/python"
fi
if [[ -z "$python_bin" ]]; then
  python_bin="$(command -v python3)"
fi
if [[ ! -x "$python_bin" ]]; then
  printf 'LABTRACK_PYTHON_BIN must point to an executable Python interpreter.\n' >&2
  exit 1
fi

image_id="$(docker image inspect --format '{{.Id}}' "$postgres_image" 2>/dev/null || true)"
if [[ ! "$image_id" =~ ^sha256:[[:xdigit:]]{64}$ ]]; then
  printf 'The cached postgres:15 image is required; this runner never pulls images.\n' >&2
  exit 1
fi
if [[ "$database_name" != labtrack_*test* ]]; then
  printf 'Refusing an acceptance database name outside the explicit local test target.\n' >&2
  exit 1
fi

port_pair="$(python3 - <<'PY'
import socket

sockets = []
try:
    for _ in range(2):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        sockets.append(server)
    print(sockets[0].getsockname()[1], sockets[1].getsockname()[1])
finally:
    for server in sockets:
        server.close()
PY
)"
read -r api_port web_port <<< "$port_pair"
if [[ -z "${api_port:-}" || -z "${web_port:-}" || "$api_port" == "$web_port" ]]; then
  printf 'Could not allocate two distinct loopback-only application ports.\n' >&2
  exit 1
fi

run_tag="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
container_name="labtrack-stagei-acceptance-$run_tag"
work_dir="$(mktemp -d /tmp/labtrack-stage-i-acceptance.XXXXXX)"
chmod 700 "$work_dir"
node_modules_link_created=0
socket_dir="$work_dir/postgresql-socket"
mkdir "$socket_dir"
# The mount root is accessible to PostgreSQL in the container. Its 0700 parent
# keeps the mounted socket path private to the invoking host user.
chmod 777 "$socket_dir"
database_env_file="$work_dir/postgres.env"
common_env_file="$work_dir/application.env"
role_env_file="$work_dir/local-roles.env"
provision_env_file="$work_dir/provision.env"
playwright_env_file="$work_dir/playwright.env"
api_log="$work_dir/api.log"
playwright_log="$work_dir/playwright.log"
postgres_log="$work_dir/postgres.log"
alembic_log="$work_dir/alembic.log"
provision_first_log="$work_dir/provision-first.log"
provision_second_log="$work_dir/provision-second.log"
api_pid=""
playwright_pid=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "$playwright_pid" ]]; then
    kill -TERM -- "-$playwright_pid" >/dev/null 2>&1 || true
    kill -TERM "$playwright_pid" >/dev/null 2>&1 || true
    wait "$playwright_pid" >/dev/null 2>&1 || true
  fi
  if [[ -n "$api_pid" ]]; then
    kill -TERM "$api_pid" >/dev/null 2>&1 || true
    wait "$api_pid" >/dev/null 2>&1 || true
  fi
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  if [[ "$node_modules_link_created" == 1 ]]; then
    rm -f -- "$web_dir/node_modules"
  fi
  rm -rf -- "$work_dir"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ "$web_node_modules" != "$web_dir/node_modules" ]]; then
  if [[ -e "$web_dir/node_modules" || -L "$web_dir/node_modules" ]]; then
    printf 'Refusing to replace the existing web/node_modules path.\n' >&2
    exit 1
  fi
  web_node_modules="$(cd -- "$web_node_modules" && pwd)"
  ln -s -- "$web_node_modules" "$web_dir/node_modules"
  node_modules_link_created=1
fi

make_secret() {
  python3 -c 'import secrets; print(secrets.token_hex(32))'
}

print_sanitized_log() {
  local log_file="$1"
  shift
  python3 - "$log_file" "$@" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(errors="replace")
for env_path in sys.argv[2:]:
    for line in Path(env_path).read_text(errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator and value and (key.endswith("_PASSWORD") or key.endswith("_EMAIL")):
            text = text.replace(value, "[redacted]")
sys.stdout.write(text)
PY
}

run_backend() {
  local environment_file="$1"
  shift
  env -i PATH="$PATH" HOME="${HOME:-/root}" bash --noprofile --norc -c '
    environment_file=$1
    backend_dir=$2
    python_bin=$3
    shift 3
    set -a
    source "$environment_file"
    set +a
    export PYTHONPATH="$backend_dir"
    cd "$backend_dir"
    exec "$python_bin" "$@"
  ' labtrack-backend "$environment_file" "$backend_dir" "$python_bin" "$@"
}

db_password="$(make_secret)"
app_password="$(make_secret)"
jwt_secret="$(make_secret)"
fixture_tag="$(python3 -c 'import secrets; print(secrets.token_hex(6))')"

cat > "$database_env_file" <<EOF
POSTGRES_USER=postgres
POSTGRES_PASSWORD=$db_password
POSTGRES_DB=$database_name
APP_POSTGRES_USER=labtrack_app
APP_POSTGRES_PASSWORD=$app_password
PGPASSWORD=$db_password
POSTGRES_INITDB_ARGS=--auth-local=scram-sha-256 --auth-host=scram-sha-256
EOF
cat > "$common_env_file" <<EOF
APP_ENV=test
POSTGRES_HOST=$socket_dir
POSTGRES_PORT=5432
POSTGRES_DB=$database_name
POSTGRES_USER=postgres
POSTGRES_PASSWORD=$db_password
APP_POSTGRES_USER=labtrack_app
APP_POSTGRES_PASSWORD=$app_password
JWT_SECRET_KEY=$jwt_secret
DESTINATION_ORGANIZATION_CODE=CENTRAL_LAB
CELERY_BROKER_URL=redis://127.0.0.1:1/0
LOG_LEVEL=INFO
EOF
cat > "$role_env_file" <<EOF
LABTRACK_LOCAL_ROLE_FIXTURES=1
LABTRACK_LOCAL_REGION_CODE=PILOT
LABTRACK_LOCAL_SENDER_ORG_CODE=LOCAL_SENDER
LABTRACK_LOCAL_LAB_ORG_CODE=CENTRAL_LAB
LABTRACK_LOCAL_SENDER_EMAIL=stage-i-sender-$fixture_tag@example.test
LABTRACK_LOCAL_SENDER_PASSWORD=$(make_secret)
LABTRACK_LOCAL_COURIER_EMAIL=stage-i-courier-$fixture_tag@example.test
LABTRACK_LOCAL_COURIER_PASSWORD=$(make_secret)
LABTRACK_LOCAL_LAB_OPERATOR_EMAIL=stage-i-lab-operator-$fixture_tag@example.test
LABTRACK_LOCAL_LAB_OPERATOR_PASSWORD=$(make_secret)
LABTRACK_LOCAL_LAB_MANAGER_EMAIL=stage-i-lab-manager-$fixture_tag@example.test
LABTRACK_LOCAL_LAB_MANAGER_PASSWORD=$(make_secret)
LABTRACK_LOCAL_OPERATIONS_EMAIL=stage-i-operations-$fixture_tag@example.test
LABTRACK_LOCAL_OPERATIONS_PASSWORD=$(make_secret)
EOF
cat "$common_env_file" "$role_env_file" > "$provision_env_file"
chmod 600 "$database_env_file" "$common_env_file" "$role_env_file" "$provision_env_file"
mkdir "$work_dir/playwright-results"

if [[ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ]]; then
  playwright_browsers_path="$PLAYWRIGHT_BROWSERS_PATH"
else
  playwright_browsers_path=""
fi
cat > "$playwright_env_file" <<EOF
LABTRACK_REAL_ACCEPTANCE=1
LABTRACK_ACCEPTANCE_WEB_PORT=$web_port
LABTRACK_ACCEPTANCE_OUTPUT_DIR=$work_dir/playwright-results
API_PROXY_TARGET=http://127.0.0.1:$api_port
LABTRACK_LOCAL_SENDER_EMAIL=stage-i-sender-$fixture_tag@example.test
LABTRACK_LOCAL_SENDER_PASSWORD=$(sed -n 's/^LABTRACK_LOCAL_SENDER_PASSWORD=//p' "$role_env_file")
LABTRACK_LOCAL_COURIER_EMAIL=stage-i-courier-$fixture_tag@example.test
LABTRACK_LOCAL_COURIER_PASSWORD=$(sed -n 's/^LABTRACK_LOCAL_COURIER_PASSWORD=//p' "$role_env_file")
LABTRACK_LOCAL_LAB_OPERATOR_EMAIL=stage-i-lab-operator-$fixture_tag@example.test
LABTRACK_LOCAL_LAB_OPERATOR_PASSWORD=$(sed -n 's/^LABTRACK_LOCAL_LAB_OPERATOR_PASSWORD=//p' "$role_env_file")
EOF
chmod 600 "$playwright_env_file"

printf 'Stage I acceptance: fresh PostgreSQL, network none, no published container ports.\n'
docker run --detach --pull=never --network none \
  --name "$container_name" \
  --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=512m \
  --mount "type=bind,src=$socket_dir,dst=/var/run/postgresql" \
  --mount "type=bind,src=$backend_dir/database/init/10-runtime-role.sh,dst=/docker-entrypoint-initdb.d/10-runtime-role.sh,readonly" \
  --env-file "$database_env_file" \
  "$postgres_image" \
  -c listen_addresses= \
  -c unix_socket_directories=/var/run/postgresql \
  -c unix_socket_permissions=0777 >/dev/null

network_mode="$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$container_name")"
port_bindings="$(docker inspect --format '{{json .HostConfig.PortBindings}}' "$container_name")"
if [[ "$network_mode" != none || ( "$port_bindings" != null && "$port_bindings" != '{}' ) ]]; then
  printf 'The disposable PostgreSQL container is not isolated from host networking.\n' >&2
  exit 1
fi

if ! ready="$(
  for attempt in $(seq 1 60); do
    if docker exec "$container_name" pg_isready --username postgres --host /var/run/postgresql --dbname "$database_name" >/dev/null 2>&1; then
      printf 'ready'
      exit 0
    fi
    sleep 1
  done
  exit 1
)"; then
  docker logs "$container_name" > "$postgres_log" 2>&1 || true
  printf 'Disposable PostgreSQL did not become ready.\n' >&2
  [[ ! -f "$postgres_log" ]] || print_sanitized_log "$postgres_log" "$database_env_file" "$common_env_file" "$role_env_file"
  exit 1
fi
if [[ "$ready" != ready ]]; then
  printf 'Disposable PostgreSQL readiness check returned an unexpected result.\n' >&2
  exit 1
fi

if ! run_backend "$common_env_file" -m alembic upgrade head > "$alembic_log" 2>&1; then
  printf 'Alembic upgrade head failed on the fresh Unix-socket database.\n' >&2
  print_sanitized_log "$alembic_log" "$common_env_file" "$role_env_file"
  exit 1
fi
if ! run_backend "$common_env_file" -m alembic current >> "$alembic_log" 2>&1; then
  printf 'Alembic current failed on the disposable database.\n' >&2
  print_sanitized_log "$alembic_log" "$common_env_file" "$role_env_file"
  exit 1
fi
if ! run_backend "$common_env_file" -m alembic heads >> "$alembic_log" 2>&1; then
  printf 'Alembic heads failed.\n' >&2
  print_sanitized_log "$alembic_log" "$common_env_file" "$role_env_file"
  exit 1
fi
if ! run_backend "$common_env_file" -m alembic check >> "$alembic_log" 2>&1; then
  printf 'Alembic schema check failed after upgrade.\n' >&2
  print_sanitized_log "$alembic_log" "$common_env_file" "$role_env_file"
  exit 1
fi
printf 'Migrations: head applied; current/head/schema check passed.\n'

privileges="$(docker exec "$container_name" psql -X --username postgres --dbname "$database_name" --tuples-only --no-align --set=ON_ERROR_STOP=1 --command="SELECT has_table_privilege('labtrack_app', 'public.users', 'SELECT,INSERT,UPDATE,DELETE') AND has_table_privilege('labtrack_app', 'public.shipments', 'SELECT,INSERT,UPDATE,DELETE') AND COALESCE((SELECT bool_and(has_sequence_privilege('labtrack_app', format('%I.%I', schemaname, sequencename), 'USAGE,SELECT')) FROM pg_sequences WHERE schemaname='public'), true);")"
if [[ "$privileges" != t ]]; then
  printf 'Runtime-role table or sequence privilege check failed.\n' >&2
  exit 1
fi
docker exec -i "$container_name" psql -X --quiet --username postgres --dbname "$database_name" --set=ON_ERROR_STOP=1 <<'SQL'
INSERT INTO organizations (id, region_id, code, name, kind, is_active)
SELECT gen_random_uuid(), region.id, 'LOCAL_SENDER', 'Local Synthetic Sender', 'HEALTHCARE_FACILITY', true
FROM regions AS region
WHERE region.code = 'PILOT'
ON CONFLICT (code) DO NOTHING;
DO $$
BEGIN
  IF (SELECT count(*) FROM regions WHERE code='PILOT') <> 1
    OR (SELECT count(*) FROM organizations WHERE code='CENTRAL_LAB') <> 1
    OR (SELECT count(*) FROM organizations WHERE code='LOCAL_SENDER') <> 1 THEN
    RAISE EXCEPTION 'local acceptance organization fixture is incomplete';
  END IF;
END
$$;
SQL
printf 'Runtime-role privileges and local organization scopes: passed.\n'

if ! scope_rows="$(docker exec -i "$container_name" sh -c 'export PGPASSWORD="$APP_POSTGRES_PASSWORD"; exec psql -X --username "$APP_POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align --field-separator="|" --set=ON_ERROR_STOP=1' <<'SQL'
SELECT 'REGION', code, '', id::text, is_active::text, is_active::text
FROM regions
WHERE code = 'PILOT'
UNION ALL
SELECT 'ORGANIZATION', organization.code, organization.kind, organization.region_id::text,
       organization.is_active::text, region.is_active::text
FROM organizations AS organization
JOIN regions AS region ON region.id = organization.region_id
WHERE organization.code IN ('CENTRAL_LAB', 'LOCAL_SENDER')
ORDER BY 1, 2;
SQL
)"; then
  printf 'Runtime-role scope diagnostic query failed.\n' >&2
  exit 1
fi
printf 'Disposable DB scope rows (runtime role; type|code|kind|region_id|active|region_active):\n%s\n' "$scope_rows"

if ! run_backend "$provision_env_file" - <<'PY'
import asyncio

from app.db.session import dispose_engine, get_session_factory
from app.modules.identity.local_role_fixtures import load_local_role_specs
from app.modules.identity.provisioning import ProvisioningError, _resolve_membership_scope


async def main() -> int:
    import os

    specs = load_local_role_specs(os.environ)
    try:
        async with get_session_factory()() as session:
            for spec in specs:
                try:
                    organization, region = await _resolve_membership_scope(
                        session,
                        role=spec.role,
                        organization_code=spec.organization_code,
                        region_code=spec.region_code,
                    )
                except ProvisioningError as error:
                    print(f"Scope lookup failed for {spec.key}: {error}")
                    return 1
                scope = organization.code if organization is not None else region.code
                print(f"Scope lookup resolved for {spec.key}: {scope}")
        return 0
    finally:
        await dispose_engine()


raise SystemExit(asyncio.run(main()))
PY
then
  printf 'A local role scope could not be resolved; see the role-specific diagnostic above.\n' >&2
  exit 1
fi

if ! run_backend "$provision_env_file" -m app.cli provision-local-roles > "$provision_first_log" 2>&1; then
  printf 'First guarded local-role provisioning run failed.\n' >&2
  print_sanitized_log "$provision_first_log" "$common_env_file" "$role_env_file"
  exit 1
fi
if ! run_backend "$provision_env_file" -m app.cli provision-local-roles > "$provision_second_log" 2>&1; then
  printf 'Repeat guarded local-role provisioning run failed.\n' >&2
  print_sanitized_log "$provision_second_log" "$common_env_file" "$role_env_file"
  exit 1
fi
python3 - "$role_env_file" "$provision_first_log" "$provision_second_log" <<'PY'
from pathlib import Path
import sys

role_env = Path(sys.argv[1]).read_text().splitlines()
logs = [Path(path).read_text(errors="replace") for path in sys.argv[2:]]
credentials = [line.partition("=")[2] for line in role_env if "_EMAIL=" in line or "_PASSWORD=" in line]
if logs[0].strip() != "Local role fixtures ensured: 5 accounts (created=5, updated=0, unchanged=0).":
    raise SystemExit("First provisioning summary did not report five created fixtures.")
if logs[1].strip() != "Local role fixtures ensured: 5 accounts (created=0, updated=0, unchanged=5).":
    raise SystemExit("Repeated provisioning summary did not report five unchanged fixtures.")
if any(value and value in "\n".join(logs) for value in credentials):
    raise SystemExit("A generated local email or password appeared in provisioning output.")
PY
printf 'Local role CLI: created five fixtures, then reported five unchanged; output contains no credentials.\n'

run_backend "$common_env_file" -m uvicorn app.main:app --host 127.0.0.1 --port "$api_port" --no-access-log > "$api_log" 2>&1 &
api_pid=$!
if ! python3 - "$api_port" <<'PY'
import socket
import sys
import time

port = int(sys.argv[1])
deadline = time.monotonic() + 30
last_error = "not started"
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as client:
            client.sendall(
                b"GET /health/ready HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            response = client.recv(4096)
        if response.startswith(b"HTTP/1.1 200") or response.startswith(b"HTTP/1.0 200"):
            raise SystemExit(0)
        last_error = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    except OSError as error:
        last_error = type(error).__name__
    time.sleep(0.25)
print(f"API loopback readiness failed: {last_error}", file=sys.stderr)
raise SystemExit(1)
PY
then
  printf 'API failed its local database readiness probe.\n' >&2
  print_sanitized_log "$api_log" "$common_env_file" "$role_env_file"
  exit 1
fi
printf 'API: ready on 127.0.0.1; Vite will use its existing loopback proxy.\n'

run_playwright() {
  setsid env -i PATH="$PATH" HOME="${HOME:-/root}" bash --noprofile --norc -c '
    environment_file=$1
    web_dir=$2
    playwright_cli=$3
    browser_path=$4
    shift 4
    set -a
    source "$environment_file"
    set +a
    if [ -n "$browser_path" ]; then export PLAYWRIGHT_BROWSERS_PATH="$browser_path"; fi
    cd "$web_dir"
    exec "$playwright_cli" test --config playwright.local-acceptance.config.ts "$@"
  ' labtrack-playwright "$playwright_env_file" "$web_dir" "$web_node_modules/.bin/playwright" "$playwright_browsers_path" "$@" > "$playwright_log" 2>&1 &
  playwright_pid=$!
  local status=0
  if wait "$playwright_pid"; then status=0; else status=$?; fi
  print_sanitized_log "$playwright_log" "$common_env_file" "$role_env_file" "$playwright_env_file"
  if [[ "$status" -ne 0 ]]; then
    printf 'FastAPI local request log for the failed browser run:\n' >&2
    print_sanitized_log "$api_log" "$common_env_file" "$role_env_file" "$playwright_env_file" >&2
  fi
  return "$status"
}

run_playwright
printf 'Real browser acceptance passed. Cleanup will remove the API, disposable database, socket, and temp files.\n'
