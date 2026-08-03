# Report State Recovery and Durability Design

Date: 2026-08-03

## Goal

Restore the available traffic history on the production Hetzner-Web instance and prevent report or threshold state from being reset, truncated, or lost during concurrent background work, process interruption, or Docker container recreation.

## Confirmed Failure

The active production `report_state.json` starts at 2026-08-03 04:25 and contains only the `hourly` key. A valid daily backup from 2026-08-03 contains 1454 snapshots from 2026-07-28 09:25 through 2026-08-02 23:55 plus rebuild and report metadata. The container was recreated at 04:26 without a host reboot.

The application currently starts the snapshot and rebuild-backfill workers concurrently. Each worker performs an unlocked whole-file read, modifies its private copy, and performs an unlocked in-place whole-file write. A reader can therefore observe a truncated JSON file, silently convert the parse failure to `{}`, and then overwrite the valid state. Even if individual reads and writes were locked, separate read and write calls would still allow lost updates between workers.

The active files are also mounted as individual Docker bind-mounted files. Replacing such a mount point atomically can fail with `Device or resource busy`, which was already observed for threshold state.

## Selected Design

### 1. Recover the maximum valid history

Stop the application before changing state. Preserve timestamped copies of both the active state and the selected backup. Build a merged state from:

- all top-level metadata and snapshots in `report_state.json.bak.20260803`;
- all newer snapshot keys from the current active `report_state.json`, with current entries winning on an identical timestamp;
- current non-empty top-level metadata winning only when it exists.

Validate the merged file as JSON and verify that snapshot keys are ordered, the first and last timestamps are expected, and all retained snapshots are dictionaries. The known gap between 2026-08-02 23:55 and 2026-08-03 04:25 cannot be reconstructed, but the first post-gap sample still allows the accumulated traffic during that interval to be attributed to 2026-08-03.

The damaged July 31 recovery file will be retained for evidence but will not be merged. No valid data before 2026-07-28 09:25 is currently available.

### 2. Move mutable state to a directory bind mount

Use one host directory, `./state`, mounted as `/app/state`. Configure:

- `REPORT_STATE_PATH=/app/state/report_state.json`
- `REPORT_STATE_BACKUP_DIR=/app/state/report_state_backups`
- `THRESHOLD_STATE_PATH=/app/state/threshold_state.json`

Remove the individual report-state, report-backup, and threshold-state mounts after their data has been copied into the new directory. Configuration files remain mounted as they are today.

This makes the target file replaceable inside a mounted directory and fixes the threshold-state `Device or resource busy` failure at the same time.

### 3. Serialize complete state transactions

Introduce a process-wide reentrant report-state lock and a single update helper that holds the lock across the complete load, mutate, backup, and save transaction. Convert every report-state writer to this helper:

- traffic snapshot collection;
- rebuild event recording;
- rebuild-history backfill;
- manual/daily report state updates;
- explicit report reset.

Read-only API endpoints use the same protected loader. This prevents partial reads and prevents two valid writers from overwriting one another's changes.

### 4. Save atomically and fail closed

Write JSON to a uniquely named temporary file in the same directory, flush it, call `fsync`, and replace the destination with `os.replace`. Best-effort directory `fsync` follows the replacement on supported platforms. Temporary files are removed after failure.

Before replacement, create the existing daily backup only from a file that parses successfully. Increase backup retention from three to seven daily copies.

Loading behavior becomes:

- missing active file: return an empty state for a genuine first installation;
- valid active file: return it;
- invalid active file with a valid backup: log the recovery and use the newest valid backup;
- invalid active file with no valid backup: raise an error and do not save `{}` over the damaged file.

This removes the current dangerous `except: return {}` behavior.

### 5. Report the real available start time

When the configured `tracking_start` predates the oldest available snapshot, return the oldest available snapshot as the effective displayed start. This prevents the page from claiming that totals cover July 1 when the earliest recoverable data begins July 28.

The configured date is still honored when it falls inside the available data range. If desired later, the UI can display configured and available dates separately; this repair keeps the API shape unchanged.

## Tests

Add regression tests for:

- atomic report-state save and reload;
- recovery from a corrupt active file using the newest valid backup;
- refusal to replace a corrupt active file when no valid backup exists;
- rejection of an invalid backup during backup creation;
- concurrent state updates preserving both mutations;
- report reset through the transaction helper;
- effective tracking start clamped to the first available sample;
- Docker Compose using the directory mount and state-path environment variables.

Run the complete local test suite after the focused tests.

## Production Migration and Verification

1. Stop the container and confirm no legacy service/process is writing the state.
2. Copy existing state artifacts to a timestamped recovery directory.
3. Create and validate the merged state under `./state`.
4. Copy the threshold state and valid report backups into `./state`.
5. Deploy the tested code and updated Compose file, then recreate the container.
6. Verify one running container and no legacy monitoring service/process.
7. Verify authenticated `/api/servers`, `/api/hourly`, `/api/daily`, and `/api/cycle` responses.
8. Confirm the daily history includes July 28 through August 3 and that cycle, today, and month totals are no longer all derived from a single day.
9. Record the snapshot count and state hash, recreate the container once more, and verify the history is retained and new snapshots continue to append.
10. Inspect container logs for state recovery, JSON parsing, `Device or resource busy`, and snapshot errors.

## Rollback

The pre-migration recovery directory preserves the old active file, backups, Compose file, and threshold file. Rollback stops the container, restores the previous Compose file and individual mounts, restores the saved files, and starts the previous image. No recovery artifact is deleted during this change.
