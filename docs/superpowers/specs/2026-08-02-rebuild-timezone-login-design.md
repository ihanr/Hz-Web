# Rebuild timestamp compatibility and login error design

## Problem

The authenticated `/api/servers` request fails with HTTP 500 after a new rebuild record is written. Historical backfilled rebuild timestamps have no UTC offset, while newly recorded timestamps include a UTC offset. Python refuses to order offset-naive and offset-aware `datetime` values. The login page currently labels every non-success response as a login failure, hiding this server error.

## Considered approaches

1. Normalize timestamps when reading and comparing them. This is the selected approach because it supports existing mixed data and future records without rewriting persisted state.
2. Rewrite the current `report_state.json` once. This would restore the current deployment but could fail again when old backups or imported state are used.
3. Remove timezone information from new records. This avoids the exception but discards useful timezone semantics and perpetuates ambiguous timestamps.

## Backend behavior

- Parse each `last_time_iso` value as today.
- Treat a timestamp without an offset as local server time and attach the current local timezone.
- Convert offset-aware timestamps to the same local timezone before comparison.
- Ignore malformed timestamps as before.
- Do not modify `report_state.json` as part of the read path.

## Frontend behavior

- HTTP 401 or 403 from `/api/servers` remains `登录失败`.
- Other HTTP failures show a server/request failure message instead of claiming the password is wrong.
- Network failures continue to show the existing network-error message.

## Tests and deployment

- Add a regression test containing one naive and one aware rebuild timestamp and assert that summary generation succeeds and selects the newest event.
- Add frontend source assertions covering the status distinction.
- Run the complete test suite and Python compilation.
- Back up the production backend and frontend before deployment.
- Rebuild the Docker service, verify `/api/servers` with the configured credentials without printing them, confirm HTTP 200 and servers 1–5, and inspect recent logs for exceptions.

## Scope

No credentials, Hetzner rebuild policy, traffic thresholds, DNS settings, or persisted report records are changed.
