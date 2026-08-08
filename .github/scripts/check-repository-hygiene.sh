#!/usr/bin/env bash

set -euo pipefail

violations=()

check_path() {
  local path="${1#./}"
  local basename="${path##*/}"
  local reason=""

  case "$basename" in
    .env.example|.env.sample|.env.template)
      return
      ;;
    .env|.env.*|.envrc)
      reason="environment or secret configuration"
      ;;
    .DS_Store|Thumbs.db|Desktop.ini)
      reason="operating-system metadata"
      ;;
    *.pem|*.key|*.p12|*.pfx|id_rsa|id_dsa|id_ecdsa|id_ed25519)
      reason="private key or certificate bundle"
      ;;
    credentials.json|service-account.json|service-account-key.json)
      reason="credential file"
      ;;
    *.log)
      reason="runtime log"
      ;;
    .coverage|coverage.xml|*.pyc|*.pyo)
      reason="generated Python test or bytecode artifact"
      ;;
    *.sqlite3|*.sqlite|*.db)
      reason="generated local database"
      ;;
    *.swp|*.swo|*~)
      reason="editor temporary file"
      ;;
  esac

  if [[ -z "$reason" ]]; then
    case "/$path/" in
      */.idea/*|*/.vscode/*)
        reason="IDE workspace metadata"
        ;;
      */.venv/*|*/venv/*|*/env/*)
        reason="local Python environment"
        ;;
      */__pycache__/*|*/.pytest_cache/*|*/.ruff_cache/*)
        reason="generated Python cache"
        ;;
      */htmlcov/*|*/data/*|*.egg-info/*)
        reason="generated runtime or build output"
        ;;
    esac
  fi

  if [[ -n "$reason" ]]; then
    violations+=("$path ($reason)")
  fi
}

if (( $# > 0 )); then
  for path in "$@"; do
    check_path "$path"
  done
else
  while IFS= read -r -d '' path; do
    check_path "$path"
  done < <(git ls-files -z)
fi

if (( ${#violations[@]} > 0 )); then
  echo "Forbidden files are tracked by Git:"
  printf '  - %s\n' "${violations[@]}"
  echo
  echo "Remove them from Git history and commit a safe example/template instead."
  exit 1
fi

echo "Repository hygiene check passed."
