# Manual Create Image Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a production WebUI choice between a dynamically selected project snapshot and a dynamically selected official Hetzner system image for manual creation of missing configured servers.

**Architecture:** Add authenticated catalog and validation helpers around the existing single-file Hetzner client, then extend the existing create endpoint without changing automatic rebuild behavior. Keep frontend catalog normalization and payload construction in a small browser/Node-compatible helper so the current Vue page can remain visually unchanged while its dynamic behavior is unit-tested.

**Tech Stack:** Python 3.13, FastAPI, requests, pytest, Vue 3 CDN page, browser-compatible JavaScript, Node.js built-in test runner, Playwright CLI, Docker Compose.

## Global Constraints

- Automatic traffic-limit rebuilds remain snapshot-only.
- Snapshot, official-image, server-type, location, and SSH-key selections are revalidated by the backend immediately before creation.
- Do not perform capacity prechecks; attempt creation in the selected location and then configured fallbacks.
- Official-image creation requires at least one project SSH key and never returns, logs, or persists a root password.
- Keep the existing `create-modal` size, theme variables, form controls, summary, warning, and button styles.
- Keep backward compatibility when `source` and `image_id` are omitted.
- Do not add production configuration, API tokens, credentials, private keys, `.superpowers/`, or `.playwright-cli/` to Git.
- Do not perform a real billable server creation during verification without separate user approval.

---

### Task 1: Dynamic Hetzner Creation Catalog

**Files:**
- Create: `test_manual_create_catalog.py`
- Modify: `production-main.py:1085-1280`

**Interfaces:**
- Produces: `HetznerClient.get_images(image_type: str) -> List[Dict[str, Any]]`
- Produces: `HetznerClient.get_server_types() -> List[Dict[str, Any]]`
- Produces: `HetznerClient.get_ssh_keys() -> List[Dict[str, Any]]`
- Produces: `_build_manual_create_catalog(name: str, config: Dict[str, Any], client: HetznerClient) -> Dict[str, Any]`
- Consumes: `_configured_missing_servers`, `_manual_create_options`, and `_request_paginated`

- [ ] **Step 1: Write failing paginated-resource and catalog-normalization tests**

Use a fake client with literal snapshot, system-image, server-type, server, and SSH-key fixtures. Assert that the catalog:

```python
assert catalog["name"] == "2"
assert catalog["default_source"] == "snapshot"
assert catalog["default_snapshot_id"] == 412977893
assert [row["id"] for row in catalog["snapshots"]] == [412977893, 412600001]
assert [row["name"] for row in catalog["system_images"]] == ["debian-12", "ubuntu-24.04"]
assert [row["name"] for row in catalog["server_types"]] == ["cx23", "cx33", "cx43", "cx53"]
assert catalog["locations"] == ["nbg1", "fsn1", "hel1"]
assert catalog["ssh_keys"] == [{"id": 77, "name": "shoo", "fingerprint": "SHA256:test"}]
```

Also assert that existing server names, disabled manual creation, unknown names, deprecated/unavailable images, deprecated types, and images with a non-matching architecture are rejected or omitted with stable error codes.

- [ ] **Step 2: Run focused tests and verify expected failures**

Run: `py -3.13 -m pytest test_manual_create_catalog.py -q`

Expected: failures because catalog client methods and `_build_manual_create_catalog` do not exist.

- [ ] **Step 3: Implement the minimal catalog methods and normalizer**

Implement `get_images` with only `snapshot` and `system` accepted, using `_request_paginated("images", "images", params={"type": image_type})`. Implement paginated `server_types` and `ssh_keys` accessors. Build JSON-safe rows with numeric IDs, stable names, labels, architecture, disk size, CPU count, memory, and disk. Preserve the configured location order and prefer the mapped snapshot ID when present.

- [ ] **Step 4: Run focused and existing manual-create tests**

Run: `py -3.13 -m pytest test_manual_create_catalog.py test_manual_create.py -q`

