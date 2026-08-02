# qBittorrent 5.2 login compatibility design

## Problem

Hetzner-Web treats qBittorrent login as successful only when `/api/v2/auth/login` returns HTTP 200 and a body beginning with `Ok.`. Live qBittorrent 5.2.2 instances 1, 3, and 4 return HTTP 204 with an empty body, set a port-specific `QBT_SID_9090` cookie, and then return valid transfer data. Instance 2 on qBittorrent 5.0.4 still returns HTTP 200, `Ok.`, and the legacy `SID` cookie.

## Considered approaches

1. Explicitly accept the two known successful login shapes. This is selected because it preserves the existing failure detection while adding only the verified qBittorrent 5.2 behavior.
2. Accept every 2xx login response and rely only on the next API call. This is broader but can temporarily classify an unexpected 200 response body as success and weakens current diagnostics.
3. Downgrade qBittorrent 5.2.2. This avoids changing Hetzner-Web but loses current qBittorrent fixes and leaves the application incompatible with the new API behavior.

## Login behavior

A login attempt succeeds when either condition is true:

- HTTP 200 and the trimmed response body starts with `Ok.` case-insensitively.
- HTTP 204, with no response-body requirement.

HTTP 403, network errors, and every other response continue through the existing retry behavior. If all attempts fail, the current `login_failed` result is retained.

`requests.Session` continues to manage cookies without checking their names. This supports both `SID` and qBittorrent 5.2's port-specific `QBT_SID_<port>` cookie.

## Post-login validation

After either successful login response, Hetzner-Web continues to request `/api/v2/sync/maindata`. A failure in that call remains an instance failure, so accepting HTTP 204 does not bypass actual connectivity or session validation.

## Tests

- Reproduce a qBittorrent 5.2 login returning 204 and valid transfer/sync payloads; require connected totals.
- Retain coverage for the existing 200 plus `Ok.` behavior.
- Verify an unexpected 200 body is still rejected rather than treated as success.
- Run the complete local suite and a live read-only check for instances 1, 3, and 4 after deployment.

## Deployment and scope

Only `production-main.py` and its regression tests change. No qBittorrent settings, credentials, cookies, Hetzner resources, traffic policy, DNS settings, or persisted report state are modified. Back up the live backend before rebuilding Docker. Do not print secrets during live verification.
