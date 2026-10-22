# Agent browser: "My browser" (extension) or "Cloud"

The Copilot agent drives a browser to research and fill job applications. A
user chooses where that browser runs:

- **My browser** (default when the CareerCraft extension is paired): the agent
  controls one dedicated tab in the user's own Chrome through the extension.
  No server capacity limit.
- **Cloud**: the existing gVisor computer on `24gb1` (16 concurrent slots).

The agent's code, the snapshot-review rule and the human approval before any
submit are identical in both modes. Only the place an action runs changes.

## Setting

`users.computer_mode` text, one of `auto` (default), `browser`, `cloud`.

- `auto`: `browser` when the user has an **online** extension device, else
  `cloud`. Online = `extension_devices.agent_seen_at` within 90 s and not
  revoked. Only the command long-poll sets `agent_seen_at` (after the device's
  eligibility checks); `last_seen_at` is refreshed by every device route,
  including extensions too old to take commands, so it can't mean "online".
- `browser`: always the extension; if no device is online, actions fail with
  503 `{"detail": "Your browser isn't connected. Open Chrome with the
  CareerCraft extension, or switch to Cloud."}`.
- `cloud`: always the relay (today's behaviour).

Migration: `supabase/migrations/20261022120000_computer_mode.sql` adds
`users.computer_mode text NOT NULL DEFAULT 'auto' CHECK (... IN
('auto','browser','cloud'))` and `extension_devices.agent_seen_at timestamptz`.

## Backend API (signed-in member)

- `GET /api/v1/computer/mode` → `{"mode": "auto|browser|cloud",
  "effective": "browser|cloud", "extension": {"paired": bool, "online": bool},
  "cloud": {"available": bool}}`
- `PUT /api/v1/computer/mode` body `{"mode": "auto|browser|cloud"}` → same
  shape. 422 on other values.
- `GET /api/v1/computer` (status) adds `"backend": "browser"|"cloud"` to its
  existing response. Browser backend status: `{"available": true, "backend":
  "browser", "running": <extension online>, "enabled": <extension online>}`.
  Cloud unchanged (`available: false` when the relay isn't configured).
- start/stop/action routes: unchanged paths. They dispatch on the effective
  backend.

## Dispatch (backend/app/services/computer_service.py)

One function decides the backend per call, `computer_backend(user_id)`
(reads `users.computer_mode` and device liveness). Every existing `relay(...)`
call for status, start, stop and action goes through it:

| Call | Cloud | Browser |
|---|---|---|
| status | `relay(user)` | built from device liveness (above) |
| start | `relay(user,"/start")` | command `start` (opens/focuses the agent tab) |
| stop | `relay(user,"/stop")` | command `stop` (closes the agent tab) |
| action | `relay(user,"/action",body)` | command `action` with the same body |
| purge (erasure) | relay DELETE | no remote state; nothing to do |

`act()` keeps validation, audit and the upload resume lookup exactly as is;
only its final `relay(...)` call is replaced by the dispatcher. Operations not
supported in browser mode (`screenshot`, `control*`, `human/*`, `files/*`,
`privacy/release`) return 409 `{"detail": "Not available in your browser
mode: watch and use the agent tab directly."}`. Supported in browser mode:
`navigate`, `read`, `snapshot`, `click`, `type`, `scroll`, `upload`,
`human/navigate`.

## Command channel (Redis + device long-poll)

- Enqueue: `RPUSH ext:cmd:{device_id}` a JSON command
  `{"id": uuid4, "kind": "start|stop|action", "body": {...}, "expires": epoch+60}`.
- Wait for the result: `BLPOP ext:res:{command_id}` with a 60 s timeout
  (navigation can be slow). Timeout → 504 `{"detail": "Your browser didn't
  respond in time."}`. The result is JSON `{"ok": true, "result": {...}}` or
  `{"ok": false, "status": 409|403|422|502, "error": "..."}`, which the backend
  raises as `HTTPException(status, error)`. On timeout the command is removed
  from the queue (`LREM`), so a browser that reconnects later never runs an
  action its caller was told failed.
- Device endpoints (device token auth, existing `get_device`):
  - `GET /api/v1/extension/device/commands/next?wait=25`: `BLPOP
    ext:cmd:{device_id}` up to `wait` seconds (max 25); returns the command,
    or 204. Expired commands are dropped. Sets `agent_seen_at`. If the client
    disconnected while waiting, a popped command is pushed back for the next
    live poll instead of being lost. A device revoked while its poll waited
    gets 401, not the command. Polls and result waits use separate Redis
    pools; a full poll pool answers without waiting instead of failing.
    Rate limit: generous (e.g. `240/minute`).
  - `POST /api/v1/extension/device/commands/{id}/result` body `{"ok": bool,
    "result"?: object, "status"?: int, "error"?: str}` → `LPUSH ext:res:{id}`
    with a 120 s TTL. A result body is capped at 1 MB. The extension retries a
    result on network or 5xx errors; any other answer is final.
- The device id in the command key comes from the authenticated device, so a
  device only ever receives its own owner's commands. A user with several
  devices: commands go to the most recently seen online device.

## Extension behaviour

- A command loop in `background.js` long-polls `commands/next` while paired.
  The 30 s alarm restarts it if the service worker was suspended. The app page
  can wake it immediately through the existing content bridge
  (`window.postMessage` with `source: "careercraft-app"`, type
  `CAREERCRAFT_AGENT_WAKE`).
- **One agent tab.** `start` opens (or focuses) a dedicated tab and remembers
  its id; every action runs only in that tab, never in any other tab. `stop`
  closes it. If the user closes the agent tab, the next action reports 409
  `"The agent tab was closed; resume to reopen it."`.
- The agent tab sits in a Chrome tab group titled "CareerCraft agent" (set by
  the extension, out of the page's reach).
- A visible banner in the agent tab (mounted in `<body>`; mounting outside it
  breaks React/Remix hydration on sites such as Greenhouse): "CareerCraft agent is controlling this
  tab", with a **Stop** button. Stop closes the agent session, and pending and
  future actions fail with 409 until the next `start`.
- `computer_run`: a new random id each time the agent tab is (re)opened.
  `snapshot` returns `{"snapshotId": n, "computer_run": run, "url", "title",
  "elements": [{"ref": "e12", "role": "button", "name": "Submit"}]}` with refs
  assigned as `data-careercraft-ref` attributes. Snapshot ids increase per run.
- Agent writes (`navigate`, `click`, `type`, `scroll`, `upload`) arriving with
  `actor: "agent"` must carry `computer_run` equal to the current run and
  `approved_snapshot` equal to the latest snapshot id, else 409 `"Browser state
  changed; take a new snapshot and review again"`. click/type/upload also
  check `parameters.snapshotId` equals the latest snapshot id.
- Results: navigate → `{"url","title","text"}` (text truncated to 4000 chars);
  read → `{"url","title","text"}`; click/type/scroll/upload → `{"ok": true,
  "url"}`; start → `{"running": true, "enabled": true}`; stop →
  `{"running": false, "enabled": false}`.
- Host permission: if the agent tab needs a site the user hasn't granted
  (optional host permission `https://*/*`), reply 403 `{"error":
  "permission_required", "origin": "https://..."}`, and use the existing
  permission prompt flow so the user can grant it. The backend turns this into
  403 "Allow the CareerCraft extension on {origin} to continue." Nothing ran,
  so the agent re-proposes the step instead of failing the run.
- Before a write runs, the extension re-reads its state and the tab URL, and
  the page itself checks `location.href` against the snapshot URL.
- Navigation is limited to public HTTPS pages and never the CareerCraft app
  itself (an agent tab on the app could approve its own steps): http,
  localhost, private and CGNAT IPv4, IPv6 literals, `.local`/`.internal`
  names and the app give 422. Hostnames resolving to private addresses can't
  be checked (an extension has no DNS API).
- One approval, one write: a successful write spends the snapshot, as in the
  cloud computer. Any URL change in the agent tab also voids it (same-URL
  `replaceState` does not). Refs resolve through a map page scripts can't
  reach. Snapshots cover the top frame only (max 400 elements), so forms inside
  iframes are not visible to the agent yet.
- Never read or return cookies, passwords (input type=password values),
  storage or credentials; `type` into a password field is refused (422).

## App (frontend)

- Settings (and the Copilot computer panel header): "Agent browser" with three
  choices: **Automatic** (my browser when connected, otherwise cloud), **My
  browser**, **Cloud**. Show extension status (paired / online) and a link to
  pair the extension when not paired.
- When the effective backend is `browser`, the computer panel shows "Running in
  your browser: the agent works in a CareerCraft tab in Chrome" with the
  connection status, instead of the cloud screen view.
- When Cloud answers 429 "slots busy" and the extension is paired, offer
  "Use my browser instead".
