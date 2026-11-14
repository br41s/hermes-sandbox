#!/usr/bin/env python3
"""Create (or re-point) the BigLobster community cron job. Idempotent.

    .venv/bin/python3 scripts/setup_social_jobs.py --dry-run
    .venv/bin/python3 scripts/setup_social_jobs.py

Run as `hermes`, never root (root flips jobs.json ownership), from the
hermes-sandbox clone (prompt_source paths resolve against it).

The job is deliberately created in the default store (no `-p`) and WITHOUT
`workdir`, for the same reasons as the two Shorts jobs:

* it runs in the default profile, whose process env carries the Meta and Buzz
  keys straight from Zeabur — so META_* and BUZZ_* never have to be copied
  into every profile's .env by the boot hook;
* the default store's pool is unbounded, while a named profile's runs one job
  at a time (CLAUDE.md, "One cron job at a time per profile; profiles run side
  by side"), so the community job does not queue behind another one.

The schedule is in the configured Hermes timezone, which this script prints.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("HERMES_HOME", os.path.join(os.path.expanduser("~"), ".hermes"))

JOBS = [
    {
        "name": "Community — BigLobster",
        "prompt_source": "social/community.prompt",
        "default_schedule": "0 8,11,14,17,20 * * *",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--schedule", default=JOBS[0]["default_schedule"])
    parser.add_argument("--deliver", default="telegram")
    parser.add_argument("--model", default=None, help="Optional model override for the job")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from cron.jobs import create_job, list_jobs, update_job

    try:
        from hermes_time import now as hermes_now

        print(f"Hermes timezone: {hermes_now().tzinfo}")
    except Exception:  # health: allow BLE001 -- the timezone probe is optional; its fallback prints
        print("Hermes timezone: (could not resolve — check `hermes config`)")

    existing = {j.get("name"): j for j in list_jobs(include_disabled=True)}
    for spec in JOBS:
        prompt = (REPO_ROOT / spec["prompt_source"]).read_text(encoding="utf-8-sig")
        schedule = args.schedule
        job = existing.get(spec["name"])
        if job:
            print(f"= {spec['name']} exists ({job['id']}); re-pointing prompt and toolsets")
            if not args.dry_run:
                update_job(job["id"], {"prompt": prompt, "prompt_source": spec["prompt_source"],
                                       "enabled_toolsets": ["social"]})
            continue
        print(f"+ {spec['name']}: '{schedule}' → {args.deliver}, toolsets [social]")
        if args.dry_run:
            continue
        created = create_job(
            prompt=prompt,
            schedule=schedule,
            name=spec["name"],
            deliver=args.deliver,
            model=args.model,
            enabled_toolsets=["social"],
            prompt_source=spec["prompt_source"],
        )
        print(f"  created {created['id']}")
    if args.dry_run:
        print("(dry run — nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
