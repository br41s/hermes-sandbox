#!/usr/bin/env python3
"""Build the site-level awareness aggregate for the Hermes onsite-SEO agent.

Project-agnostic: every specific (web dir, site base, output path, pillars) comes
from args, nothing is hardcoded. Deterministic, stdlib-only, no network.

WHY THIS EXISTS: the cron sandbox blocks `execute_code` and `python3 -c/-e`
(arbitrary unattended code). The agent therefore cannot build the link graph
inline. It invokes THIS committed script by path (`python3 build_sitestate.py ...`),
which is a normal file execution and clears the approval gate — and is versioned
and auditable, the safer trust model.

This builder owns only the file-derived portion of site-state: the internal link
graph, inbound edges, orphans, and broken internal links (an outbound href whose
target isn't any crawled page). `keyword_url_map` and `cannibalization` are GSC-
derived and left for the agent to populate via its GSC tool; pass --merge to
preserve them across runs.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser


class _LinkExtractor(HTMLParser):
    """Collect internal <a href> targets (absolute site URLs) and count body words."""

    _SKIP_TAGS = ("script", "style", "noscript", "template")

    def __init__(self, site_base):
        super().__init__()
        self.site_base = site_base.rstrip("/")
        self.links = []
        self._texts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag != "a":
            return
        for attr, val in attrs:
            if attr != "href" or not val:
                continue
            v = val.strip()
            if v == self.site_base or v.startswith(self.site_base + "/"):
                self.links.append(v)
            elif v.startswith("/") and not v.startswith("//"):
                self.links.append(self.site_base + v)

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._texts.append(data)

    def word_count(self):
        return len(" ".join(self._texts).split())


def _url_for(rel_path, site_base):
    rel = rel_path.replace(os.sep, "/")
    return f"{site_base.rstrip('/')}/{rel}"


def build_graph(web_dir, site_base):
    """Return (graph, errors). Partial graph on per-file failure beats none."""
    graph = {}
    errors = []
    for root, _dirs, files in os.walk(web_dir):
        for fn in files:
            if not fn.endswith(".html"):
                continue
            full = os.path.join(root, fn)
            url = _url_for(os.path.relpath(full, web_dir), site_base)
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    html = fh.read()
                ex = _LinkExtractor(site_base)
                ex.feed(html)
                graph[url] = {"outbound": sorted(set(ex.links)), "inbound": [],
                              "word_count": ex.word_count()}
            except Exception as e:  # noqa: BLE001 — log and continue
                errors.append(f"{url}: {e}")
                graph[url] = {"outbound": [], "inbound": [], "word_count": 0}
    # inbound = inverse of outbound, restricted to edges whose target is a page
    for src, data in graph.items():
        for tgt in data["outbound"]:
            if tgt in graph and src not in graph[tgt]["inbound"]:
                graph[tgt]["inbound"].append(src)
    return graph, errors


def find_orphans(graph, pillars):
    pset = set(pillars)
    return sorted(u for u, d in graph.items() if u not in pset and not d["inbound"])


def find_broken_internal_links(graph):
    """Internal hrefs whose target isn't any crawled page — a 404 in waiting.

    ``build_graph`` already computes this while building inbound edges (an
    outbound target not in ``graph`` is silently excluded from ``inbound``);
    this just keeps the pairs instead of discarding them.
    """
    return {
        src: sorted(tgt for tgt in data["outbound"] if tgt not in graph)
        for src, data in sorted(graph.items())
        if any(tgt not in graph for tgt in data["outbound"])
    }


def git_head(repo):
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — commit is best-effort metadata
        return None


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the SEO site-state aggregate.")
    p.add_argument("--web-dir", required=True, help="Directory of static HTML to scan.")
    p.add_argument("--site-base", required=True, help="e.g. https://biglobster.top")
    p.add_argument("--out", required=True, help="Output site-state.json path.")
    p.add_argument("--repo", help="Git repo for built_against_commit (default: web-dir).")
    p.add_argument("--pillar", action="append", default=[],
                   help="URL excluded from orphan detection (repeatable).")
    p.add_argument("--merge", action="store_true",
                   help="Preserve keyword_url_map/cannibalization already in --out.")
    args = p.parse_args(argv)

    if not os.path.isdir(args.web_dir):
        print(f"ERROR: web-dir not found: {args.web_dir}", file=sys.stderr)
        return 2

    site_base = args.site_base.rstrip("/")
    pillars = set(args.pillar) | {site_base, site_base + "/", site_base + "/index.html"}

    graph, errors = build_graph(args.web_dir, site_base)
    orphans = find_orphans(graph, pillars)
    broken_internal_links = find_broken_internal_links(graph)
    commit = git_head(args.repo or args.web_dir)

    keyword_url_map, cannibalization = {}, []
    if args.merge and os.path.exists(args.out):
        try:
            with open(args.out, "r", encoding="utf-8") as fh:
                prev = json.load(fh)
            keyword_url_map = prev.get("keyword_url_map", {}) or {}
            cannibalization = prev.get("cannibalization", []) or []
        except Exception as e:  # noqa: BLE001 — fall back to empty, don't abort
            print(f"WARN: could not merge prior site-state: {e}", file=sys.stderr)

    state = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "built_against_commit": commit,
        "internal_link_graph": graph,
        "keyword_url_map": keyword_url_map,
        "orphans": orphans,
        "broken_internal_links": broken_internal_links,
        "cannibalization": cannibalization,
    }

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, args.out)  # atomic

    print(f"site-state written: {args.out}")
    broken_count = sum(len(v) for v in broken_internal_links.values())
    print(
        f"pages={len(graph)} orphans={len(orphans)} "
        f"broken_internal_links={broken_count} commit={commit or 'n/a'}"
    )
    if errors:
        print(f"parse_errors={len(errors)} (partial graph)", file=sys.stderr)
        for e in errors[:10]:
            print(f"  {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
