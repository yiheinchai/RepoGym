#!/usr/bin/env sh
# Export verified tasks in SWE-bench JSONL, plus git bundles so another machine can reproduce them.
set -e
OUT=${1:-./repogym-export}
mkdir -p "$OUT"
repogym export --format swebench --tier verified -o "$OUT/tasks.jsonl"
repogym list --json --tier verified | python3 -c 'import json,sys; [print(t["id"]) for t in json.load(sys.stdin)]' \
  | while read -r id; do repogym bundle "$id" -o "$OUT/$id.bundle"; done
echo "wrote $OUT"
