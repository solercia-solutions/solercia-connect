#!/bin/sh
set -eu

json_error() {
  code=$1
  message=$2
  status=$3
  cat <<EOF
{
  "schema": "solercia.connect/v1",
  "ok": false,
  "operation": "wrapper",
  "status": "$status",
  "provider": null,
  "account": null,
  "route": null,
  "data": {},
  "receipt": null,
  "error": {
    "code": "$code",
    "message": "$message",
    "retryable": false,
    "missing_configuration": [],
    "remediation": ["Run make setup, then retry the wrapper command."],
    "cause_category": "configuration"
  }
}
EOF
}

if [ "$#" -lt 2 ]; then
  json_error "invalid_input" "TYPE and ACCOUNT are required" "invalid_input"
  exit 2
fi

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
if [ -x "$script_dir/.venv/bin/solercia-connect" ]; then
  cli="$script_dir/.venv/bin/solercia-connect"
elif command -v solercia-connect >/dev/null 2>&1; then
  cli=$(command -v solercia-connect)
else
  json_error "cli_not_installed" "The solercia-connect CLI is not installed" "configuration_error"
  exit 1
fi

exec "$cli" connect "$@"
