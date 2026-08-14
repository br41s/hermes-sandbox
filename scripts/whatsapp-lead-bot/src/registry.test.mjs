import { test } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync, unlinkSync } from "node:fs";
import { loadRegistry } from "./registry.js";

const SAMPLE = { "2": { chatwootAccountId: 2, siteUrl: "https://example.com", enabled: true } };

function clearEnv() {
  delete process.env.REGISTRY_JSON_BASE64;
  delete process.env.REGISTRY_JSON;
}

test("loads from REGISTRY_JSON_BASE64 when set", () => {
  clearEnv();
  process.env.REGISTRY_JSON_BASE64 = Buffer.from(JSON.stringify(SAMPLE)).toString("base64");
  const result = loadRegistry();
  assert.deepEqual(result, SAMPLE);
  clearEnv();
});

test("REGISTRY_JSON_BASE64 survives a quote-stripping-style corruption of raw JSON", () => {
  // This is the exact failure mode found live on Zeabur: its .env loader
  // strips double quotes from values, so raw REGISTRY_JSON becomes
  // invalid JSON in the running container. Base64 has no quote
  // characters for that kind of loader to touch.
  clearEnv();
  const raw = JSON.stringify(SAMPLE);
  const corrupted = raw.replace(/"/g, ""); // simulates the quote-stripping bug
  assert.throws(() => JSON.parse(corrupted)); // confirms the bug would break raw JSON
  process.env.REGISTRY_JSON_BASE64 = Buffer.from(raw).toString("base64");
  const result = loadRegistry(); // base64 form is unaffected
  assert.deepEqual(result, SAMPLE);
  clearEnv();
});

test("falls back to REGISTRY_JSON when base64 form is absent", () => {
  clearEnv();
  process.env.REGISTRY_JSON = JSON.stringify(SAMPLE);
  const result = loadRegistry();
  assert.deepEqual(result, SAMPLE);
  clearEnv();
});

test("REGISTRY_JSON_BASE64 takes priority over REGISTRY_JSON when both are set", () => {
  clearEnv();
  process.env.REGISTRY_JSON = JSON.stringify({ "9": { chatwootAccountId: 9 } });
  process.env.REGISTRY_JSON_BASE64 = Buffer.from(JSON.stringify(SAMPLE)).toString("base64");
  const result = loadRegistry();
  assert.deepEqual(result, SAMPLE);
  clearEnv();
});

test("throws a clear error when REGISTRY_JSON_BASE64 doesn't decode to valid JSON", () => {
  clearEnv();
  process.env.REGISTRY_JSON_BASE64 = Buffer.from("not json").toString("base64");
  assert.throws(() => loadRegistry(), /Failed to parse REGISTRY_JSON_BASE64/);
  clearEnv();
});

test("falls back to a file on disk when no env var is set", () => {
  clearEnv();
  const tmpPath = "./__test_registry.json";
  writeFileSync(tmpPath, JSON.stringify(SAMPLE));
  try {
    const result = loadRegistry(tmpPath);
    assert.deepEqual(result, SAMPLE);
  } finally {
    unlinkSync(tmpPath);
  }
});
