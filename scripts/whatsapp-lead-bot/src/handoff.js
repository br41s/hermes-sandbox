/**
 * A handoff is a promise that a person will continue. When nobody does,
 * the promise expires and the bot takes the conversation back on the
 * visitor's next message, instead of leaving them talking to a closed
 * door (a visitor who came back the next day got no answer at all on
 * 2026-10-15 because the conversation was still `open`).
 *
 * "Nobody did" = no human activity for HANDOFF_TTL_MINUTES: neither the
 * handoff itself (escalation_at) nor any message by a person (Chatwoot
 * sender.type "user" -- an agent's reply or private note; the bot is
 * "agent_bot", the visitor "contact") is newer than that.
 *
 * Pure functions, no I/O -- see handoff.test.mjs.
 */

export function handoffTtlMs() {
  const minutes = Number(process.env.HANDOFF_TTL_MINUTES || 15);
  return (Number.isFinite(minutes) && minutes > 0 ? minutes : 15) * 60 * 1000;
}

function toMs(value) {
  if (value == null || value === "") return null;
  if (typeof value === "number") return value < 1e12 ? value * 1000 : value; // Chatwoot: unix seconds
  const t = Date.parse(value);
  return Number.isNaN(t) ? null : t;
}

// The newest moment a person touched the conversation: the handoff, or any
// outgoing message / note whose sender is a person.
export function lastHumanActivityAt(messages, escalationAt) {
  let latest = toMs(escalationAt);
  for (const m of messages || []) {
    if (m?.sender?.type !== "user") continue;
    const t = toMs(m.created_at);
    if (t != null && (latest == null || t > latest)) latest = t;
  }
  return latest;
}

export function handoffExpired({ messages, escalationAt, now = Date.now(), ttlMs = handoffTtlMs() }) {
  const last = lastHumanActivityAt(messages, escalationAt);
  // No record of when it was handed off and nobody ever wrote: treat as
  // expired -- the alternative is a conversation no one can reach.
  if (last == null) return { expired: true, lastHumanActivityAt: null, idleMs: null };
  const idleMs = now - last;
  return { expired: idleMs >= ttlMs, lastHumanActivityAt: last, idleMs };
}
