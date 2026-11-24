#!/usr/bin/env bash
# Builds the plugin zip that Decky installs: out/flipper-remote-decky-<version>.zip
#   APP=path/to/flipper-remote ./build.sh     (where the Flipper Remote app's flipper_remote.py is)
set -euo pipefail
cd "$(dirname "$0")"

APP="${APP:-../flipper-remote}"
VERSION=$(python3 -c "import json; print(json.load(open('package.json'))['version'])")

cp "$APP/flipper_remote.py" py_modules/flipper_remote.py   # the app's code, unchanged
pnpm install --frozen-lockfile --silent
pnpm -s typecheck
pnpm -s build

mkdir -p out
ZIP="out/flipper-remote-decky-$VERSION.zip"
python3 - "$ZIP" <<'EOF'
import sys, zipfile
files = ["plugin.json", "package.json", "main.py", "LICENSE", "README.md", "dist/index.js",
         "py_modules/flipper_helper.py", "py_modules/flipper_remote.py"]
# Decky wants one folder at the top of the zip, with plugin.json in it.
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for name in files:
        z.write(name, "flipper-remote/" + name)
EOF
echo "Built $ZIP"
