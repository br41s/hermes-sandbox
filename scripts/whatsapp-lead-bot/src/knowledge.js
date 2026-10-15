/**
 * Fetches and caches GET /api/knowledge from a customer's bl-site-package
 * deployment. Short TTL cache (KNOWLEDGE_CACHE_TTL_MS) because site
 * content changes rarely but a webhook-driven conversation can have
 * several turns in a row -- no reason to re-fetch every turn.
 */

const cache = new Map(); // chatwootAccountId -> { data, fetchedAt }

function ttlMs() {
  return Number(process.env.KNOWLEDGE_CACHE_TTL_MS || 300000);
}

// Returns the knowledge payload, or null if the fetch failed and no
// usable cache existed. Never throws -- callers (policy.js, via the
// webhook handler) treat a null return as "no_knowledge_available" and
// escalate, per the never-answer-ungrounded rule.
export async function fetchKnowledge(entry) {
  const cached = cache.get(entry.chatwootAccountId);
  const fresh = cached && Date.now() - cached.fetchedAt < ttlMs();
  if (fresh) return cached.data;

  try {
    const res = await fetch(`${entry.siteUrl.replace(/\/+$/, "")}/api/knowledge`, {
      headers: { Authorization: `Bearer ${entry.knowledgeToken}` },
    });
    if (!res.ok) throw new Error(`knowledge fetch failed: ${res.status}`);
    const data = await res.json();
    cache.set(entry.chatwootAccountId, { data, fetchedAt: Date.now() });
    return data;
  } catch (err) {
    // Stale-but-present cache beats no knowledge at all -- a site that's
    // briefly unreachable shouldn't force every in-flight conversation to
    // escalate if we fetched successfully a few minutes ago.
    if (cached) return cached.data;
    console.error(
      `knowledge fetch failed for account ${entry.chatwootAccountId}: ${err.message}`,
    );
    return null;
  }
}

export function clearKnowledgeCache() {
  cache.clear();
}

export function knowledgeCacheState() {
  return Object.fromEntries(
    [...cache.entries()].map(([accountId, { fetchedAt }]) => [
      accountId,
      { ageMs: Date.now() - fetchedAt },
    ]),
  );
}

// Per-turn catalogue lookup, when the site offers one (`catalogue.search_path`
// in the payload). The payload carries a 60-product sample of a 20,000-product
// catalogue; without this the model guessed products, prices and URLs for a
// "pluma de buena calidad" (Shoroban, 2026-10-15). Never throws: null means
// "no matches available", and the model falls back to the sample + categories.
export async function searchCatalogue(entry, knowledge, query) {
  const path = knowledge?.catalogue?.search_path;
  if (!path || !query) return null;
  const url = `${entry.siteUrl.replace(/\/+$/, "")}${path}?q=${encodeURIComponent(String(query).slice(0, 300))}&limit=8`;
  try {
    const res = await fetch(url, {
      headers: { Authorization: `Bearer ${entry.knowledgeToken}` },
      signal: AbortSignal.timeout(Number(process.env.KNOWLEDGE_SEARCH_TIMEOUT_MS || 6000)),
    });
    if (!res.ok) throw new Error(`catalogue search failed: ${res.status}`);
    const data = await res.json();
    return { products: Array.isArray(data.products) ? data.products : [], categories: Array.isArray(data.categories) ? data.categories : [] };
  } catch (err) {
    console.error(`catalogue search failed for account ${entry.chatwootAccountId}: ${err.message}`);
    return null;
  }
}
