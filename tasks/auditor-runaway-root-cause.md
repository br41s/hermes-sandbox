# Auditor runaway reviews — root cause

**Status:** root cause identified 2026-09-13 from Langfuse trace
`d4101c0a8725d8e92cc3b5d4003ac8a8` (auditor reviewing br41s/biglobster#507).
Not yet fixed.

## Symptom

The auditor cron (`c19bb95c0a62`) spends 79–92 LLM calls on a single review pass,
running 30+ minutes. Because every profile/workdir job shares one single-thread
executor (`cron/scheduler.py:441`), this starves the whole fleet — the BigLobster
Gap Hunter sat `claimed` with zero activity for 20+ minutes waiting for it.

## Root cause: the agent has no record of its own intent

Each turn the model emits substantial `reasoning`, calls a tool, and **the reasoning
is discarded**. From the trace:

- `LLM call 8` output keys: `content` (length **1**), `reasoning` (length **935**),
  `tool_calls`.
- `LLM call 9` input: every assistant message is
  `keys=['content','role','tool_calls']` with `content=''`. There is **no
  `reasoning` key**.

So the next turn sees bare tool calls with empty content plus raw results, and has to
reconstruct what it was doing. Its own words, from call 8:

> "the outputs shown are from prior tool calls that were already executed (PR 507 diff
> and view). Let me start fresh. … I haven't run PASO 1 yet in this conversation as far
> as I can see. … Actually the system may have pre-executed."

It is not misremembering. **It never had a memory to lose.**

The consequence is visible in the same history: tool results of **520, 3947 and 174
bytes each repeat 2–3 times** within 12 messages. It re-runs identical commands
because it cannot tell it already ran them.

## Two hypotheses ruled out

- **Malformed history.** It is well formed — assistant `tool_calls` pair correctly
  with `role=tool` results.
- **System prompt lost.** Langfuse stops logging `system`/`user` after call 3
  (call 8 logs only 5 assistant + 7 tool messages), but the token counts prove the
  real request still carried it: call 1 was 41,244 input tokens for system+user
  alone, and call 8 reports 46,547 — which 12 short messages cannot account for.
  That is a Langfuse logging artifact, not context loss.

## Fix directions, ranked

1. **Durable external state (preferred).** The auditor writes a per-run checklist
   file — PRs seen, step completed — and reads it each turn. This is the ledger
   pattern already working for the Gap Hunter
   (`/opt/data/profiles/biglobster/gaphunter/research-ledger.md`), and it survives
   reasoning loss by design rather than depending on the model's recall.
2. **Require narration.** Instruct the model to put a one-line "what I just did,
   what's next" in `content` on every tool call. Nearly free, and it repairs the
   record inside the conversation.
3. **Persist reasoning across turns.** Feed `reasoning` back in assistant messages.
   Most faithful, but it is an agent-loop change affecting every agent and it
   inflates the cached prefix — see the caching invariant in `CLAUDE.md`.

A per-run call cap is still worth adding as defence in depth, but it is **not** the
fix: capping truncates a confused run rather than un-confusing it.

## Note on method

`/opt/data/logs/agent.log` cannot show this — it has no view of message history.
Langfuse is the source of truth for agent behaviour; see "Diagnosing an agent run"
in `CLAUDE.md`.
