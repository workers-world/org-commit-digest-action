#!/usr/bin/env bash
# Wrapper for local runs; GitHub Action calls digest.py directly.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/digest.py" "$@"
