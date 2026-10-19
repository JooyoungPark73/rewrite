# Live acceptance scripts

`p1-e2e.mjs` … `p6-e2e.mjs` drive the real backend (serving `frontend/dist`) with
Playwright and print one PASS/FAIL line per manual check from the phase briefs in
`.agents/scratch/`. `run-all.sh` prepares a fresh repo per phase (via `prep.py`) and runs
them all; see its header for prerequisites. Phases 4–6 make real claude/codex calls.
