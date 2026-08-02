# Rebuild Timestamp Compatibility and Login Error Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore authenticated server-list requests when rebuild history contains mixed timezone formats, and stop presenting server-side failures as bad credentials.

**Architecture:** Normalize every parsed rebuild timestamp to the server's local timezone at the comparison boundary, leaving persisted report data unchanged. Keep the single-file Vue UI but branch login errors by HTTP status: 401/403 are credential failures and other non-success responses are service failures.

**Tech Stack:** Python 3.11+, FastAPI, pytest/unittest, Vue 3 single-file CDN application, Docker Compose, Playwright for browser behavior verification.

## Global Constraints

- Do not modify `report_state.json`, credentials, Hetzner rebuild policy, traffic thresholds, or DNS settings.
- Do not include `config.yaml`, `web_config.json`, tokens, passwords, or SSH keys in commits.
- Production verification must not create, delete, or rebuild any Hetzner server.

---

### Task 1: Normalize mixed rebuild timestamps

**Files:**
- Create: `test_rebuild_timestamp_summary.py`
- Modify: `production-main.py:208-233`

**Interfaces:**
- Consumes: `state["rebuild_stats"][name]["last_time_iso"]` strings in ISO-8601 form.
- Produces: `_parse_rebuild_timestamp(value: Any) -> Optional[datetime]`, returning an aware datetime in local server time, or `None` for malformed values.

- [ ] **Step 1: Write the failing regression test**

```python
def test_summary_orders_naive_and_aware_rebuild_timestamps():
    state = {
        "rebuild_stats": {
            "2": {
                "count": 1,
                "last_time": "2026-08-01 08:00",
                "last_time_iso": "2026-08-01T08:00:00",
                "last_source": "历史回填",
            },
            "1": {
                "count": 1,
                "last_time": "2026-08-02 04:19:25",
                "last_time_iso": "2026-08-02T04:19:25+08:00",
                "last_source": "流量超标自动重建",
            },
        }
    }

    summary = main._summarize_rebuild_stats(state)

    assert summary["last"]["server"] == "1"
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `py -m pytest -q test_rebuild_timestamp_summary.py`

Expected: FAIL with `TypeError: can't compare offset-naive and offset-aware datetimes`.

- [ ] **Step 3: Implement the minimal normalization helper**

```python
def _parse_rebuild_timestamp(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except Exception:
        return None
    local_tz = _now_local().tzinfo
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)
```

Update `_summarize_rebuild_stats()` to call the helper and skip `None` values.

- [ ] **Step 4: Run focused and full tests**

Run: `py -m pytest -q test_rebuild_timestamp_summary.py`

Expected: PASS.

Run: `py -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the backend regression fix**

```powershell
git add -- production-main.py test_rebuild_timestamp_summary.py
git commit -m "fix: normalize rebuild timestamps before comparison"
```

### Task 2: Distinguish authentication and service failures

**Files:**
- Modify: `production-index.html:2498-2502`
- Modify: `production-index.html:2630-2634`
- Modify: `production-index.html:3040-3059`

**Interfaces:**
- Consumes: HTTP status from `fetch("/api/servers")`.
- Produces: localized `loginFailed` only for 401/403 and localized `serviceError` for other non-success statuses.

- [ ] **Step 1: Define behavior cases before editing**

Use these literal cases for browser verification:

```text
401 -> 登录失败。
403 -> 登录失败。
500 -> 服务暂时不可用，请稍后重试。
network rejection -> 网络错误。
```

- [ ] **Step 2: Add localized service-error strings and the status branch**

```javascript
if (!res.ok) {
  this.error = res.status === 401 || res.status === 403
    ? this.langPack.loginFailed
    : this.langPack.serviceError;
  return;
}
```

Add matching English and Chinese `serviceError` translations beside `loginFailed` and `networkError`.

- [ ] **Step 3: Verify behavior in a real browser**

Load the real page with Playwright. Intercept `/api/servers` once with 401 and once with 500, submit the login form, and assert the visible Chinese message matches the cases above. Do not persist entered credentials or print them.

- [ ] **Step 4: Run syntax and diff checks**

Run: `py -m py_compile production-main.py`

Run: `git diff --check`

Expected: both exit 0.

- [ ] **Step 5: Commit the frontend behavior fix**

```powershell
git add -- production-index.html
git commit -m "fix: distinguish login and service errors"
```

### Task 3: Deploy and verify production

**Files:**
- Deploy: `production-main.py` to `/opt/hetzner-web/main.py`
- Deploy: `production-index.html` to `/opt/hetzner-web/index.html`

**Interfaces:**
- Consumes: the existing Docker Compose deployment and mounted configuration files.
- Produces: HTTP 200 from authenticated `/api/servers` and an unchanged live server inventory.

- [ ] **Step 1: Run final local verification**

Run: `py -m pytest -q`

Run: `py -m py_compile production-main.py`

Run: `git diff --check`

Expected: all commands exit 0.

- [ ] **Step 2: Back up live files and validate persistent state**

Create a timestamped directory under `/opt/hetzner-web/backups/`, copy `main.py` and `index.html`, and run `python3 -m json.tool report_state.json` before replacement.

- [ ] **Step 3: Upload, compile, and rebuild Docker**

Upload both files to temporary names, verify SHA-256, run Python compilation, install them atomically, run `docker compose config -q`, then `docker compose up -d --build`.

- [ ] **Step 4: Verify the original failure is gone**

Using credentials loaded inside the container from `/app/web_config.json`, request `/api/servers` without printing the credentials. Require HTTP 200, assert live server names are `1`, `2`, `3`, `4`, and `5`, validate `report_state.json`, and inspect recent logs for `TypeError`, traceback, or HTTP 500.

- [ ] **Step 5: Push intended commits to the user's GitHub main branch**

Fetch `personal/main`, verify it is an ancestor, inspect the changed-file list for secrets/configuration, and push `HEAD:main` only after production verification succeeds.
