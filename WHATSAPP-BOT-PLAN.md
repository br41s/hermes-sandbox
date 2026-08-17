# WhatsApp AI lead chatbot — decisions record

Product: a rented WhatsApp AI lead-qualification chatbot for BigLobster website customers.
Status: **planning complete, implementation not started.** No implementation code has been written.

Companion document: `WHATSAPP-BOT-IMPLEMENTATION-PROMPT.md` (the handoff brief for the coding agent).

---

## ⏰ Open reminder — Meta Tech Provider registration

**Start on or after 3 September 2026** (a scheduled reminder is already armed for that date).

Deferred on 13 Aug 2026 because the BigLobster LLC was still being incorporated, and Meta business
verification requires the legal entity's documents.

- Timeline is **4–6 weeks** (2–4 weeks partner application, 1–2 weeks WhatsApp specialty, plus
  business verification). It is the **longest pole in the project** — nothing else takes this long.
- Until then the pilot runs on the **customer-creates-their-own-Meta-App** path. That is acceptable
  for the first customers (friends, set up in person) but **is not defensible once the chatbot is
  publicly marketed** on the biglobster website: Meta requires ISVs offering WhatsApp to clients to
  be enrolled Tech Providers, and non-enrolled ISVs are prohibited from sending messages.
- Tech Provider status also unlocks Embedded Signup (one-click onboarding) and a single central
  webhook endpoint for all customers.

---

## Decisions taken

| # | Decision | Rationale |
|---|---|---|
| 1 | **Build on self-hosted Chatwoot CE**, not a bespoke integration | Removes ~half the original build: channel, inbox, conversation state machine, human takeover, history. Deliberate override of the "no new service / SQLite only" minimalism rule. |
| 2 | **Customer owns the WABA, phone number and Meta billing** | Clean separation; customer keeps the number if they leave. Matches Meta's Tech Provider model. |
| 3 | **€49/month** for infrastructure, integration, updates and fixes — **excluding** Meta message fees | At €25 the Meta fees could exceed our own fee, which reads badly. See cost note below. |
| 4 | **Thin BigLobster-styled inbox** in the bl-site-package panel over Chatwoot's API | Keeps us on stock CE images (trivial upgrades), avoids the enterprise-gated custom-branding feature, keeps visual consistency. |
| 5 | **Run Chatwoot on the existing Zeabur (Frankfurt)** alongside Hermes | ~1.4–1.9 GB tuned footprint against ~4 GB free. Fine at 2 customers. Conditions in the implementation brief. |
| 6 | **Pilot on customer-created Meta Apps**; register as Tech Provider once the LLC exists | See reminder above. |

### Standing principle established

> A good-fit open-source project may override the workspace minimalism rule (single container,
> SQLite, no new services). Evaluate fit first; the rule is a default, not a constraint.

---

## Cost model

**BigLobster charges €49/month.** Meta charges the customer directly.

Spain, from **1 October 2026**, service messages inside the 24-hour window stop being free:

| Category | Per business-sent message |
|---|---|
| Service / utility | ~€0.0166 |
| Marketing | ~€0.051–0.061 |
| Customer inbound | free |

An 8-message qualification conversation ≈ **€0.13**. 200 conversations/month ≈ **€27/month** to Meta.

**Two consequences:**

1. The sales page must state a concrete estimate, not a vague disclaimer:
   *"€49/month + WhatsApp costs billed directly by Meta — typically €5–30/month depending on volume."*
2. **Bot chattiness is money.** Every extra bot turn costs the customer ~1.7 cents. The bot must
   combine greeting + AI disclosure + first question into a single message and prefer fewer, denser
   replies. This is a hard requirement in the implementation brief, not a nicety.

**Permanent constraint:** because the customer pays Meta directly, all-in bundled pricing is
impossible. Rebilling Meta usage requires a credit line, which is the Solution Partner tier — a
different, lengthier program. "€49 + your Meta usage" is the pricing model for the foreseeable future.

---

## Verified facts about Chatwoot

Checked against source, not blog summaries.

