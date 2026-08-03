# Report State Durability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover the production traffic history and make report and threshold state survive concurrent workers, interrupted writes, and Docker recreation without silent resets.

**Architecture:** Keep the existing single-file application, but centralize report-state persistence behind a reentrant transaction lock and same-directory atomic JSON replacement. Move mutable Docker files beneath one directory mount so atomic replacement works, recover corrupt active state from the newest valid daily backup, and migrate production only after the tested image is ready.

**Tech Stack:** Python 3.13, FastAPI, pytest, Docker Compose, JSON files, PowerShell/SSH production deployment.

## Global Constraints

- Preserve all recoverable snapshots from the valid 2026-08-03 backup and the newer active file.
- Never silently convert corrupt persisted report state to `{}`.
- Never overwrite corrupt active state when no valid backup exists.
- Serialize complete report-state read-modify-write transactions, not only individual reads and writes.
- Store mutable state under `./state:/app/state` and atomically replace files inside that directory mount.
- Keep seven valid daily report-state backups.
- Do not delete any production recovery artifact.
- Do not stage the untracked `.playwright-cli/` directory.

---

### Task 1: Durable Report-State Persistence

**Files:**
- Create: `test_report_state_durability.py`
- Modify: `production-main.py:1-205`

**Interfaces:**
- Produces: `_load_report_state_unlocked() -> Dict[str, Any]`
- Produces: `_save_report_state_unlocked(state: Dict[str, Any]) -> None`
- Produces: `_update_report_state(mutator: Callable[[Dict[str, Any]], Any]) -> Any`
- Produces: `_load_report_state() -> Dict[str, Any]`
- Consumes: `REPORT_STATE_PATH`, `REPORT_STATE_BACKUP_DIR`, `REPORT_STATE_BACKUP_KEEP`

- [ ] **Step 1: Write failing atomic-save and corrupt-file recovery tests**

Create isolated temporary active and backup paths by assigning the module constants. Assert that `_save_report_state` leaves valid JSON and no temporary files, that a corrupt active file loads the newest valid backup, and that a corrupt active file with no valid backup raises instead of returning `{}`.

```python
def test_load_report_state_recovers_newest_valid_backup(tmp_path):
    configure_paths(tmp_path)
    Path(main.REPORT_STATE_PATH).write_text("{broken", encoding="utf-8")
    backups = Path(main.REPORT_STATE_BACKUP_DIR)
    backups.mkdir()
    (backups / "report_state.json.bak.20260801").write_text('{"hourly":{"old":{}}}', encoding="utf-8")
    (backups / "report_state.json.bak.20260802").write_text('{"hourly":{"new":{}}}', encoding="utf-8")
    assert main._load_report_state()["hourly"] == {"new": {}}

def test_load_report_state_refuses_silent_reset_without_backup(tmp_path):
    configure_paths(tmp_path)
    Path(main.REPORT_STATE_PATH).write_text("{broken", encoding="utf-8")
    with pytest.raises(main.ReportStateError):
        main._load_report_state()
```

- [ ] **Step 2: Run focused tests and verify the expected failures**

Run: `py -3.13 -m pytest test_report_state_durability.py -q`

Expected: failures because `ReportStateError` and safe backup recovery do not exist and the active writer is not atomic.

- [ ] **Step 3: Implement locked loading, valid-backup fallback, atomic save, and transaction helper**

Add `REPORT_STATE_LOCK = threading.RLock()`, `ReportStateError`, validated JSON-object loading, newest-valid-backup selection, and a same-directory temporary writer using `tempfile.mkstemp`, `flush`, `os.fsync`, and `os.replace`. `_update_report_state` must hold the reentrant lock across load, mutation, backup, and save. `_backup_report_state` must parse the active file before copying it and retain seven daily files.

- [ ] **Step 4: Run focused tests and verify they pass**

Run: `py -3.13 -m pytest test_report_state_durability.py -q`

Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- production-main.py test_report_state_durability.py
git commit -m "fix: make report state persistence durable"
```

### Task 2: Convert Writers to Transactions and Correct the Tracking Start

**Files:**
- Modify: `test_report_state_durability.py`
- Modify: `production-main.py:189-205,822-853,1870-1935,2598-2633,2872-2874,3319-3334,3435-3694`

**Interfaces:**
- Consumes: `_update_report_state(mutator)` from Task 1
- Produces: all API readers calling `_load_report_state()`
- Produces: `_compute_tracking_totals()` whose `start` never predates the first included snapshot

- [ ] **Step 1: Write failing concurrency and tracking-start tests**

Use multiple threads that repeatedly increment the same persisted counter through `_update_report_state`; assert the final value equals the exact thread-count times iteration-count so any lost read-modify-write update fails the test without introducing a lock-internal barrier deadlock. Add literal tracking fixtures proving a configured July 1 start is clamped to the actual July 28 first sample, while a configured in-range start is retained.

```python
def test_tracking_start_does_not_predate_available_history():
    hourly = {
        "2026-07-28 09:25": {"1": {"name": "1", "outbound_bytes": 100, "inbound_bytes": 20}},
        "2026-07-28 09:30": {"1": {"name": "1", "outbound_bytes": 150, "inbound_bytes": 30}},
    }
    result = main._compute_tracking_totals(hourly, "2026-07-01 00:00")
    assert result == {"start": "2026-07-28 09:25", "outbound_tb": "0.000", "inbound_tb": "0.000"}
