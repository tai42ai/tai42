#!/usr/bin/env bash
# Run pyright per workspace member, the way CI does.
#
# There is no root pyright config, so a `pyright` run from the repo root falls
# back to its defaults (standard mode, reportMissingImports=error) and reports
# hundreds of false positives. Each member carries its own `[tool.pyright]`; it
# only binds when pyright runs from that member's directory. This script scopes
# the venv to each member (`uv sync --locked --package <name> <extras>`) then
# runs `uv run --no-sync pyright` from its directory — the exact per-member
# recipe the ci.yml `check` matrix uses.
#
# The member/extras list below is derived from the member table in
# .github/workflows/ci.yml (jobs.changes.assemble, `dir|package|sync_extra`
# columns); that table is the source of truth — keep this list in step with it.
#
# Members run independently (CI runs them as a fail-fast:false matrix), so this
# sweeps every member and reports all failures at the end rather than stopping
# on the first.
set -euo pipefail

cd "$(dirname "$0")/.."

# dir|package|sync_extra — mirrors the ci.yml member table.
read -r -d '' MEMBERS <<'EOF' || true
core/contract|tai42-contract|--extra dev
core/kit|tai42-kit|--extra dev --extra llm --extra jq --extra uvicorn --extra redis --extra curl --extra postgres
core/cli|tai42-cli|--extra dev
core/skeleton|tai42-skeleton|--extra dev
plugins/accounts-oidc|tai42-accounts-oidc|--extra dev
plugins/accounts-postgres|tai42-accounts-postgres|--extra dev
plugins/agents|tai42-agents|--extra dev
plugins/backend-arq|tai42-backend-arq|
plugins/backend-celery|tai42-backend-celery|
plugins/backend-rq|tai42-backend-rq|
plugins/channel-slack|tai42-channel-slack|
plugins/channel-telegram|tai42-channel-telegram|
plugins/channel-twilio|tai42-channel-twilio|
plugins/channel-web|tai42-channel-web|
plugins/channel-whatsapp|tai42-channel-whatsapp|
plugins/config-k8s|tai42-config-k8s|
plugins/identity-oidc|tai42-identity-oidc|--extra dev
plugins/identity-redis|tai42-identity-redis|--extra dev
plugins/monitoring-langfuse|tai42-monitoring-langfuse|
plugins/sandbox-docker|tai42-sandbox-docker|
plugins/sandbox-local|tai42-sandbox-local|
plugins/storage-github|tai42-storage-github|
plugins/storage-local|tai42-storage-local|
plugins/storage-s3|tai42-storage-s3|
plugins/toolbox|tai42-toolbox|--extra dev
plugins/tools-github|tai42-tools-github|--extra dev
plugins/tools-stripe|tai42-tools-stripe|--extra dev
plugins/tools-twilio|tai42-tools-twilio|--extra dev
plugins/tools-whatsapp|tai42-tools-whatsapp|--extra dev
plugins/webhook-verifier-github|tai42-webhook-verifier-github|
plugins/webhook-verifier-stripe|tai42-webhook-verifier-stripe|
e2e|tai42-e2e|
EOF

failed=()
while IFS='|' read -r dir package sync_extra; do
  [ -n "$dir" ] || continue
  echo "==> pyright $package ($dir)"
  # shellcheck disable=SC2086  # sync_extra is a deliberate multi-flag string.
  uv sync --locked --package "$package" $sync_extra
  if ( cd "$dir" && uv run --no-sync pyright ); then
    echo "    ok: $package"
  else
    echo "    FAIL: $package"
    failed+=("$package")
  fi
done <<<"$MEMBERS"

if [ ${#failed[@]} -gt 0 ]; then
  echo
  echo "pyright failed for ${#failed[@]} member(s): ${failed[*]}"
  exit 1
fi

echo
echo "pyright clean across all members"
