#!/usr/bin/env bash
# Makes tests/pb: Python code for the Flipper's protobuf messages. The mock Flipper uses it, and a few tests use it
# to check the app's own encoding. It's generated from Flipper's .proto files
# (github.com/flipperdevices/flipperzero-protobuf), which aren't copied into this repository.
#
#   tests/make_pb.sh                  downloads the .proto files (needs git)
#   tests/make_pb.sh path/to/protos   uses a copy you already have
#
# Needs grpcio-tools (pip install grpcio-tools).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REV=1c84fa48919cbb71d1cc65236fc0ee36740e24c6   # the version the tests were written against (December 2024)

SRC="${1:-}"
if [ -z "$SRC" ]; then
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  git clone --quiet https://github.com/flipperdevices/flipperzero-protobuf "$TMP/proto"
  git -C "$TMP/proto" checkout --quiet "$REV"
  SRC="$TMP/proto"
fi
if ! ls "$SRC"/flipper.proto > /dev/null 2>&1; then
  echo "There's no flipper.proto in $SRC." >&2
  exit 1
fi

mkdir -p "$HERE/pb"
touch "$HERE/pb/__init__.py"
python3 -m grpc_tools.protoc -I "$SRC" --python_out="$HERE/pb" "$SRC"/*.proto
echo "Made $HERE/pb"
