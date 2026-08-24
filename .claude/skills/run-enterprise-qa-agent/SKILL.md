---
name: run-enterprise-qa-agent
description: Build, launch, and drive the RAG Agent full stack (Python/FastAPI backend, React/Vite frontend, Postgres, mock tenant microservices). Use when asked to run/start/launch this project, take a screenshot of the RAG Agent UI, log in and query the chat, check the gateway/admin panel, or smoke-test the backend API.
---

RAG Agent is a full-stack app: a FastAPI backend (`src/ragent_backend/app.py`,
uvicorn on :8010), a React/Vite frontend (`frontend/`, :5190, proxies `/api`
and `/ws` to the backend), Postgres (persistent system service, not managed
by this skill), and 4 mock tenant microservices (`scripts/tenant_service_supervisor.py`,
ports 9101/9102/9201/9202) that the admin "网关监控" (Gateway Monitor) page
polls for health. Drive the UI with the `mcp__claude-in-chrome__*` browser
tools (no `chromium-cli` binary in this environment — verified below); for
backend-only changes, `curl` against the API is faster and covers more of
what recent PRs actually touch (role/permission logic).

All paths below are relative to the repo root.

## Prerequisites

macOS + Homebrew. Postgres 16 already runs as a brew service with a
`ragent`/`ragent` role and database provisioned — this is shared,
persistent system state, not something this skill starts fresh each run:

```bash
brew services list | grep postgresql        # expect "started"
brew services start postgresql@16           # only if not already running
psql "postgresql://ragent:ragent@localhost:5432/ragent" -c '\q'   # sanity check
```

Python deps live in `.venv/` (`pyproject.toml`), frontend deps in
`frontend/node_modules/` (`package.json`). Both were already installed in
this container — not reinstalled this session. If either is missing on a
truly fresh checkout:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e .
cd frontend && npm install && cd ..
```

## Run (agent path)

Three app processes, backend before frontend (see Gotchas — avoids a few
seconds of harmless proxy noise):

```bash
# 1. Backend API — :8010 (RAGENT_PORT in .env; 8000 is taken by another
#    project on this machine, hence the non-default port)
nohup .venv/bin/python -m src.ragent_backend.app > /tmp/ragent-backend.log 2>&1 &
disown

# 2. Tenant demo microservices — 4 mock KB/attendance connectors
#    (Acme + Globex) that the Gateway Monitor admin page polls.
#    Self-healing: re-launches any of the 4 that die, every 15s, forever.
nohup .venv/bin/python scripts/tenant_service_supervisor.py > /tmp/ragent-supervisor.log 2>&1 &
disown

