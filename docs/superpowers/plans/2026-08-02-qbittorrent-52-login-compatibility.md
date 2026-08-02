# qBittorrent 5.2 Login Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Hetzner-Web collect qBittorrent statistics from both legacy `200 + Ok.` login responses and qBittorrent 5.2's successful `204 No Content` responses.

**Architecture:** Add one focused response classifier at the qB login boundary and reuse it both when deciding whether to stop retrying and when deciding whether login ultimately failed. Keep `requests.Session` cookie handling and the existing `/api/v2/sync/maindata` validation unchanged.

**Tech Stack:** Python 3.11+, requests, FastAPI, pytest, Docker Compose.

## Global Constraints

- Only `production-main.py` and focused regression tests change for runtime behavior.
- Do not modify qBittorrent settings, credentials, cookies, Hetzner resources, traffic policy, DNS settings, or persisted report state.
- Never print credentials or cookie values during verification.
- Production verification is read-only against qBittorrent and Hetzner APIs.

---

### Task 1: Add qBittorrent 5.2 login-response compatibility

**Files:**
- Create: `test_qbittorrent_login_compatibility.py`
- Modify: `production-main.py:299-355`

**Interfaces:**
- Consumes: `Optional[requests.Response]` from `/api/v2/auth/login`.
- Produces: `_qb_login_succeeded(response: Optional[requests.Response]) -> bool`.
- Preserves: `_fetch_qb_instance(instance, counter_mode)` result shape and existing retry behavior.

- [ ] **Step 1: Write the failing 204 regression test and compatibility cases**

```python
def test_qb_52_login_204_collects_sync_totals(monkeypatch):
    session = FakeSession(
        login_response=response(204, ""),
        sync_response=json_response(
            200,
            {
                "server_state": {
                    "alltime_ul": 123,
                    "alltime_dl": 456,
                    "up_info_data": 12,
                    "dl_info_data": 34,
                    "up_info_speed": 5,
                    "dl_info_speed": 6,
                    "connection_status": "connected",
                }
            },
        ),
    )
    monkeypatch.setattr(main.requests, "Session", lambda: session)

    result = main._fetch_qb_instance(instance(), "alltime")

    assert result["status"] == "ok"
    assert result["upload_bytes"] == 123
    assert result["download_bytes"] == 456


def test_legacy_200_ok_remains_successful(monkeypatch):
    session = FakeSession(response(200, "Ok."), valid_sync_response())
    monkeypatch.setattr(main.requests, "Session", lambda: session)
    assert main._fetch_qb_instance(instance(), "alltime")["status"] == "ok"


def test_unexpected_200_body_remains_login_failure(monkeypatch):
    session = FakeSession(response(200, "Fails."), valid_sync_response())
    monkeypatch.setattr(main.requests, "Session", lambda: session)
    result = main._fetch_qb_instance(instance(), "alltime")
    assert result["status"] == "error"
    assert result["error"].startswith("login_failed:")
```

The test helpers use real `requests.Response` objects. `FakeSession` replaces only external HTTP transport and returns the complete response structures consumed by production code.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `py -m pytest -q test_qbittorrent_login_compatibility.py`

Expected: the qBittorrent 5.2 case fails with `login_failed: status=204`; legacy and rejected-body cases pass.

- [ ] **Step 3: Implement the minimal response classifier**

```python
def _qb_login_succeeded(response: Optional[requests.Response]) -> bool:
    if response is None:
        return False
    if response.status_code == 204:
        return True
    return (
        response.status_code == 200
        and response.text.strip().lower().startswith("ok")
    )
```

Use `_qb_login_succeeded(login)` for the retry-loop `break` condition and the final failure condition. Do not inspect cookie names or relax any other response status.

- [ ] **Step 4: Run focused and full tests**

Run: `py -m pytest -q test_qbittorrent_login_compatibility.py`

Expected: 3 tests pass.

Run: `py -m pytest -q`

Expected: the complete suite passes.

- [ ] **Step 5: Commit the compatibility fix**

```powershell
git add -- production-main.py test_qbittorrent_login_compatibility.py
git commit -m "fix: accept qBittorrent 5.2 login responses"
```

### Task 2: Deploy and verify live qBittorrent collection

**Files:**
- Deploy: `production-main.py` to `/opt/hetzner-web/main.py`

**Interfaces:**
- Consumes: existing `/opt/hetzner-web/config.yaml` and Docker Compose service.
- Produces: qB status `connected` with transfer totals for configured instances 1, 2, 3, and 4.

- [ ] **Step 1: Run final local checks**

Run: `py -m pytest -q`

Run: `py -m py_compile production-main.py`

Run: `git diff --check`

Expected: all exit 0.

- [ ] **Step 2: Validate and back up production**

Confirm the Docker service is running and `report_state.json` is valid JSON. Create a timestamped backup directory under `/opt/hetzner-web/backups/` and copy the current `main.py`. Do not read or replace configuration files.

- [ ] **Step 3: Upload and rebuild**

Upload `production-main.py` under a temporary name, verify its SHA-256 and Python compilation, install it as `main.py`, run `docker compose config -q`, and run `docker compose up -d --build`.

- [ ] **Step 4: Verify the original symptom through Hetzner-Web**

Load Web credentials inside the container without printing them and request `/api/qb`. Require HTTP 200. For instances 1, 3, and 4, require status `ok`, connection status `connected`, and numeric upload/download totals. Confirm instance 2 remains `ok`.

- [ ] **Step 5: Verify runtime safety**

Validate `report_state.json`, confirm the legacy systemd service remains inactive and disabled, and inspect recent Docker logs for `login_failed: status=204`, traceback, or HTTP 500.

- [ ] **Step 6: Push intended commits**

Fetch `personal/main`, verify it is an ancestor of the current branch, inspect `git diff --name-status personal/main..HEAD` to ensure no configuration or secret files are included, then push `HEAD:main` to the user's `ihanr/Hetzner-Web` repository.
