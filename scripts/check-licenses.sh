#!/bin/bash
# Fail if a runtime dependency has a license outside the project's policy:
# permissive, LGPL, MPL-2.0 and GPLv3/AGPLv3 are allowed; GPLv2-only and EPL are not.
# Run it in an environment synced without dev dependencies:
#   uv sync --locked --no-dev --all-packages && scripts/check-licenses.sh
# A license string that matches nothing here fails the check and needs a person to look.
set -euo pipefail

allowed=(
    "MIT" "BSD" "Apache" "ISC" "Python Software Foundation" "PSF" "Zlib" "Unlicense" "0BSD"
    "LGPL" "Lesser General Public License"
    "GPL-3.0" "GPLv3" "AGPL"
    "MPL-2.0" "Mozilla Public License 2.0"
    # paho-mqtt (under aiomqtt) is "EPL-2.0 OR BSD-3-Clause": used under the BSD-3-Clause
    # option, its Eclipse Distribution License; the "BSD" entry above lets it through.
)

IFS=';'
uvx pip-licenses --python .venv/bin/python --from=mixed --partial-match \
    --allow-only="${allowed[*]}"
