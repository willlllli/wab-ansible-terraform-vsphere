#!/usr/bin/env bash
# Starter fuer wrapper.py: laedt secrets.env und nutzt den Python des venv.
# Liegt in ~/iac_lab/scripts/; secrets.env und venv-wab/ liegen eine Ebene hoeher.
# Aufruf aus beliebigem Verzeichnis, z. B. in tmux:
#   tmux new -s wab '~/iac_lab/scripts/start_wrapper.sh run --phase trocken'
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

if [[ ! -r secrets.env ]]; then
    echo "secrets.env fehlt oder ist nicht lesbar" >&2
    exit 2
fi
if [[ "$(stat -c %a secrets.env)" != "600" ]]; then
    echo "secrets.env hat nicht die Rechte 600" >&2
    exit 2
fi

set -a
# shellcheck disable=SC1091
source ./secrets.env
set +a

exec ./venv-wab/bin/python ./scripts/wrapper.py "$@"