# 3. Frontend — Vite dev server, :5190 (strictPort: true — fails loudly
#    instead of silently picking another port if 5190 is taken).
#    cd/disown must be in the SAME shell invocation as the `&` — a
#    subshell `(cd frontend && ... &)` makes `disown` fail with
#    "no current job" (harmless, but noisy; avoid it).
cd frontend && nohup npm run dev -- --host > /tmp/ragent-frontend.log 2>&1 &
disown
cd ..
```

Poll instead of guessing when things are ready. This macOS box has no
`timeout`/`gtimeout` binary (GNU coreutils, not installed) — use a bounded
bash loop instead:

```bash
poll() { for i in $(seq 1 "$2"); do curl -sf "$1" >/dev/null && return 0; sleep 1; done; return 1; }
poll http://localhost:8010/docs 30 && echo "backend ready"
poll http://localhost:5190/ 30 && echo "frontend ready"
poll http://localhost:9201/healthz 20 && echo "attendance mock ready"
```

Stop (leave Postgres running — it's shared system state):

```bash
lsof -tiTCP:8010 -sTCP:LISTEN | xargs -r kill
lsof -tiTCP:5190 -sTCP:LISTEN | xargs -r kill
pkill -f "scripts/tenant_service_supervisor.py"
pkill -f "uvicorn services.tenant_kb_demo.app:app"
pkill -f "uvicorn services.tenant_attendance_demo.app:app"
```

### Backend-only smoke test (curl)

Covers what most recent PRs here actually touch — role/permission logic in
`app.py`/`role_store.py`. No browser needed.

```bash
TOKEN=$(curl -s -X POST http://localhost:8010/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"qa_run_company_user","password":"QaRun@2026"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s http://localhost:8010/api/v1/auth/me -H "Authorization: Bearer $TOKEN"
# → 200 {"username":"qa_run_company_user","roles":[...],"organization":{"name":"测试新公司",...}}
```

Platform-admin-only endpoints (gateway monitor, role/org management):

```bash
TOKEN=$(curl -s -X POST http://localhost:8010/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"qa_run_platform_admin","password":"QaRun@2026"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s http://localhost:8010/api/v1/admin/gateway/connectors -H "Authorization: Bearer $TOKEN"
# → each connector's health_status should be "connected" or "internal".
#   "unreachable" on the two attendance rows means step 2 above
#   (tenant_service_supervisor.py) isn't running.
```

More accounts (different roles/orgs, all password `QaRun@2026`) are listed
in `tests/fixtures/golden_test_set_tenant_kb.json` under `"accounts"`.

### Full UI smoke test (claude-in-chrome)

This environment has no `chromium-cli` binary — drive the real Chrome
browser with the `mcp__claude-in-chrome__*` MCP tools instead. Load them
first if deferred:

```
ToolSearch: select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__tabs_close_mcp
```

Verified sequence (login → RAG query → grounded answer with citation):

1. `tabs_context_mcp {createIfEmpty:true}` → get a `tabId`.
2. `navigate {tabId, url:"http://localhost:5190"}`.
3. `computer {action:"screenshot", tabId}`. If it lands directly in the
   chat (already logged in — the extension's Chrome profile can carry a
   token in localStorage from a previous run), open the user menu (top
   right, click the username) → click "退出登录" to get a clean login
   screen for testing the actual login path.
4. On the login form, **click into the username field and select-all
   (triple-click) before typing** — Chrome's password manager can
   autofill a saved credential for an unrelated seed account (e.g.
   `alice`); don't submit that, overwrite both fields:
   - username: `qa_run_company_user`
   - password: `QaRun@2026`
5. Click "登录" → `computer {action:"screenshot", tabId}` → should show
   the chat screen ("你好！我是 RAG Agent").
6. Click the message input, type a question this account's KB covers,
   e.g. `年假可以顺延到次年几月？`, press `Return`.
7. `computer {action:"wait", duration:10, tabId}` — the right-hand
   "LANGGRAPH 实时追踪" panel shows live pipeline stages (意图解析 →
   知识库检索 → generate); wait for it to stop, then screenshot.
8. Expect a grounded answer with a citation card underneath (e.g.
   "顺延规则" / "最晚需在次年3月31日前休完"). A generic/empty answer, or
   a panel stuck on "等待中", means the backend or Postgres isn't
   actually reachable from the frontend — check `/tmp/ragent-*.log`.
9. `tabs_close_mcp {tabId}` when done — tabs you create are yours to
   clean up.

## Run (human path)

`start.bat` in the repo root is Windows-only and not usable here — it
does the same three `nohup`-equivalent launches as above (backend on
:8000, frontend on :5173) but with the *default* ports, which conflicts
with another project on this machine. Use the agent-path commands above
instead; they're the same commands a human would run manually on macOS.

## Gotchas

- **Non-default ports.** Backend is :8010 and frontend is :5190, not the
  8000/5173 that `start.bat` and most of the docs assume — see the
  comment in `.env` ("本机 8000/5173 被另一个项目占用"). `vite.config.js`
  has `strictPort: true`, so if 5190 is already taken the frontend
  process exits immediately instead of silently picking another port.
- **Frontend logs `ECONNREFUSED` on `/api/v1/workflows` etc. for the
  first couple seconds** if the frontend finishes booting before the
  backend does. Harmless and self-clears once the backend's `Uvicorn
  running on http://0.0.0.0:8010` line appears — don't chase it.
- **Gateway Monitor showing "连接失败"/"unreachable" for the two 考勤
  (attendance) connectors (or the two KB ones) almost always means
  `scripts/tenant_service_supervisor.py` was never started** — it's a
  separate process from the main backend, easy to forget when only
  starting backend+frontend. It self-heals anything it manages, but only
  while it's running.
- **Login page can autofill an unrelated saved password** (seen:
  username `alice` from Chrome's own password manager) when the login
  form first renders. Always select-all and overwrite both fields
  explicitly rather than trusting what's already in them.
- **Role model changed 2026-08-24**: the platform ("运营方") side now has
  only two global system roles, `super_admin`/`org_admin` (超级管理员/
  企业管理员) — the old `admin`/`user` (管理员/普通用户) tiers were
  retired. Don't expect 4 rows in the platform's "角色管理" page; 2 is
  correct.

## Troubleshooting

- **`curl http://localhost:8010/docs` refused**: backend isn't up yet or
  crashed — check `/tmp/ragent-backend.log`. Common cause: Postgres not
  running (`brew services start postgresql@16`).
- **`address already in use` on :8010/:5190**: a previous run's process
  is still listening — `lsof -tiTCP:8010 -sTCP:LISTEN | xargs -r kill`
  (swap the port) before relaunching.
- **Gateway connectors show `unreachable` for attendance/KB rows**: see
  the Gotchas entry above — start
  `scripts/tenant_service_supervisor.py`, wait ~5s, recheck.
- **`401` from `/api/v1/auth/login`**: wrong password, or the account
  doesn't exist in this DB — cross-check against
  `tests/fixtures/golden_test_set_tenant_kb.json`.
