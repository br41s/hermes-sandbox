import { test } from "node:test";
import assert from "node:assert/strict";
import { handoffExpired, lastHumanActivityAt, handoffTtlMs } from "./handoff.js";

const MIN = 60 * 1000;
const now = Date.parse("2026-10-15T10:00:00Z");
const sec = (iso) => Math.floor(Date.parse(iso) / 1000);
const bot = (iso) => ({ message_type: 1, sender: { type: "agent_bot" }, created_at: sec(iso) });
const human = (iso) => ({ message_type: 1, sender: { type: "user" }, created_at: sec(iso) });
const visitor = (iso) => ({ message_type: 0, sender: { type: "contact" }, created_at: sec(iso) });

test("only a person's messages count as human activity; bot and visitor do not", () => {
  const msgs = [visitor("2026-10-15T09:50:00Z"), bot("2026-10-15T09:51:00Z"), human("2026-10-15T09:40:00Z")];
  assert.equal(lastHumanActivityAt(msgs, "2026-10-15T09:30:00Z"), Date.parse("2026-10-15T09:40:00Z"));
  assert.equal(lastHumanActivityAt([visitor("2026-10-15T09:59:00Z"), bot("2026-10-15T09:59:30Z")], "2026-10-15T09:30:00Z"), Date.parse("2026-10-15T09:30:00Z"));
});

test("a handoff nobody picked up expires after the TTL", () => {
  const r = handoffExpired({ messages: [bot("2026-10-15T09:30:00Z")], escalationAt: "2026-10-15T09:30:00Z", now, ttlMs: 15 * MIN });
  assert.equal(r.expired, true);
  assert.equal(r.idleMs, 30 * MIN);
});

test("a handoff a person is working on does not expire while they are active", () => {
  const r = handoffExpired({
    messages: [bot("2026-10-15T09:30:00Z"), human("2026-10-15T09:55:00Z")],
    escalationAt: "2026-10-15T09:30:00Z",
    now,
    ttlMs: 15 * MIN,
  });
  assert.equal(r.expired, false);
  assert.equal(r.idleMs, 5 * MIN);
});

test("a person who replied and then went quiet past the TTL hands the visitor back to the bot", () => {
  const r = handoffExpired({ messages: [human("2026-10-15T09:00:00Z")], escalationAt: "2026-10-15T08:30:00Z", now, ttlMs: 15 * MIN });
  assert.equal(r.expired, true);
});

test("no handoff timestamp and no human message ever: expired, never a closed door", () => {
  assert.equal(handoffExpired({ messages: [], escalationAt: null, now, ttlMs: 15 * MIN }).expired, true);
});

test("TTL comes from HANDOFF_TTL_MINUTES, default 15, nonsense ignored", () => {
  delete process.env.HANDOFF_TTL_MINUTES;
  assert.equal(handoffTtlMs(), 15 * MIN);
  process.env.HANDOFF_TTL_MINUTES = "30";
  assert.equal(handoffTtlMs(), 30 * MIN);
  process.env.HANDOFF_TTL_MINUTES = "-3";
  assert.equal(handoffTtlMs(), 15 * MIN);
  delete process.env.HANDOFF_TTL_MINUTES;
});
