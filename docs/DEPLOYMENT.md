# Deployment Guide

Production runs on a single self-hosted Oracle Cloud "Always Free" ARM VM.
There is no Supabase Cloud dependency anywhere in this setup — Postgres,
auth verification, and everything else self-hosted or reached via each
service's own API (Clerk, the user's own AI provider, etc.).

---

## Infrastructure Overview

```
Internet
    │
    ▼
ngrok tunnel (configure_runtime.py + a systemd unit set this up;
              forwards to 127.0.0.1:18180)
    │
    ▼
APP VM  "chola-public", 10.0.0.183  (Oracle "Always Free" ARM,
        network_mode: host throughout — the VM has no IPv6 route, and
        Supabase's managed Postgres/Storage were IPv6-only from here)
├── gateway (nginx, loopback :18180)
│     /api/v1/* → backend :18100
│     everything else → frontend :18101
├── careercraft-isolated  (deploy/oracle-vm/compose.yml)
│   ├── backend               (FastAPI, :18100)
│   ├── frontend              (Next.js, :18101)
│   ├── temporal-worker       (python -m app.temporal_worker — runs every workflow)
│   ├── notification-worker   (python -m app.notification_worker — in-app + email notifications)
│   ├── postgres              (pgvector/pgvector:pg16, :18132 — self-hosted DB)
│   └── redis                 (SSE pub/sub, rate limiting, LLM sessions — no queues)
└── temporal-isolated  (deploy/oracle-vm/temporal-compose.yml)
    └── self-hosted Temporal server + its own Postgres + Web UI, ALL on
        loopback: 127.0.0.1:7233 (frontend), 6933–6939 (membership),
        19232 (temporal-postgres), 19280 (UI). Nothing leaves the box, so
        no firewall / Security List port is needed for Temporal.

INFRA VM  "instance-20260921-0007", 10.0.0.182
├── nango-isolated  (deploy/oracle-vm/nango-compose.yml)
│   └── self-hosted Nango — Gmail/Drive OAuth broker, :191xx range
└── ollama-isolated (deploy/oracle-vm/ollama-compose.yml)
    └── embeddings, :11434, allowed only from the APP VM's private IP
```

Temporal used to run on the INFRA VM and was reached over the public
internet. It now lives next to its only clients on the APP VM; the
`*-isolated` compose project names still let any piece move to its own box
later.

---

## Prerequisites

- An Oracle Cloud "Always Free" ARM VM (or equivalent — nothing here is
  Oracle-specific beyond the free-tier sizing and the lack of an IPv6 route)