Expected: all catalog tests and existing snapshot-only tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- production-main.py test_manual_create_catalog.py
git commit -m "feat: expose dynamic manual create catalog"
```

### Task 2: Server-Side Image and SSH-Key Validation

**Files:**
- Modify: `test_manual_create.py`
- Modify: `test_manual_create_catalog.py`
- Modify: `production-main.py:1272-1402,2152-2235`

**Interfaces:**
- Extends: `HetznerClient.create_missing_server(name, config, server_type=None, preferred_location=None, allow_fallback=True, source=None, image_id=None, ssh_key_ids=None) -> Dict[str, Any]`
- Produces: `_validate_manual_create_selection(name, config, client, source, image_id, server_type, preferred_location, ssh_key_ids) -> Dict[str, Any]`
- Consumes: the current dynamic resources from Task 1

- [ ] **Step 1: Write failing official-image and validation tests**

Add literal fixtures proving:

```python
assert client.create_payloads == [{
    "name": "2",
    "server_type": "cx23",
    "image": 1001,
    "location": "nbg1",
    "start_after_create": True,
    "ssh_keys": [77],
}]
```

Add separate failures for wrong source type, missing image, deprecated image, architecture mismatch, snapshot disk larger than the selected type disk, foreign SSH-key ID, missing SSH key in system mode, and a server name that already exists. Assert no POST occurs for each invalid request. Add a regression test proving the old call without `source` or `image_id` still uses `snapshot_id_map` exactly as before.

- [ ] **Step 2: Run focused tests and verify expected failures**

Run: `py -3.13 -m pytest test_manual_create.py test_manual_create_catalog.py -q`

Expected: new source arguments or validation branches fail while original tests remain green.

- [ ] **Step 3: Implement validation and generalized creation**

Resolve the default source/image from `snapshot_id_map` only when the new fields are omitted. Otherwise fetch current images, server types, SSH keys, and servers, validate their IDs and compatibility, then construct the POST payload. Include SSH-key IDs only for official-image mode. Keep the existing ordered capacity-fallback loop and error payload shape.

- [ ] **Step 4: Redact password-like response data**

Return only the new server ID, public IPv4, selected image ID/source, server type, new location, and attempted locations. Do not copy `root_password`, authorization headers, or raw Hetzner response bodies into results, Telegram text, or exceptions.

- [ ] **Step 5: Run focused and complete Python tests**

Run: `py -3.13 -m pytest test_manual_create.py test_manual_create_catalog.py -q`

Run: `py -3.13 -m pytest -q`

Expected: all new and existing tests pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add -- production-main.py test_manual_create.py test_manual_create_catalog.py
git commit -m "feat: validate manual image source creation"
```

### Task 3: Authenticated Catalog and Create API

**Files:**
- Create: `test_manual_create_api.py`
- Modify: `production-main.py:3490-3620`

**Interfaces:**
- Produces: `GET /api/create_catalog?name=<name>`
- Extends: `POST /api/create_missing`
- Consumes: `_build_manual_create_catalog`, `_perform_manual_create`, and `_require_auth`

- [ ] **Step 1: Write failing API contract tests**

Use FastAPI `TestClient`, a temporary WebUI config, and a fake Hetzner client. Assert unauthenticated catalog requests return 401; an allowed missing name returns 200 with the Task 1 catalog; an existing or unknown name returns 400/409 with a stable code. Assert the POST route forwards exactly:

```python
{
    "source": "system",
    "image_id": 1001,
    "server_type": "cx23",
    "preferred_location": "nbg1",
    "allow_fallback": True,
    "ssh_key_ids": [77],
}
```

Add malformed tests for nonnumeric image IDs, non-list SSH keys, nonnumeric SSH-key IDs, and unsupported sources.

- [ ] **Step 2: Run API tests and verify expected failures**

Run: `py -3.13 -m pytest test_manual_create_api.py -q`

Expected: 404 for the missing catalog route and payload-forwarding failures for the current POST route.

- [ ] **Step 3: Implement the routes and status mapping**

Add the authenticated catalog route. Normalize numeric IDs before workflow invocation. Extend `_perform_manual_create` to forward the new values. Map invalid/stale selection errors to 400, existing/concurrent creation to 409, and upstream failures to the existing 500 behavior.

- [ ] **Step 4: Run API and complete Python tests**

Run: `py -3.13 -m pytest test_manual_create_api.py -q`

Run: `py -3.13 -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add -- production-main.py test_manual_create_api.py
git commit -m "feat: add manual create catalog API"
```

### Task 4: Original-Style Dynamic WebUI

**Files:**
- Create: `static/manual-create.js`
- Create: `test_manual_create_ui.js`
- Modify: `production-index.html:282-395,2170-2225,2240-2270,2510-2670,4239-4295`

**Interfaces:**
- Produces: `ManualCreate.normalizeCatalog(raw) -> Catalog`
- Produces: `ManualCreate.compatibleServerTypes(catalog, source, imageId) -> Array<ServerType>`
- Produces: `ManualCreate.buildPayload(dialog) -> Object`
- Consumes: `GET /api/create_catalog` and `POST /api/create_missing`

- [ ] **Step 1: Write failing Node tests for frontend state and payloads**

Create a CommonJS/browser-compatible helper contract. With literal fixtures assert source switching selects the mapped snapshot by default, official mode selects `debian-12` and `cx23`, a large snapshot filters out `cx23`, the official payload contains SSH-key IDs, and the snapshot payload does not include them.

```javascript
assert.deepEqual(buildPayload(dialog), {
  name: "2",
  source: "system",
  image_id: 1001,
  server_type: "cx23",
  preferred_location: "nbg1",
  allow_fallback: true,
  ssh_key_ids: [77],
});
```