| Question | Answer |
|---|---|
| Core license | **MIT (Expat)**. `enterprise/` is separately licensed. Blogs claiming AGPLv3 are wrong — the LICENSE file was read directly. MIT permits building and reselling a derivative service. |
| AgentBot API | **MIT core** (`app/models/agent_bot.rb`). Our OpenRouter bot plugs into it for free. |
| Captain AI | **Enterprise-only** (`enterprise/app/models/captain/`); self-hosted requires Premium Support at $19/agent/month. **We do not use it** — we bring our own AI. |
| WhatsApp Cloud API channel | **Core.** Only voice calling is feature-flagged. |
| Multi-tenancy | `Account` is the tenant, `account_id`-scoped throughout. **Platform API is self-hosted-only and free**; needs a platform app created at `/super_admin/platform_apps`. |
| Agent seat limit in CE | None. |
| License hygiene | Run the `-ce`/`-foss` image tag or set `DISABLE_ENTERPRISE=true`. **Mandatory** — we are selling this. |
| pgvector | **Required since v4.0.** Plain Postgres fails with `PG::FeatureNotSupported`. Zeabur has a PostGIS+pgvector template. |
| Resource needs | 4 GB / 2 vCPU handles ~10k conversations/day — three orders of magnitude above our volume. |

### Known upstream problems

- **[chatwoot#13154](https://github.com/chatwoot/chatwoot/issues/13154) — OPEN, severity 2.**
  Self-hosted WhatsApp Embedded Signup is broken (webhook subscription OAuthException #100, the
  `messages` field is not auto-subscribed, SDK load failures). **Embedded Signup is therefore not
  available**, which is a second reason the pilot uses manual onboarding. Revisit when both this
  lands and we hold Tech Provider status. Note Meta deprecates Embedded Signup v2 on 15 Oct 2026 —
  target v4 when we build it.
- **[chatwoot#13280](https://github.com/chatwoot/chatwoot/issues/13280) — OPEN.** RAM + swap
  exhaustion after upgrading to v4.9.0 (~14–15 GB on a 16 GB box; individual processes only
  300–350 MB, so likely process-count explosion). Mitigation: pin a known-good version tag, set hard
  per-service memory limits.

---

## Architecture

```
Visitor ──WhatsApp──▶ Meta Cloud API
                          │  (customer-owned WABA, customer pays Meta,
                          │   customer-owned Meta App during the pilot)
                          ▼
        ┌─────────────────────────────────────────┐
        │  BigLobster Chatwoot CE (Zeabur, EU)    │
        │  Account per customer · WhatsApp inbox  │
        │  conversation state · human takeover    │
        └───────┬─────────────────────────▲───────┘
     AgentBot   │ message_created          │ reply via API
     webhook    ▼                          │
        ┌─────────────────────────────────────────┐
        │  BigLobster bot service (Node, ours)    │
        │  registry · OpenRouter · lead extract   │
        │  escalation policy · output validation  │
        └───────┬─────────────────────────────────┘
                │ GET /api/knowledge (bearer token)
                ▼
        Customer's bl-site-package deployment
        (site content, articles, products, SQLite)
```

Chatwoot's native conversation states map onto the originally-planned state machine directly:
`pending` = bot-owned, bot flips to `open` to hand off, human flips back to `pending` to return
control. **The `state_version` column and its race-condition handling from the original paper are
deleted** — solved upstream by a project running this at scale.

### The gap centralising created

In the original per-deployment design the bot lived inside each customer's container, so website
knowledge retrieval was a local SQLite query. Moving the bot to a central service **removes that free
access**. Hence the new `GET /api/knowledge` endpoint in bl-site-package and the registry in the bot
service — see the implementation brief. This was not in the original technical paper and would have
stalled the AI phase.

---

## Risks accepted

| Risk | Reality |
|---|---|
| Single point of failure | Chatwoot down = every customer's bot down. The old per-deployment design failed independently. Do not promise an SLA we cannot hold. |
| **GDPR: BigLobster becomes a data processor** | All customers' WhatsApp conversations (phone numbers, names, message content) live in our Postgres. Requires a **signed DPA with each customer**, EU hosting (Frankfurt), and a documented retention policy. Get the DPA reviewed — do not invent the wording. |
| Standing infra ops | Postgres + Redis + Sidekiq + Rails upgrades, backups, monitoring — an ongoing commitment that does not exist in our stack today. |
| Backups hold everyone's PII | Encrypted, tested restore, defined retention. Note `service exec` cannot move >~5 MB on Zeabur, so backups must go container → object storage directly. |
| Shared node with Hermes | Two customers' PII sits on the same node as the Hermes control plane. Widens DPA and incident scope. Set explicit memory limits so Chatwoot cannot starve Hermes. |

---

## Deferred

Voice notes, images, documents and media download · automated outbound campaigns · approved-template
management (except the single re-engagement template, see brief) · CRM integrations · multi-agent
assignment · WebSockets · semantic embeddings / vector search of site content · analytics dashboards ·
automatic translation · WhatsApp Business mobile-app coexistence · Embedded Signup · Chatwoot Captain.
