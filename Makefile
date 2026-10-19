# rewrite — start and stop the app without remembering the commands.
#
#   make setup                 install backend (uv) and frontend (npm) dependencies, build the UI
#   make start [REPO=~/paper]  start the backend on 127.0.0.1:8765 serving the built UI, open the browser
#   make stop                  stop it
#   make restart REPO=~/paper  stop + start
#   make status                is it running? (pid and /api/health)
#   make open                  open http://127.0.0.1:8765 in the default browser
#   make logs                  follow the server log
#   make init REPO=~/paper     create .rewrite/ in a paper repo (idempotent)
#   make dev REPO=~/paper      development: backend with --reload on :8000 + Vite on :5173
#   make test                  backend pytest + ruff, frontend typecheck + lint + build + e2e
#
# REPO is the paper repo to open (default: $REWRITE_REPO). Without either, `make start` shows a
# Finder folder picker on macOS (Cancel = choose the repo in the UI, which can browse folders
# too). `rewrite init` (idempotent: it only adds missing files, never overwrites) runs on the
# repo before serving, so a repo initialised by an older version gets new prompt files.
# PORT defaults to 8765. The pid and log live under .run/ in this repo (gitignored), never in
# a global path; one `make start` = one backend = one paper (use a different PORT for another).

SHELL := /bin/bash
.DEFAULT_GOAL := help

PORT ?= 8765
REPO ?= $(REWRITE_REPO)
# A leading ~ is not expanded inside make (and a command-line REPO= cannot be reassigned), so expand it into REPO_DIR.
REPO_DIR := $(patsubst ~%,$(HOME)%,$(REPO))
URL  := http://127.0.0.1:$(PORT)
RUN  := .run
PID  := $(RUN)/server-$(PORT).pid
LOG  := $(RUN)/server-$(PORT).log

# `open` on macOS, xdg-open / wslview elsewhere.
OPEN := $(shell command -v open 2>/dev/null || command -v wslview 2>/dev/null || command -v xdg-open 2>/dev/null || echo true)

.PHONY: help setup build start stop restart status open logs init dev dev-backend dev-frontend test test-backend test-frontend clean

help:
	@sed -n '2,15p' $(MAKEFILE_LIST) | sed 's/^# \{0,1\}//'

setup:
	cd backend && uv sync
	cd frontend && npm install
	$(MAKE) build

build:
	cd frontend && npm run build

init:
	@test -n "$(REPO)" || { echo "usage: make init REPO=<paper repo>"; exit 2; }
	cd backend && uv run rewrite init "$(REPO_DIR)"

start: $(RUN)
	@if [ -f "$(PID)" ] && kill -0 "$$(cat "$(PID)")" 2>/dev/null; then \
	  echo "already running on $(URL) (pid $$(cat "$(PID)")); use make restart"; exit 0; \
	fi; \
	if [ ! -f frontend/dist/index.html ]; then \
	  echo "frontend/dist is missing; run make setup (or make build) first"; exit 2; \
	fi; \
	if curl -sf "$(URL)/api/health" > /dev/null; then \
	  echo "something else already answers on $(URL); pick another PORT"; exit 2; \
	fi; \
	repo="$(REPO_DIR)"; \
	if [ -z "$$repo" ] && command -v osascript > /dev/null 2>&1; then \
	  echo "no REPO given: pick the paper folder in the Finder dialog (Cancel = choose it in the UI)"; \
	  repo=$$(osascript -e 'POSIX path of (choose folder with prompt "rewrite: choose the paper repo to open")' 2>/dev/null | sed 's:/$$::'); \
	fi; \
	if [ -n "$$repo" ]; then \
	  ( cd backend && uv run rewrite init "$$repo" > /dev/null ) || { echo "rewrite init $$repo failed"; exit 1; }; \
	fi; \
	if [ -n "$$repo" ]; then set -- --repo "$$repo"; else set --; fi; \
	( cd backend && exec uv run rewrite serve "$$@" --port $(PORT) ) > "$(LOG)" 2>&1 < /dev/null & \
	echo $$! > "$(PID)"; \
	for i in $$(seq 1 40); do curl -sf "$(URL)/api/health" > /dev/null && break; sleep 0.5; done; \
	if ! curl -sf "$(URL)/api/health" > /dev/null || ! kill -0 "$$(cat "$(PID)")" 2>/dev/null; then \
	  echo "server did not come up; see $(LOG)"; tail -20 "$(LOG)"; rm -f "$(PID)"; exit 1; \
	fi; \
	echo "rewrite is running on $(URL) (pid $$(cat "$(PID)"), log $(LOG))"; \
	$(OPEN) "$(URL)"

stop:
	@if [ -f "$(PID)" ]; then \
	  pid=$$(cat "$(PID)"); \
	  if kill -0 "$$pid" 2>/dev/null; then \
	    pkill -TERM -P "$$pid" 2>/dev/null; kill -TERM "$$pid" 2>/dev/null; \
	    for i in $$(seq 1 20); do kill -0 "$$pid" 2>/dev/null || break; sleep 0.25; done; \
	    kill -0 "$$pid" 2>/dev/null && { pkill -KILL -P "$$pid" 2>/dev/null; kill -KILL "$$pid" 2>/dev/null; }; \
	    echo "stopped pid $$pid"; \
	  else echo "not running (stale pid file)"; fi; \
	  rm -f "$(PID)"; \
	else echo "not running"; fi

restart: stop start

status:
	@if [ -f "$(PID)" ] && kill -0 "$$(cat "$(PID)")" 2>/dev/null; then \
	  echo "running: pid $$(cat "$(PID)") on $(URL)"; curl -s "$(URL)/api/health"; echo; \
	else echo "not running"; fi

open:
	$(OPEN) "$(URL)"

logs:
	tail -f "$(LOG)"

# Development: backend with auto-reload on :8000, Vite dev server on :5173 proxying /api and /ws.
dev:
	@trap 'kill 0' INT TERM EXIT; \
	  ( cd backend && REWRITE_REPO="$(REPO_DIR)" uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 ) & \
	  ( cd frontend && npm run dev ) & \
	  wait

dev-backend:
	cd backend && REWRITE_REPO="$(REPO_DIR)" uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

dev-frontend:
	cd frontend && npm run dev

test: test-backend test-frontend

test-backend:
	cd backend && uv run pytest -q && uv run ruff check .

test-frontend:
	cd frontend && npm run typecheck && npm run lint && npm run build && npm run e2e

clean: stop
	rm -rf $(RUN) frontend/dist

$(RUN):
	mkdir -p $(RUN)
