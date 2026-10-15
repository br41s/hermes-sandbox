import { test } from "node:test";
import assert from "node:assert/strict";

test("searchCatalogue asks the site with the visitor's message and never throws", async () => {
  const { searchCatalogue } = await import("./knowledge.js");
  const entry = { chatwootAccountId: 3, siteUrl: "https://shoroban.com/", knowledgeToken: "tok" };
  let seen = null;
  globalThis.fetch = async (url, opts) => { seen = { url, auth: opts.headers.Authorization }; return { ok: true, json: async () => ({ products: [{ sku: "P1" }], categories: [] }) }; };
  const r = await searchCatalogue(entry, { catalogue: { search_path: "/api/knowledge/search" } }, "necesito una pluma");
  assert.equal(seen.url, "https://shoroban.com/api/knowledge/search?q=necesito%20una%20pluma&limit=8");
  assert.equal(seen.auth, "Bearer tok");
  assert.deepEqual(r, { products: [{ sku: "P1" }], categories: [] });
  assert.equal(await searchCatalogue(entry, { catalogue: {} }, "x"), null, "no search_path: no call");
  globalThis.fetch = async () => { throw new Error("ECONNRESET"); };
  assert.equal(await searchCatalogue(entry, { catalogue: { search_path: "/api/knowledge/search" } }, "x"), null);
});
