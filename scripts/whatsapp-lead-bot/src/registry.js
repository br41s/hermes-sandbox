/**
 * account_id -> { chatwootAccountId, siteUrl, knowledgeToken, companyName,
 * enabled } lookup.
 *
 * MVP storage: a single JSON file (REGISTRY_PATH, see .env.example),
 * loaded once at startup. No hot-reload, no database -- restart the
 * service to pick up a new/changed customer entry. This matches the
 * project's "no new infra unless it earns it" default: at pilot scale (a
 * handful of customers) a flat file is not a bottleneck, and it keeps the
 * bot service from needing its own database on top of Chatwoot's.
 *
 * Revisit with a real store once onboarding a customer needs to happen
 * without a deploy.
 */

import { readFileSync } from "node:fs";

let registry = null;

export function loadRegistry(path = process.env.REGISTRY_PATH || "./registry.json") {
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
