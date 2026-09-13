# Pilot: deepseek-v4.1-flash on the biglobster profile

**Status:** baseline captured 2026-09-13, switch pending the #237 deploy.

## Why a pilot and not a fleet switch

V4.1 was validated on exactly one task — the auditor's protocol-following
failure (tool orchestration). The content fleet does long-form generation,
mostly in Spanish. Different capability; being good at one implies nothing
about the other.

The content agents are also not broken. A fleet-wide swap would change a
working system on speculation, and it would repeat the 2026-09-05 pattern: a
deliberate model change, applied without per-agent verification, that degraded
things invisibly for four days.

Content fails differently from the auditor. A broken auditor *stops posting* —
loud, once you know to look. A degraded content agent *keeps publishing*, just
with worse Spanish and weaker structure. Invisible for weeks, and already on
the site by the time anyone notices.

## Scope — 3 jobs, not 4

Changing `/opt/data/profiles/biglobster/config.yaml` `model.default` affects
every job in the profile whose own `model` is unset:

| job | id | follows profile? |
|---|---|---|
| BigLobster Gap Hunter | `ce583d11dedd` | yes |
| Biglobster SEO / GEO | `20ec3607f2c6` | yes |
| Off-Site GEO Scout | `be8a4add42b0` | yes |
| Biglobster Translation Engineer | `e197e33b0f00` | **NO** — job-pinned to `tencent/hy3` |

The Infographic Engineer (`2988bba27c73`) runs under the `default` profile
despite its name, so it is also unaffected.

## Baseline — captured BEFORE the switch (12–15 runs each)

```
job                      runs  calls~  max  tools  dups   dup%   model
BigLobster Gap Hunter      15    23.2   45    566     9   1.6%   openai/gpt-5.6-luna
Biglobster SEO/GEO         15    30.1   48    663    31   4.7%   openai/gpt-5.6-luna
Off-Site GEO Scout         12    12.2   24    239     4   1.7%   openai/gpt-5.6-luna
Translation Engineer       12    31.8   44    435     0   0.0%   tencent/hy3  (excluded)
```

`dup%` = cross-turn duplicate tool calls: the same tool with byte-identical
arguments issued again later in the same run. Within-turn repeats are already
removed by `AIAgent._deduplicate_tool_calls`, so every one of these is a genuine
"did the work twice" event. It is the cheapest mechanical proxy for an agent
losing the thread — the auditor sat at 44% while broken, against a healthy
fleet median near 2%.

## Cost

| | in $/M | out $/M |
|---|---:|---:|
| `openai/gpt-5.6-luna` | 0.200 | 1.200 |
| `deepseek/deepseek-v4.1-flash` | 0.150 | 0.600 |

~25% cheaper input, 50% cheaper output. Context is equivalent (~1.05M both).

## Success criteria — decide after one week

1. **dup% no worse** than baseline on all three jobs.
2. **calls/run no worse** — a rise means it is working harder for the same output.
3. **Spanish output quality, judged by the CEO.** This is the criterion that
   actually matters and the one no metric here captures. The other two only
   catch mechanical regressions.

Any of 1–2 clearly worse, or 3 failing, → roll back.

## Rollback

One line, no deploy, effective next run:

```yaml
# /opt/data/profiles/biglobster/config.yaml
model:
  default: openai/gpt-5.6-luna
```

## Safety net

PR #237 (runaway-agent incident signal) must be deployed BEFORE the switch.
It pages when any agent run burns its iteration budget — the failure mode that
went unnoticed for four days on the auditor. Moving models across a profile
without that alarm live is the mistake this pilot exists to avoid repeating.

## Note on config durability

Both the profile `config.yaml` and the job records live on the volume, not in
git. A volume restore reverts them silently. The job-record pin
(`update_job(id, {"model": ...})`) survives a profile-config rebalance but not a
volume restore, and `provider_routing` has no job-level equivalent at all.
