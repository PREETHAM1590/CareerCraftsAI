# Architecture and security review — 12 November 2026

## Assessment

CareerCraft has a defensible core but too much operational surface for a solo-maintained product. Keep the existing stack during this correctness pass; avoid a framework rewrite. Unit/stub coverage does not demonstrate live provider quality or employer-site compatibility.

## Actual flows

- **Search:** authenticated FastAPI route → Temporal search workflow → connector registry / public job catalog + live fallbacks → deduplication, location eligibility and ranking → member applications. Redis caches public discovery results; saved boards are member-scoped.
- **Documents:** authenticated upload → bounded parsing → stored original and profile facts → contact-stripped chunks and embedding provider → per-member pgvector retrieval → agent through internal LLM gateway → fact checking / repair → PDF or DOCX and parse-back warnings.
- **Apply:** backend task and field plan → paired Chrome extension in the member's browser → deterministic profile/saved answers or reviewed narrative drafts → server approval and audit → browser submission. Auto-submit is opt-in, capped, and disallowed for LinkedIn/Naukri. Approval records are not proof the employer accepted an application.
- **Email:** connected Gmail integration → classification / company-domain checks → status update or suggestion; outbound drafts remain behind approval.

## Stack decisions

| Component | Decision | Reason |
|---|---|---|
| FastAPI / SQLAlchemy / Postgres | Keep | Good fit; explicit transactions and ownership checks remain essential. |
| pgvector | Keep | Avoid another vector service; evaluate real embeddings before claiming relevance. |
| Redis | Keep, isolate workloads | Cache eviction must not jeopardize rate limits or workflow state. Prefer separate instances when capacity warrants it. |
| Temporal | Keep, limit scope | Useful for durable approvals/retries; do not duplicate workflow state in ad hoc schedulers. |
| LangGraph | Simplify | Keep only graphs needing multiple steps; avoid agent abstractions around deterministic operations. |
| Clerk | Keep | Do not replace working authentication during a correctness pass. Runtime verification requires project keys. |
| Nango | Reassess later | Useful integration plumbing, but self-hosting adds token-management and operational burden. |
| CopilotKit | Reassess later | Dependency and protocol cost; a simpler chat UI may be sufficient. Do not force incompatible dependency majors. |
| Browser extension | Keep | Appropriate place for user-session form filling; restrict hosts and require explicit pairing changes. |
| Remote computer/sandbox stack | Freeze expansion | High operational cost and Docker-socket privilege; isolate it from the API and member-data stores. |
| Monitoring | Simplify | Prioritize actionable error, queue-age, workflow-failure and backup alerts over more services. |

## Security priorities

1. **PII boundary:** identity fields should never require a model. Gateway pseudonymisation and local rehydration reduce exposure, not eliminate it; unknown names, free-form addresses and old stored vectors require special care. Prefer local embeddings for stronger data locality.
2. **Prompt injection:** delimit documents, email and retrieved data, but enforce side-effect permissions in code. Fences alone are not a security guarantee.
3. **Tenant isolation:** the API's owner database role can bypass RLS. Retain explicit ownership filters and real-database cross-user tests; plan a non-owner runtime role separately.
4. **Browser isolation:** a compromised app-origin script must not silently switch accounts; arbitrary-site access needs a member-approved host. Review supervisor Docker-socket access before exposing browser infrastructure publicly.
5. **Dependency management:** audit patched dependencies and block new high/critical production advisories. The recorded Zod 3 advisory remains an explicitly accepted risk, not a clean audit.
6. **Deployment:** use non-root containers where supported, minimize exposed ports, isolate secrets, and rehearse restore/rollback. Do not change production Docker policy without a separate deployment rehearsal.

## Release gates and limits

Real-database migration, concurrency, deletion and cross-user tests matter as much as fast stub tests. CI should execute the hermetic route journey, not merely collect it. Keep live-site tests opt-in: no real applications should be submitted by CI.

Managed frontend Preview was attempted in this workspace and returned HTTP 500 because the Clerk publishable key is absent. The server was stopped through Preview controls. No authentication bypass or substitute Clerk account was introduced; signed-in visual verification remains pending.

Do not describe this branch as production-proven until signed-in UI checks, real-model privacy behaviour, actual employer-form tests with user approval, and the deployment/backup rehearsal have been completed.
