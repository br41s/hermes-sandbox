#!/usr/bin/env python3
"""Create (or re-point) the two BigLobster Shorts cron jobs. Idempotent.

    .venv/bin/python3 scripts/setup_shorts_jobs.py --dry-run
    .venv/bin/python3 scripts/setup_shorts_jobs.py

Run as `hermes`, never root (root flips jobs.json ownership), from the
hermes-sandbox clone (prompt_source paths resolve against it).

Both jobs are deliberately created WITHOUT `profile` and `workdir`:

* they run in the default profile, whose process env carries the studio and
  publishing keys straight from Zeabur — so those keys never have to be
  copied into every profile's .env by the boot hook;
* profile/workdir jobs share ONE sequential thread (CLAUDE.md, "One long
  agent run starves every other agent"). These two need neither, so they
  stay off it — and neither waits on a render anyway: the producer submits
  and exits, the publisher collects what finished.

Schedules are in the configured Hermes timezone, which this script prints.
The publisher runs twice: once after the producer's renders finish, and once
later to finish anything Instagram was still processing.
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
        "name": "Shorts Producer — BigLobster",
        "prompt_source": "shorts/biglobster-shorts-producer.prompt",
        "flag": "producer",
        "default_schedule": "30 8 * * *",
    },
    {
        "name": "Shorts Publisher — BigLobster",
        "prompt_source": "shorts/biglobster-shorts-publisher.prompt",
        "flag": "publisher",
        "default_schedule": "20 9,13 * * *",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--producer-schedule", default=JOBS[0]["default_schedule"])
    parser.add_argument("--publisher-schedule", default=JOBS[1]["default_schedule"])
    parser.add_argument("--deliver", default="telegram")
    parser.add_argument("--model", default=None, help="Optional model override for both jobs")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from cron.jobs import create_job, list_jobs, update_job

    try:
        from hermes_time import now as hermes_now

        print(f"Hermes timezone: {hermes_now().tzinfo}")
    except Exception:
        print("Hermes timezone: (could not resolve — check `hermes config`)")

    existing = {j.get("name"): j for j in list_jobs(include_disabled=True)}
    schedules = {"producer": args.producer_schedule, "publisher": args.publisher_schedule}
    for spec in JOBS:
        prompt = (REPO_ROOT / spec["prompt_source"]).read_text(encoding="utf-8")
        schedule = schedules[spec["flag"]]
        job = existing.get(spec["name"])
        if job:
            print(f"= {spec['name']} exists ({job['id']}); re-pointing prompt and toolsets")
            if not args.dry_run:
                update_job(job["id"], {"prompt": prompt, "prompt_source": spec["prompt_source"],
                                       "enabled_toolsets": ["shorts"]})
            continue
        print(f"+ {spec['name']}: '{schedule}' → {args.deliver}, toolsets [shorts]")
        if args.dry_run:
            continue
        created = create_job(
            prompt=prompt,
            schedule=schedule,
            name=spec["name"],
            deliver=args.deliver,
            model=args.model,
            enabled_toolsets=["shorts"],
            prompt_source=spec["prompt_source"],
        )
        print(f"  created {created['id']}")
    if args.dry_run:
        print("(dry run — nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
