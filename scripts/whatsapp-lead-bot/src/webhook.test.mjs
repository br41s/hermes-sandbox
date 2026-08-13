import { test } from "node:test";
import assert from "node:assert/strict";
import { shouldProcess } from "./webhook.js";

const basePayload = {
  event: "message_created",
  message_type: "incoming",
  content_type: "text",
  conversation: { id: 1, status: "pending" },
};

test("processes a normal incoming text message on a bot-owned (pending) conversation", () => {
  assert.equal(shouldProcess(basePayload).process, true);
});

test("ignores non-message_created events", () => {
  const result = shouldProcess({ ...basePayload, event: "conversation_updated" });
  assert.equal(result.process, false);
  assert.equal(result.reason, "wrong_event");
});

test("ignores outgoing messages (the bot's own sent messages re-triggering the webhook)", () => {
  const result = shouldProcess({ ...basePayload, message_type: "outgoing" });
  assert.equal(result.process, false);
  assert.equal(result.reason, "not_incoming");
});

test("never processes a conversation a human owns (status = open) -- the core safety guard", () => {
  const result = shouldProcess({
    ...basePayload,
    conversation: { id: 1, status: "open" },
  });
  assert.equal(result.process, false);
  assert.equal(result.reason, "not_bot_owned");
});

test("never processes a resolved conversation", () => {
  const result = shouldProcess({
    ...basePayload,
    conversation: { id: 1, status: "resolved" },
  });
  assert.equal(result.process, false);
});

test("flags non-text content as unsupported media instead of running the AI pipeline", () => {
  const result = shouldProcess({ ...basePayload, content_type: "image" });
  assert.equal(result.process, false);
  assert.equal(result.reason, "unsupported_media");
});
