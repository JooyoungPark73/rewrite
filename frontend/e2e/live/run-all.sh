#!/usr/bin/env bash
# Runs the live acceptance scripts p1..p6 against the REAL backend, each on a fresh
# copy of examples/sample-paper. Usage, from the repo root:
#   frontend/e2e/live/run-all.sh [out-dir] [phases...]      e.g. run-all.sh /tmp/rw 1 2 3
# Needs: uv, node + `npm install` + `npx playwright install chromium` in frontend/,
# latexmk on PATH, `npm run build` done (the backend serves frontend/dist), and for
# phases 4-6 the claude and codex CLIs logged in (real model calls, ~$0.05 per script).
set -u
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
OUT="${1:-${TMPDIR:-/tmp}/rewrite-live}"; shift || true
PHASES=("$@"); [ ${#PHASES[@]} -eq 0 ] && PHASES=(1 2 3 4 5 6)
PORT=8765
mkdir -p "$OUT"
# bash 3.2 (macOS) has no associative arrays, hence the case statements below.
mode_for() { case "$1" in 4) echo intro;; 5) echo p5;; 6) echo all;; *) echo none;; esac; }
args_for() { case "$1" in 4) echo "--runner claude --model sonnet";; 5|6) echo "--model sonnet";; *) echo "";; esac; }
status=0
for n in "${PHASES[@]}"; do
  R="$OUT/repo$n"; rm -rf "$R" "$R-origin.git"
  cp -r "$ROOT/examples/sample-paper" "$R"
  ( cd "$R" && git init -q && git add -A && git -c user.name=live -c user.email=live@example.com commit -qm init
    if [ "$n" = 5 ]; then git init -q --bare "$R-origin.git" && git remote add origin "$R-origin.git"; fi )
  ( cd "$ROOT/backend" && uv run rewrite init "$R" >/dev/null )
  ( cd "$ROOT/backend" && uv run rewrite serve --repo "$R" --port $PORT > "$OUT/server$n.log" 2>&1 & )
  for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null && break; sleep 0.5; done
  if [ "$n" -le 3 ]; then curl -s -X POST "http://127.0.0.1:$PORT/api/compile" >/dev/null; fi
  MODE=$(mode_for "$n"); if [ "$MODE" != none ]; then python3 "$ROOT/frontend/e2e/live/prep.py" "$MODE"; fi
  ( cd "$ROOT/frontend" && node "e2e/live/p$n-e2e.mjs" "$R" "$OUT" $(args_for "$n") ) | tee "$OUT/p$n.log" | grep -E "RESULT|FAIL|SKIP|JS ERRORS" | sed "s/^/p$n: /"
  grep -q "RESULT: PASS" "$OUT/p$n.log" || status=1
  pkill -f "rewrite serve --repo $R" ; sleep 1
done
echo "all done; screenshots and logs in $OUT"; exit $status
