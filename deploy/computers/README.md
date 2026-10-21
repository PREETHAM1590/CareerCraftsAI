# Private on-demand computers

## Production (careercraftsai.me)

Host: SSH alias `24gb1` (Oracle A1, 4 OCPU / 24 GB, Ubuntu 26.04), checkout of
this directory at `~/careercraft-computers/deploy/computers`, secrets in the
root-only `/opt/careercraft-secrets/computers.env`.

- **Isolation: gVisor.** Every user's browser runs under `runsc`
  (`COMPUTER_RUNTIME`, default `runsc`), a user-space kernel between untrusted
  pages and the host. Install it once:
  `apt-get install runsc` from the gvisor.dev apt repository, `runsc install`,
  `systemctl restart docker`.
- **Egress by address.** Docker's embedded DNS (127.0.0.11) does not answer
  inside gVisor containers, so browsers dial the Squid proxy at its pinned IP
  `172.31.250.10` (see `compose.yml`, `harden-computer.mjs`). Squid resolves
  sites itself and still denies private ranges and the metadata endpoint.
- **No test fixture.** `test-portal` runs only with `--profile test`, and its
  Squid exception is built in only with `ALLOW_TEST_PORTAL=1`.
- **Link to the app.** The APP VM's `sandbox-tunnel` service (in
  `deploy/oracle-vm/compose.yml`) forwards its loopback `127.0.0.1:14312` to the
  relay, as the dedicated `sandboxtunnel` account (`nologin`). Its key carries
  `restrict,port-forwarding,permitopen="127.0.0.1:14312",command="/bin/false"`,
  and `/etc/ssh/sshd_config.d/70-sandboxtunnel.conf` enforces the same for the
  account: `AllowTcpForwarding local`, `PermitOpen 127.0.0.1:14312`,
  `PermitListen none`, `PermitTTY no`, `ForceCommand /bin/false` (per-key
  `permitopen` alone does not stop `-R`). `backend.env` carries
  `SANDBOX_RELAY_URL=http://127.0.0.1:14312` and the relay token.

### Capacity (measured 2026-10-22 on 24gb1, gVisor)

`loadtest_computers.py`: N users at once, each starting a computer, opening real
job boards, polling a screenshot every 4 s and reading agent snapshots.

| Concurrent users | Navigate p50 | Screenshot p50 / p95 | Snapshot p95 | Failures |
|---|---|---|---|---|
| 12 | 15 s | 0.6 s / 3.8 s | 3.8 s | 0 |
| 16 | 18–26 s | 0.9–3.2 s / 4.7–9.3 s | 11–17 s | 0 |
| 20 | 32 s | 5.8 s / 16 s | 24 s | 4 |
| 24 | 29 s | 7.0 s / 18 s | 34 s | 10 |

CPU is the limit (4 cores at 100% from 12 browsers); memory is ~400 MB per
browser. Production: `SANDBOX_MAX_SESSIONS=16`, `COMPUTER_MEMORY_BYTES=1073741824`.
For comparison, runc at 12 users: navigate p50 2.7 s, screenshot p50 0.09 s;
gVisor's syscall interception costs roughly 4-6x in latency (no KVM on A1).

### Changing the internal network

`careercraft-sandbox-computers` has a fixed name and subnet (`172.31.250.0/24`,
egress at `.10`). A host created before the subnet was pinned keeps its old
network; recreate it once:

```sh
E=/opt/careercraft-secrets/computers.env
sudo docker ps -aq --filter name=sandbox-computer-u | xargs -r sudo docker rm -f
sudo docker compose --env-file $E down
sudo docker network rm careercraft-sandbox-computers 2>/dev/null || true
sudo docker compose --env-file $E up -d supervisor relay egress
```

```sh
E=/opt/careercraft-secrets/computers.env
sudo docker compose --env-file $E build computer-image
sudo docker compose --env-file $E build private-computer-image supervisor relay egress
sudo docker compose --env-file $E up -d supervisor relay egress
# Verification, test mode: set ALLOW_TEST_PORTAL=1 in $E, then
#   sudo docker compose --env-file $E build egress
#   sudo docker compose --env-file $E --profile test up -d egress test-portal
# run verify*.py inside the relay container (see below), and switch back
# without touching supervisor/relay (live computers keep running):
#   set ALLOW_TEST_PORTAL=0 in $E, then
#   sudo docker compose --env-file $E --profile test rm -sf test-portal
#   sudo docker compose --env-file $E build egress
#   sudo docker compose --env-file $E up -d egress
```

## Test deployment

Test deployment: SSH alias `24gb`, isolated directory `~/careercraft-sandbox`.
The local CareerCraft app stays at http://localhost:18180/copilot. Its backend
and Temporal worker reach the relay through an SSH tunnel on their private
Docker network. The remote relay is published only on `127.0.0.1:14312`.

OpenBot source is pinned to `b6932d31a8d6e7896c15139dfc27a6c6911deb27`.
Build the base before the private computer image. Retain the upstream license.

