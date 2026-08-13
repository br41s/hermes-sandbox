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
