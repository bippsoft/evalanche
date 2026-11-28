#!/usr/bin/env bash
# Export the schema of every fixture under both pinned evaluators, compare the
# two outputs, and compare each with the expected file.
#
#   tests/schema/run.sh           check
#   tests/schema/run.sh --update  rewrite the expected files from CppNix
set -euo pipefail

root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"

: "${EVALANCHE_CPPNIX_BIN:=$(nix build --no-link --print-out-paths .#cppnix | grep -v -- "-man$")/bin}"
: "${EVALANCHE_LIX_BIN:=$(nix build --no-link --print-out-paths .#lix | grep -v -- '-man$')/bin}"
nixpkgs=$(nix eval --raw --impure --expr "(builtins.getFlake \"path:$root\").inputs.nixpkgs.outPath")

out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
status=0

for fixture in tests/schema/fixtures/*.nix; do
  name=$(basename "$fixture" .nix)
  expr="import $root/nix/schema.nix { target = { modules = [ $root/$fixture ]; lib = import $nixpkgs/lib; }; }"
  for evaluator in cppnix lix; do
    case $evaluator in
      cppnix) bin=$EVALANCHE_CPPNIX_BIN ;;
      lix) bin=$EVALANCHE_LIX_BIN ;;
    esac
    "$bin/nix-instantiate" --eval --strict --json -E "$expr" | jq -S . > "$out/$name.$evaluator.json"
  done
  if ! diff -u "$out/$name.cppnix.json" "$out/$name.lix.json"; then
    echo "$name: cppnix and lix differ"
    status=1
  fi
  if [[ ${1:-} == --update ]]; then
    cp "$out/$name.cppnix.json" "tests/schema/expected/$name.json"
  elif ! diff -u "tests/schema/expected/$name.json" "$out/$name.cppnix.json"; then
    echo "$name: differs from expected"
    status=1
  else
    echo "$name: ok ($(jq '.types | length' "$out/$name.cppnix.json") types, $(jq '.options | length' "$out/$name.cppnix.json") options)"
  fi
done
exit $status
