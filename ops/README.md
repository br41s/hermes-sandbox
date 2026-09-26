# ops/ — the fork's own runbooks and security records

Deployment runbooks (Zeabur), incident write-ups and security procedures for
this fork. They live here, not in `docs/`, on purpose:

- **Upstream folded `docs/` into the public Docusaurus site** (`website/docs/`)
  in v2026.9.x. Anything left in `docs/` gets carried there by the merge's
  directory-rename detection, so our incident and security runbooks would end
  up published.
- **A directory upstream does not have never conflicts.** These files cost
  seven "file location" conflicts on the v2026.9.24 merge while they sat in
  `docs/`.

Keep new fork-only docs here (or in `tasks/` for plans), never in `docs/` or
`website/`.
