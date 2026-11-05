# GSC (Google Search Console) — read-only MCP server

First-party, **baked into the image** (not installed via `hermes mcp install`).
Gives the BigLobster SEO/GEO agent read access to Search Console analytics
through a dedicated **service account** with **read-only** scope.

Unlike the catalog entries in this directory (`linear/`, `n8n/`), there is no
`manifest.yaml`: this server is wired directly in `docker/config.yaml` under
`mcp_servers.gsc`, so it is present on every container boot with no install step.

## Why first-party + service account

The BigLobster Google identity was banned once for bot activity and reinstated
on appeal. All code touching that identity stays in-repo and auditable. The
Search Analytics API is sanctioned and metered — it does not route through
Google's anti-abuse systems — and a read-only service account is a separate
identity from the human account, so this path does not re-create the ban risk.

## Tools

- `gsc_list_sites()` — list properties the service account can read (use to
  verify the credential + property grant).
- `gsc_search_analytics(site_url, start_date, end_date, dimensions, row_limit, search_type)`
  — clicks / impressions / CTR / position.

## Setup

1. **GCP:** enable the Google Search Console API, create a service account, and
   download a JSON key.
2. **Search Console:** add the service account's `client_email` as a
   **Restricted** (read) user on the `biglobster.top` property
   (Settings → Users and permissions).
3. **Credential delivery:** base64-encode the key (unwrapped) and set it as a
   Zeabur env var. In the container shell:
   ```sh
   base64 -w0 /path/to/key.json
   ```
   Set the output as `GSC_SERVICE_ACCOUNT_B64`. (`docker/cont-init.d/03-biglobster-config`
   syncs it into `.env`; `config.yaml` interpolates it into the server's env.)

## Verify

After deploy, in the container:
```sh
/opt/hermes/.venv/bin/python optional-mcps/gsc/server.py   # should start, no error
```
Then ask the agent (or a test session) to call `gsc_list_sites` — it should
return the `biglobster.top` property. A 403 means the service-account email was
not added to the property.

Zero extra dependencies: `mcp` ships via the `[all]` extra; `PyJWT[crypto]` and
`requests` are core deps.