```

- [ ] **Step 2: Run focused tests and verify expected failures**

Run: `py -3.13 -m pytest test_report_state_durability.py -q`

Expected: the tracking-start assertion fails and at least one writer/concurrency behavior remains unprotected.

- [ ] **Step 3: Convert every report-state writer and reader**

Wrap rebuild event updates, manual report updates, snapshot updates, report reset, and startup backfill in `_update_report_state`. Replace API `_load_json(REPORT_STATE_PATH)` calls with `_load_report_state()`. Keep network calls outside the lock where practical, but never release the lock between loading persistent state and committing the mutation.

- [ ] **Step 4: Clamp the effective tracking start**

Set `start_label` to the first actual included snapshot key when `start_override <= keys[0]`; retain the configured value only when it falls after the first available key and before or at the last key.

- [ ] **Step 5: Run focused and complete tests**

Run: `py -3.13 -m pytest test_report_state_durability.py -q`

Run: `py -3.13 -m pytest -q`

Expected: all tests pass, including the original 40-test baseline.

- [ ] **Step 6: Commit Task 2**

```powershell
git add -- production-main.py test_report_state_durability.py
git commit -m "fix: serialize report state updates"
```

### Task 3: Directory-Mounted Mutable State

**Files:**
- Modify: `docker-compose.yml`
- Modify: `test_report_state_durability.py`
- Modify: `.gitignore` only if a repository-local `state/` directory is not already ignored

**Interfaces:**
- Produces: `/app/state/report_state.json`
- Produces: `/app/state/report_state_backups`
- Produces: `/app/state/threshold_state.json`

- [ ] **Step 1: Write a failing Compose behavior test**

Parse `docker-compose.yml` with PyYAML and assert the service has the three exact state-path environment values and exactly one `./state:/app/state` mutable-state mount, with no individual mounts targeting the three old container paths.

- [ ] **Step 2: Run the Compose test and verify it fails**

Run: `py -3.13 -m pytest test_report_state_durability.py -q`

Expected: failure because Compose still mounts individual files.

- [ ] **Step 3: Update Docker Compose**

Add `REPORT_STATE_PATH`, change `REPORT_STATE_BACKUP_DIR`, add `THRESHOLD_STATE_PATH`, replace the three state mounts with `./state:/app/state`, and preserve all configuration mounts and ports unchanged.

- [ ] **Step 4: Validate Compose and run all tests**

Run: `docker compose config --quiet`

Run: `py -3.13 -m pytest -q`

Expected: Compose exits zero and all tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add -- docker-compose.yml test_report_state_durability.py .gitignore
git commit -m "fix: mount mutable state as a directory"
```

### Task 4: Production Recovery, Deployment, and Restart Proof

**Files:**
- Deploy: `production-main.py`
- Deploy: `docker-compose.yml`
- Create on server: `/opt/hetzner-web/state/`
- Create on server: `/opt/hetzner-web/backups/report-state-durability-<timestamp>/`

**Interfaces:**
- Consumes: valid `/opt/hetzner-web/report_state_backups/report_state.json.bak.20260803`
- Consumes: active `/opt/hetzner-web/report_state.json`
- Produces: merged `/opt/hetzner-web/state/report_state.json`

- [ ] **Step 1: Re-run local release verification**

Run: `git diff --check HEAD~3..HEAD`

Run: `py -3.13 -m pytest -q`

Run: `docker compose config --quiet`

Expected: zero failures and zero Compose errors.

- [ ] **Step 2: Stop production and create recovery copies**

Stop only the `hetzner-web` Compose service. Create a timestamped recovery directory and copy the old Compose file, active report state, threshold state, and report backup directory into it. Confirm the target recovery path is under `/opt/hetzner-web/backups` before copying.

- [ ] **Step 3: Merge and validate state while stopped**

Run a reviewed Python migration script on the server that loads both JSON objects, overlays active `hourly` entries on the backup `hourly`, overlays only non-empty active metadata, writes to a temporary file under `/opt/hetzner-web/state`, `fsync`s, replaces the target, and prints the count plus first/last keys. Expect at least 1454 backup snapshots plus all non-overlapping active snapshots.

- [ ] **Step 4: Deploy code and Compose, build, and start**

Copy only the tested source and Compose files, build the service, and start it. Do not copy local `config.yaml`, `web_config.json`, secrets, or `.playwright-cli/`.

- [ ] **Step 5: Verify live APIs and single-process ownership**

Authenticate using a temporary cookie mechanism that does not print the password. Verify `/api/servers`, `/api/hourly`, `/api/daily`, and `/api/cycle`; verify daily rows span the recovered dates. Check Docker and host process/service listings for exactly one active application container and no legacy monitor.

- [ ] **Step 6: Prove restart persistence**

Record snapshot count and first/last keys, recreate the container, then re-read the state. Assert the first key and snapshot count did not regress, the container starts cleanly, and a later snapshot appends normally.

- [ ] **Step 7: Inspect error logs**

Search fresh container logs for `report state`, `snapshot error`, JSON decode errors, `Device or resource busy`, and tracebacks. Any match must be explained or fixed before completion.

- [ ] **Step 8: Record final Git status without pushing**

Run: `git status --short --branch`

Do not push or merge unless the user explicitly requests it after the production verification.
