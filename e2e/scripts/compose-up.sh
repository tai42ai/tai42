#!/usr/bin/env bash
# Start services of one profile of the e2e compose file and wait until they are healthy.
#
# Usage: compose-up.sh <profile> <service>...
#
# The services start detached, so a service that fails to start or never turns healthy
# leaves its reason only in its container log. On a failed wait this prints every
# container of the profile (stopped ones included) and all their logs, so the failing
# dependency of a named service shows too, then exits non-zero.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <profile> <service>..." >&2
  exit 2
fi
profile=$1
shift

cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker compose --profile "$profile" up -d --wait "$@" || {
  echo "::group::$profile profile: containers and logs"
  docker compose --profile "$profile" ps --all
  docker compose --profile "$profile" logs
  echo "::endgroup::"
  exit 1
}
