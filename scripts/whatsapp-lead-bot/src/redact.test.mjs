import { test } from "node:test";
import assert from "node:assert/strict";
import { maskPhone, redactBody, redactForLog } from "./redact.js";

test("maskPhone keeps country code and last 3 digits only", () => {
  assert.equal(maskPhone("+34600111222"), "+34 ••• ••• 222");
});

test("maskPhone handles missing/empty input safely", () => {
  assert.equal(maskPhone(undefined), "(sin número)");
  assert.equal(maskPhone(""), "(sin número)");
});

test("redactBody never includes the full message content", () => {
  const long = "Necesito un presupuesto para reformar mi cocina de 12 metros cuadrados";
  const result = redactBody(long, 10);
  assert.ok(!result.includes(long));
  assert.match(result, /\[\d+ chars\]/);
});

test("redactBody handles empty content", () => {
  assert.equal(redactBody(""), "(vacío)");
  assert.equal(redactBody(null), "(vacío)");
});

test("redactForLog strips known-sensitive keys at any depth", () => {
  const input = {
    conversation_id: 42,
    status: "pending",
    sender: { phone: "+34600111222", email: "a@b.com" },
    api_access_token: "secret-token",
  };
  const out = redactForLog(input);
  assert.equal(out.conversation_id, 42);
  assert.equal(out.status, "pending");
  assert.equal(out.sender.phone, "[redacted]");
  assert.equal(out.sender.email, "[redacted]");
  assert.equal(out.api_access_token, "[redacted]");
});