- [ ] **Step 2: Run Node tests and verify expected failure**

Run: `node --test test_manual_create_ui.js`

Expected: module-not-found or missing-export failure because `static/manual-create.js` does not exist.

- [ ] **Step 3: Implement the pure frontend helper**

Use a small UMD-style module so Node can `require()` it and the browser receives `window.ManualCreate`. Normalize only JSON data, compute compatible types from `architecture`, `disk`, and `disk_size`, and build stable-ID request payloads without secrets.

- [ ] **Step 4: Run Node tests and verify green**

Run: `node --test test_manual_create_ui.js`

Expected: all helper tests pass.

- [ ] **Step 5: Update the existing Vue modal without restyling it**

Load `/static/manual-create.js`. Change the row button to `创建服务器`. Add a full-width `create-select` for source, then a conditional full-width snapshot or official-image select. Keep existing `create-form-grid`, `create-field`, `create-select`, `create-fallback-check`, `create-summary`, `create-warning`, and `btn` classes. Add only `align-items: start`, `align-content: start`, a full-width grid class, and loading/error text styles derived from existing variables.

- [ ] **Step 6: Connect catalog loading and submission**

Make `openCreateDialog` open a disabled loading state, fetch `/api/create_catalog?name=...`, normalize it, apply defaults, and show a specific error when a mode has no images or SSH keys. Recompute compatible types when source/image changes. Submit `ManualCreate.buildPayload(createDialog)` and refresh the catalog after a stale-selection response.

- [ ] **Step 7: Verify behavior in a real browser**

Serve the production page locally with workers disabled, open it using Playwright CLI, and verify both light and dark modes at desktop and mobile widths. Exercise snapshot/system switching, multiple image options, CX23 without parenthetical text, aligned specification/location controls, loading and disabled confirmation, summary changes, and no white native option background in dark mode. Save screenshots under `.playwright-cli/` only.

- [ ] **Step 8: Run all local verification and commit**

Run: `node --test test_manual_create_ui.js`

Run: `py -3.13 -m pytest -q`

Run: `docker compose config --quiet`

Run: `git diff --check`

Expected: all checks pass.

```powershell
git add -- production-index.html static/manual-create.js test_manual_create_ui.js
git commit -m "feat: add dynamic image source selector"
```

### Task 5: Production Deployment and Read-Only Verification

**Files:**
- Deploy as `/opt/hetzner-web/main.py`: `production-main.py`
- Deploy as `/opt/hetzner-web/static/index.html`: `production-index.html`
- Deploy as `/opt/hetzner-web/static/manual-create.js`: `static/manual-create.js`

**Interfaces:**
- Consumes: existing production `config.yaml`, `web_config.json`, and `/app/state`
- Produces: deployed catalog and extended manual-create endpoints

- [ ] **Step 1: Re-run the complete release gate**

Run: `node --test test_manual_create_ui.js`

Run: `py -3.13 -m pytest -q`

Run: `docker compose config --quiet`

Run: `git diff --check`

- [ ] **Step 2: Back up production files and state references**

Stop the application only when files are ready. Create a timestamped directory under `/opt/hetzner-web/backups/manual-create-image-sources-<timestamp>` and copy `main.py`, `static/index.html`, `docker-compose.yml`, and the current state summary. Do not copy or print secret configuration values.

- [ ] **Step 3: Deploy only tested artifacts**

Upload source files to `.codex-new` names, validate Python syntax and file presence, install them over the deployed paths, rebuild, and recreate the single `hetzner-web` service. Do not upload local `config.yaml`, `web_config.json`, `.superpowers/`, or `.playwright-cli/`.

- [ ] **Step 4: Verify production without creating a server**

Using server-local credentials without printing them, call `/api/create_catalog` for one missing configured name. Verify multiple snapshots when present, multiple official images, current server types including CX23 when returned, configured locations, and project SSH keys. Load the WebUI and verify source switching and styling. Confirm `/api/servers`, `/api/daily`, and `/api/qb` still return 200.

- [ ] **Step 5: Verify persistence and single ownership**

Confirm exactly one application container and one Uvicorn process, one `/app/state` mount, unchanged report-state first timestamp/count or higher count, and no new tracebacks, secret output, snapshot errors, JSON errors, or `Device or resource busy` messages.

- [ ] **Step 6: Do not run a real creation test automatically**

Report that catalog and UI verification are complete. A real snapshot or official-image creation is billable and requires a separate explicit user instruction naming the missing server, source, image, type, and location.

- [ ] **Step 7: Record final branch status without pushing or merging**

Run: `git status --short --branch`

Use `superpowers:finishing-a-development-branch` to present the integration choices after all verification passes.