- Docker + the Docker Compose plugin
- The `/opt/careercraft-secrets/` directory, containing (all gitignored,
  root-owned, `chmod 600`):
  - `backend.env` — `DATABASE_URL`, `REDIS_URL`, `CLERK_SECRET_KEY`,
    `APP_SECRET_KEY`, `TEMPORAL_ADDRESS=127.0.0.1:7233`, `RESEND_API_KEY`
    (notification emails are skipped without it) and `RESEND_FROM_EMAIL`
    (a sender on a domain verified in Resend, e.g. `noreply@careercraftsai.me`), third-party API keys, etc.
    Shared by `backend`, `temporal-worker` and `notification-worker`. A
    leftover `INTERNAL_SECRET` is ignored.
  - `public.env` — `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`
    (frontend build args — see the note in `deploy/oracle-vm/compose.yml`
    about why these are passed as build `args`, not runtime env, and never
    merged into `backend.env`)
  - `postgres.env`, `redis.conf`, `temporal.env` (APP VM —
    `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_PWD`/`POSTGRES_DB` for
    Temporal's own Postgres)
  - `nango.env`, `nango-redis.conf` (INFRA VM only)
  - `ngrok.yml` — used by the systemd unit `configure_runtime.py` writes
  - `public.env`'s `CLERK_SECRET_KEY` must be the **same instance** as the
    publishable key (both live). A leftover `sk_test` there issues tokens
    from the development instance that production then rejects.
- Root filesystem mounted **without** online `discard` (Oracle's cloud image
  ships with it). Every deploy frees GBs of old image layers, and synchronous
  TRIM on the block volume kept the disk ~96% busy for 30+ minutes, stalling
  Postgres and the API. Ubuntu's weekly `fstrim.timer` trims instead:

  ```bash
  sudo sed -i 's/\tdiscard,commit=30/\tcommit=30/' /etc/fstab  # the / entry
  sudo findmnt --verify --tab-file /etc/fstab && sudo systemctl daemon-reload
  sudo mount -o remount,nodiscard / && systemctl is-enabled fstrim.timer
  ```
- Host nginx: every TLS site listens with `http2`, and proxied JSON is
  gzipped. See `deploy/oracle-vm/nginx-host/gzip-text.conf`.

---

## 1. Clone and check out the code

```bash
cd ~/CareerCraftsAI   # wherever the checkout lives on the VM
git checkout master
git pull origin master
```

---

## 2. Apply pending database migrations

`scripts/migrate.py` applies every file in `supabase/migrations/` that is not
yet recorded in the `public.schema_migrations` ledger, in filename order, each
in its own transaction. Run it from the repo checkout on the VM before
deploying code that depends on a new migration (it needs Python and psycopg,
not the app image):

```bash
python3 -m venv ~/.careercraft-migrate && ~/.careercraft-migrate/bin/pip install 'psycopg[binary]'
export DATABASE_URL=postgresql://careercraft:<password>@127.0.0.1:18132/<database>
~/.careercraft-migrate/bin/python scripts/migrate.py --dry-run   # list what is pending
~/.careercraft-migrate/bin/python scripts/migrate.py
```

**First run on a database migrated by hand.** The runner refuses to touch a
database that has tables but no ledger. Record what is already applied, then
run it normally to apply the rest:

```bash
~/.careercraft-migrate/bin/python scripts/migrate.py --baseline 20261001093000_b_extension_task_statuses.sql
```

`--baseline` with no file name records every file as applied.

---

## 3. Rebuild and restart the changed services

```bash
cd deploy/oracle-vm
sudo docker compose build backend frontend temporal-worker notification-worker
sudo docker compose up -d backend frontend temporal-worker notification-worker
```

Only rebuild the services whose code actually changed — rebuilding
`postgres`/`redis`/`gateway` is never needed for an
application code change.

**Important:** `NEXT_PUBLIC_*` variables (the Clerk publishable key,
`NEXT_PUBLIC_API_URL`, etc.) are baked into the frontend's JS bundle at
**build time** via Docker build `args` — editing `/opt/careercraft-secrets/
public.env` and merely `docker restart`-ing the frontend container does
**not** pick up the change. A rebuild (`docker compose build frontend`) is
required whenever any `NEXT_PUBLIC_*` value changes.

---

## 4. Verify

```bash
sudo docker compose ps
curl -s http://127.0.0.1:18180/health | python3 -m json.tool
```

Expect `"status":"ok"`, `"db":"ok"`, `"redis":"ok"`, and
`"temporal":{"connected":true,"workers":<N>,...}`. A `workers` count of 0
means `temporal-worker` isn't actually registered even if the container
shows `Up` — `docker compose ps` alone doesn't catch that.

---

## Temporal (the only job engine)

Every agent run, job search, application, follow-up and recurring job is a
Temporal workflow; nothing else executes background work. To run it in
production:

1. **Server.** On the **APP VM**, from `deploy/oracle-vm`:
   `sudo docker compose -f temporal-compose.yml up -d` (needs
   `/opt/careercraft-secrets/temporal.env`). Server, its Postgres and the UI
   all bind loopback only (7233, 6933–6939, 19232, 19280), so no firewall or
   Security List rule is needed and nothing is exposed to the internet.
   Its data lives in the `temporal-isolated_temporal_pgdata` volume — never
   delete it without a `pg_dump` (`-p 19232`, databases `temporal` and
   `temporal_visibility`).
2. **Address.** `TEMPORAL_ADDRESS=127.0.0.1:7233` in `backend.env`. Traffic
   never leaves the box, so no TLS is needed; only if the server ever moves
   off-box set `TEMPORAL_TLS_CERT_PATH`/`TEMPORAL_TLS_KEY_PATH` (+
   `TEMPORAL_TLS_CA_PATH`) and mount the files into the workers and `backend`.
3. **Workers.** `temporal-worker` (task queue `careercraft`) and
   `notification-worker` (`python -m app.notification_worker`, task queues
   `careercraft-notifications` and `careercraft-notifications-email`) are both
   services in `compose.yml` and both read `backend.env`. The notification
   worker is required for in-app notifications and notification emails
   (the latter also needs `RESEND_API_KEY`).
   `/health` reports pollers for both queue groups.
   **Temporal UI:** it has no auth and is not published. Use an SSH tunnel:
   `ssh -N -L 19280:127.0.0.1:19280 oraclevm`, then open
   `http://localhost:19280`.
   **Log rotation:** the Temporal services and `notification-worker` use the
   `json-file` driver capped at 10 MB × 3 files each.
4. **Schedules.** Each worker start registers `daily-job-search`,
   `maintenance` (`TEMPORAL_SCHEDULES_ENABLED=true`). Check with
   `temporal schedule list`.
5. **Retire the old queue workers** on any host that predates the Temporal
   migration: stop and remove the Node `worker` and Python `agent-worker`
   containers (`docker compose up -d --remove-orphans` does this for
   services no longer in the compose file), and delete the old queue's
   leftover Redis keys once no old worker is running. Redis stays — it backs the SSE
   event bus, the public demo's per-IP quota, LLM gateway sessions, token
   budgets and caches (slowapi limits are in-process).
6. **Verify** with `/health` (step 4 above) and, to run a job by hand,
   `temporal schedule trigger --schedule-id daily-job-search`.

---

## Ollama (embeddings)

Chat providers without an embeddings API (Anthropic, DeepSeek, OpenRouter,
NVIDIA NIM) index documents through a self-hosted Ollama serving
`qwen3-embedding:0.6b`. It runs on the INFRA VM
(`deploy/oracle-vm/ollama-compose.yml`), bound to that box's private IP only —
Ollama has no authentication.

1. On the INFRA VM, start it and pull the model:

   ```bash
   cd /opt/careercraft
   docker compose -f deploy/oracle-vm/ollama-compose.yml up -d
   docker compose -f deploy/oracle-vm/ollama-compose.yml exec ollama \
     ollama pull qwen3-embedding:0.6b
   ```

2. Allow TCP 11434 **only from the main app VM's private IP**, never
   `0.0.0.0/0`:
   - OCI: add an ingress rule to the subnet's security list — source
     `<app-vm-private-ip>/32`, TCP, destination port 11434.
   - On the box itself:

     ```bash
     sudo iptables -I INPUT -p tcp --dport 11434 -s <app-vm-private-ip> -j ACCEPT
     sudo iptables -A INPUT -p tcp --dport 11434 -j DROP
     sudo netfilter-persistent save
     ```

3. In `/opt/careercraft-secrets/backend.env` on the main VM:

   ```
   EMBEDDING_PROVIDER=ollama
   EMBEDDING_OLLAMA_URL=http://10.0.0.182:11434
   ```

4. Recreate the services that read it:

   ```bash
   docker compose -f deploy/oracle-vm/compose.yml up -d --force-recreate backend temporal-worker
   ```

5. Check from the main VM:
   `curl -s http://10.0.0.182:11434/api/tags` should list `qwen3-embedding:0.6b`.

After any embedding-model or dimension change, existing documents stay in
their old collection (collections are named `{user}_{doc}_{provider}_{dim}d`)
and are no longer searched. **Users must re-upload their documents** to
re-index them.

---

## Secrets rotation (e.g. switching Clerk from a development to a
## production instance)

1. Edit the relevant `.env` file under `/opt/careercraft-secrets/` directly
   (`sudo sed -i` or a text editor over SSH — these files are never in git)
2. Rebuild + restart whichever service reads that variable (see the
   `NEXT_PUBLIC_*` build-time note above — this is the most common way a
   key rotation silently fails to take effect)
3. Verify with a scoped, secret-safe check before trusting it, e.g.:
   ```bash
   sudo bash -c "grep -o '^NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=\"pk_[a-z]*' /opt/careercraft-secrets/public.env"
   ```

---

## Rollback

```bash
cd ~/CareerCraftsAI
git checkout <previous-commit-or-tag>
cd deploy/oracle-vm
sudo docker compose build backend frontend temporal-worker notification-worker
sudo docker compose up -d backend frontend temporal-worker notification-worker
```

There is no automated down-migration path — a schema change that needs
rolling back requires writing and running the inverse SQL by hand, the same
way the forward migration was applied in step 2 above.

---

## Known gaps (not yet solved, worth knowing about)

- **No CI/CD auto-deploy.** Deploys are manual (`git pull` + rebuild on the
  VM directly), not triggered by merging to `master`.
- **ngrok as the public ingress** is unusual for a permanent production
  setup (normally used for temporary/dev tunneling) — the comments in
  `deploy/oracle-vm/nango-compose.yml` describe the current layout as
  "for now," suggesting this is understood to be a transitional setup, not
  the intended long-term architecture.

### Retired application executor: rollout order

Stop new application starts on the old release, then complete or cancel all its
application workflows and stop the old workers before deploying this release.
Do not replay histories for the retired executor against the new worker.
`ensure_schedules()` removes the obsolete `application-status-check` schedule.
Apply `20260930090000_b_retire_browser_execution.sql` only after the drain;
it archives encrypted state and revokes runtime access without deleting data.
For rollback, drain the new workers, apply
`docs/agent-b/rollback/b_retire_browser_execution.sql`, and restore the previous
release. Data purge is a separate operator-authorized retention operation.
Applications require a paired extension; server browser tasks fail closed in
production. Local browser search tools remain for development only.
