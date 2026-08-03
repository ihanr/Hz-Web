# Manual Create Image Sources Design

Date: 2026-08-04

## Goal

Allow the existing WebUI action for a missing configured server to create it either from a user-selected project snapshot or from a user-selected official Hetzner system image, while preserving the current location fallback, duplicate-name protection, DNS synchronization, Telegram notification, and visual style.

Automatic traffic-limit rebuilds remain snapshot-only. This change affects only the manual creation flow for a missing server.

## User Interface

The existing `create-modal` remains the visual foundation. It keeps the current 540-pixel width, color variables, dark/light themes, typography, borders, buttons, warning block, summary block, and two-column form layout.

A full-width `创建来源` select is added above the existing fields:

- `从指定快照创建`
- `从官方系统镜像全新创建`

Snapshot mode displays a full-width snapshot select. Official-image mode displays a full-width official-image select. The existing server-type and preferred-location selects remain below them and stay vertically aligned. No new card-style selector, white dropdown, or separate visual language is introduced.

The confirmation summary includes source, image, server type, preferred location, and fallback order. The warning changes with the source:

- snapshot: the selected snapshot restores its existing system and application environment;
- official image: the server is a clean installation and does not contain qBittorrent, old configuration, or old data.

The row action text changes from `从快照创建` to `创建服务器`, because the source is chosen inside the modal.

## Dynamic Catalog

Opening the modal requests an authenticated creation catalog from a dedicated endpoint. The dashboard refresh endpoint is not extended with catalog calls, avoiding extra Hetzner API latency on every statistics refresh.

The catalog contains:

- all usable project snapshots returned by Hetzner `GET /images?type=snapshot`;
- all usable official system images returned by `GET /images?type=system`;
- all non-deprecated server types returned by `GET /server_types`;
- configured creation locations in their existing fallback order;
- all project SSH keys returned by `GET /ssh_keys` for official-image creation;
- the machine's configured snapshot ID as the default when it is still present.

Catalog labels are presentation data only. Creation requests submit stable IDs. Snapshot labels show description, ID, and creation date. Official-image labels show distribution, version, architecture, and image ID. Server-type labels show the API name and available CPU, memory, and disk values. `CX23` is displayed exactly as `CX23`, without a `CX22` parenthetical suffix.

Catalog entries are filtered for static compatibility:

- image architecture must match the server type architecture;
- a snapshot's image disk size must fit the server type disk;
- deprecated or unavailable image records are omitted;
- official images and server types come from the current API response rather than hardcoded lists.

The application does not perform capacity prechecks. Actual creation is attempted in the selected location and then the configured fallback locations, because a capacity check can disagree with a real create request.

## API and Validation

Add an authenticated read-only endpoint:

`GET /api/create_catalog?name=<configured-server-name>`

It returns the filtered catalog and defaults only when the requested name is an allowed missing-server name.

Extend `POST /api/create_missing` with:

- `source`: `snapshot` or `system`;
- `image_id`: numeric Hetzner image ID;
- `server_type`: selected server-type name;
- `preferred_location`: selected location name;
- `allow_fallback`: boolean;
- `ssh_key_ids`: numeric SSH-key IDs, required for official-image mode.

For backward compatibility, an omitted `source` and `image_id` preserve the current behavior: use the configured snapshot mapping for the named server.

The server never trusts catalog values returned earlier to the browser. Immediately before creation it reloads the allowed server name and current Hetzner image, server-type, location, and SSH-key records, then validates:

- the server name is allowed and does not already exist;
- source is one of the two supported values;
- the selected image exists and has the requested source type;
- the selected type is non-deprecated and compatible with the image;
- the preferred and fallback locations remain within configured locations;
- every submitted SSH-key ID belongs to the project;
- official-image creation has at least one selected SSH key.

The Hetzner create payload uses the validated numeric image ID and SSH-key IDs. The application must not display, log, return, or persist a generated root password. Requiring an SSH key for official-image creation prevents password-based handoff through the WebUI.

## Configuration

Existing `rebuild.snapshot_id_map` remains the missing-server name allowlist and supplies the default snapshot for each name. No existing mapping is removed.

Existing `rebuild.location_fallbacks` remains the allowed ordered location list.

Existing `rebuild.manual_create.enabled` remains the feature switch. Its configured server types are treated as preferred defaults for snapshot mode, but the catalog may include additional API-compatible types such as `CX23` for official-image mode. Server-side compatibility validation remains authoritative.

No API token, WebUI credential, Telegram token, Cloudflare token, private key, or production `config.yaml` is added to Git.

## Creation Flow

1. The user clicks `创建服务器` on a missing-server row.
2. The modal opens in a loading state and requests the catalog for that name.
3. Snapshot mode defaults to the name's configured snapshot when available. Official-image mode defaults to the first current compatible official image and compatible server type, with `CX23` available when returned by the project API.
4. Changing image or source recomputes compatible server types and updates the summary.
5. Submission sends stable IDs and the selected fallback preference.
6. The backend revalidates all selections against current Hetzner API data.
7. Creation attempts the preferred location, followed by the configured fallback locations when enabled.
8. On success, the existing Cloudflare update and Telegram success paths run unchanged.
9. When all locations return capacity errors, send the existing failure notification and do not retry automatically.

## Error Handling

- Catalog request failure: keep the modal open, show a service error, and disable confirmation.
- Empty snapshots: disable snapshot mode but leave official-image mode usable.
- Empty official images or SSH keys: disable official-image mode with a specific explanation.
- Stale or invalid selection: return HTTP 400 with a stable error code and refresh the catalog in the UI.
- Existing server with the same name or concurrent manual creation: retain HTTP 409 behavior.
- Hetzner capacity exhaustion: retain attempted-location details and HTTP 412-derived error behavior.
- Other Hetzner errors: return sanitized messages without credentials or root passwords.

## Testing

Backend tests cover catalog pagination and normalization, source filtering, snapshot disk compatibility, architecture compatibility, SSH-key ownership, stale selections, backward-compatible snapshot requests, official-image create payloads, fallback order, and secret redaction.

Frontend tests or extracted pure-function tests cover source switching, dynamic options, default selection, incompatible-type filtering, loading and disabled states, aligned two-column fields, summaries, and request payloads.

The full existing test suite and Docker Compose validation must pass. Production verification checks both catalog modes without creating a server, then performs only a user-approved real creation test. Deployment must preserve production configuration and mutable state.

## Out of Scope

- Changing automatic traffic-limit rebuilds to official images.
- Installing qBittorrent or restoring application configuration after an official-image installation.
- Capacity prechecks or automatic retry after all configured locations fail.
- Creating arbitrary server names that are not already represented by the configured missing-server allowlist.
