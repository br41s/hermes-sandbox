/**
 * account_id -> { chatwootAccountId, siteUrl, knowledgeToken, companyName,
 * enabled } lookup.
 *
 * MVP storage: a single JSON blob, loaded once at startup. No hot-reload,
 * no database -- restart the service to pick up a new/changed customer
 * entry. This matches the project's "no new infra unless it earns it"
 * default: at pilot scale (a handful of customers) a flat file is not a
 * bottleneck, and it keeps the bot service from needing its own database
 * on top of Chatwoot's.
 *
 * Three sources, checked in order:
 *
 * 1. REGISTRY_JSON_BASE64 -- the registry content as base64-encoded JSON.
 *    This is the one that actually works on Zeabur: its `variable env`
 *    .env-file loader silently strips double quotes from values, which
 *    corrupts raw JSON (found live -- {"a":"b"} became {a:b} in the
 *    running container, breaking JSON.parse). Base64's alphabet has no
 *    quote/comma/brace characters for any such loader to mangle.
 * 2. REGISTRY_JSON -- raw JSON string. Kept for environments that don't
 *    have that quote-stripping problem (e.g. a plain shell export).
 * 3. REGISTRY_PATH -- a file on disk, for local dev convenience.
 *
 * Revisit with a real store once onboarding a customer needs to happen
 * without a deploy.
 */

import { readFileSync } from "node:fs";

let registry = null;

export function loadRegistry(path = process.env.REGISTRY_PATH || "./registry.json") {
  const base64Json = process.env.REGISTRY_JSON_BASE64;
  if (base64Json) {
    try {
      registry = JSON.parse(Buffer.from(base64Json, "base64").toString("utf8"));
      return registry;
    } catch (err) {
      throw new Error(`Failed to parse REGISTRY_JSON_BASE64: ${err.message}`);
    }
  }
  const inlineJson = process.env.REGISTRY_JSON;
  if (inlineJson) {
    try {
      registry = JSON.parse(inlineJson);
      return registry;
    } catch (err) {
      throw new Error(`Failed to parse REGISTRY_JSON: ${err.message}`);
    }
  }
  try {
    const raw = readFileSync(path, "utf8");
    registry = JSON.parse(raw);
  } catch (err) {
    throw new Error(`Failed to load registry from ${path}: ${err.message}`);
  }
  return registry;
}

export function getRegistryEntry(chatwootAccountId) {
  if (registry === null) loadRegistry();
  const entry = registry[String(chatwootAccountId)];
  if (!entry || entry.enabled === false) return null;
  return entry;
}

export function registrySize() {
  if (registry === null) loadRegistry();
  return Object.values(registry).filter((e) => e.enabled !== false).length;
}