```sh
# .env here also sets ALLOW_TEST_PORTAL=1, or verify*.py cannot reach the fixture
docker compose --env-file .env build computer-image
docker compose --env-file .env build private-computer-image supervisor relay egress
docker compose --env-file .env --profile test up -d supervisor relay egress test-portal
```

Create `.env` outside source control with independent random 32-byte secrets:
`COMPUTER_TOKEN`, `COMPUTER_SUPERVISOR_TOKEN`, `SANDBOX_RELAY_TOKEN`.
The local `deploy/local/up.ps1` reads the relay token from gitignored
`deploy/secrets/local/sandbox.env`. `sandbox-tunnel` mounts only the SSH key and
known-hosts file from that directory. It verifies the server host key.
The tunnel targets the user-authorized `24gb` host; change `tunnel.sh` if moving it.

There are eight computer slots and a 90-second inactivity timeout. Screen and
control polling neither allocates a computer nor renews its lease. Profiles and
workspace files survive sleep. A full pool returns 429; it does **not** silently
queue another browser. Temporal persists approval waits without a browser.

Browsers use an internal Docker network and a mandatory Squid egress proxy.
Private addresses and metadata endpoints are denied. The sole private HTTP
exception is the synthetic `test-portal`; remove that exception and fixture
service when leaving the test deployment. Proxy request logging is disabled.
Only the supervisor holds Docker socket access. Each child gets its own HMAC
token and isolated profile/workspace volumes.

The pinned image uses Docker isolation with Chromium's own process sandbox off
under the default Docker seccomp policy. This deployment is for testing. Before
production, configure a supported Chromium sandbox policy or a stronger container
runtime and test it with the target portals.

The model gets accessibility snapshots, never screenshots, cookies, shell,
scripts, Docker access, or credential retrieval. Browser mutations require
an immutable app approval, current computer run and reviewed snapshot.
Takeover and restarts invalidate old actions; uncertain actions are not retried.
In the expanded browser, a focused native input surface forwards mouse clicks,
text, paste and navigation keys through the human-only routes in order. Esc
leaves keyboard capture. Text is buffered briefly outside chat/state; queued
input drains before toolbar actions and is discarded on loss of control.
`verify-keyboard.py` checks typing, editing, Unicode and privacy on the fixture.
Credentials are encrypted with the existing AES-256-GCM helper, owner scoped,
and filled only into inputs belonging to the exact saved HTTPS origin.
Browser processes necessarily receive the plaintext to sign in. Session-cookie
volumes are private filesystem data, not encrypted by this vault feature.

Private typing/filling persists a marker outside the model workspace. Model page
access remains blocked until the user leaves the login page, checks that no
secrets are visible, and chooses **Finish private input**. Release human control
afterward. Deleting a saved login does not revoke portal session cookies.
Unmasking a password or changing only a URL query/fragment cannot release the
private-input block on the same page.

Verification uses only generated accounts and synthetic fixture data:

```sh
docker cp verify.py careercraft-sandbox-relay-1:/tmp/verify.py
docker exec careercraft-sandbox-relay-1 python /tmp/verify.py
# Run on the host alongside it for Docker CPU/RAM samples:
python3 observe.py > resource-samples.jsonl
```

Start with a free pool. Verification stops every computer it creates. It keeps
its profiles so workspace persistence can be inspected; do not delete other
users' volumes when cleaning synthetic test data. After changing a Compose
network, force-recreate its services so Docker DNS aliases are restored.

This is a bounded test sandbox, not a claim of support for every portal. CAPTCHA,
MFA, browser-detection challenges and login require human takeover. Saved resume
uploads use an owner-scoped document ID, a current file-input ref and a reviewed
snapshot. File bytes go directly from document storage to the browser, without
giving the model a filesystem path or file-reading capability. The user can
upload manually while holding control; agent uploads require approval.
Generic-portal submission reconciliation with the application ledger remains
unconnected; the extension workflow is the supported path for tracking those.
An active user model is required for planning. Public search and resume storage
work without one. Run `verify-upload.py` inside the relay to check uploads and
stale-approval rejection against the synthetic portal.
CopilotKit's self-managed-agent production license must be resolved before sale.

## Account erasure contract

The backend's expiry sweep calls authenticated `DELETE /users/{user_uuid}` before
Clerk/account deletion. The relay disables the owner and serializes purge against
all actions and idle release. The supervisor removes only UUID-derived resources
whose supervisor, namespace and bot labels all match: the container, profile and
workspace volumes. Missing resources are successful; Docker errors, busy volumes
and ownership conflicts fail closed and are retried by the next account sweep.
A minimal UUID tombstone blocks delayed requests from recreating an erased owner.
No browser data is stored in that tombstone. Ordinary Stop still retains profiles.

Rebuild both supervisor and relay when deploying this change. An old supervisor
without the purge route cannot confirm erasure and the backend retains the account
for retry. Removing relay configuration on a host that has existing volumes is
not a supported erasure operation; keep it configured until all owners are purged.
