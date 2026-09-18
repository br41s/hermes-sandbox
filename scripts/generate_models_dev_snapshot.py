#!/usr/bin/env python3
"""Regenerate the bundled models.dev provider snapshot.

``agent/models_dev.py`` resolves provider and model metadata from
``https://models.dev/api.json``, cached on disk at
``$HERMES_HOME/models_dev_cache.json``. On a cold install with no network
(or an egress-filtered host) both are unavailable, and every provider whose
API-key env var is only known to models.dev resolves to nothing — most
visibly ``openrouter``, which is deliberately excluded from
``hermes_cli.auth.PROVIDER_REGISTRY``. ``is_provider_explicitly_configured()``
then returns False for a provider whose key IS set, hiding it from the
desktop model picker.

This script writes the offline floor for that path: the provider-level half
of ``api.json``, with the per-model payload dropped. The full document is
~4.7 MB across 4000+ models; the trimmed snapshot is ~35 KB, because
provider identity is what the offline path actually needs — context windows
and prices are useless without a network to spend them on.

The snapshot is a *last resort*, never a shortcut: the disk cache and the
network both take precedence, so a fresh install that can reach models.dev
never reads it. It is therefore allowed to age between releases.

Usage::

    python scripts/generate_models_dev_snapshot.py

Output: ``agent/models_dev_snapshot.json``
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

MODELS_DEV_URL = "https://models.dev/api.json"
OUTPUT_PATH = os.path.join(REPO_ROOT, "agent", "models_dev_snapshot.json")

# Provider keys ``agent.models_dev._parse_provider_info`` reads. Anything
# else in the upstream entry (notably ``models``, which is 99% of the bytes,
# and ``npm``, which is an AI-SDK detail Hermes never consults) is dropped.
KEEP_KEYS = ("id", "name", "env", "api", "doc")


def trim(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce the full api.json to provider metadata only."""
    out: Dict[str, Any] = {}
    for provider_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        trimmed = {k: entry[k] for k in KEEP_KEYS if entry.get(k) is not None}
        # ``env`` is the whole point of the snapshot — a provider without one
        # cannot be auto-detected from the environment, so it buys us nothing.
        if not trimmed.get("env"):
            continue
        trimmed.setdefault("id", provider_id)
        out[provider_id] = trimmed
    return out


def main() -> int:
    print(f"Fetching {MODELS_DEV_URL} ...")
    response = requests.get(MODELS_DEV_URL, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data:
        print("models.dev returned an unexpected payload; refusing to write.", file=sys.stderr)
        return 1

    snapshot = trim(data)
    if len(snapshot) < 50:
        # Sanity floor: the catalog has carried 100+ providers for years.
        # A near-empty result means upstream changed shape — fail loudly
        # rather than shipping a snapshot that silently covers nothing.
        print(f"Only {len(snapshot)} providers survived trimming; refusing to write.", file=sys.stderr)
        return 1

    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    size_kb = os.path.getsize(OUTPUT_PATH) / 1024
    print(f"Wrote {len(snapshot)} providers to {OUTPUT_PATH} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
