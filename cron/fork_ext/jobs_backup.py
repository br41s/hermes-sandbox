"""Two-tier backup of the cron jobs file before every write (fork-owned).

Moved verbatim out of ``cron/jobs.py`` so the fork's code does not sit inside
upstream's file. ``cron.jobs`` re-imports ``_backup_jobs_file`` and calls it
from ``_save_jobs_unlocked`` exactly as before.

``cron.jobs`` is imported lazily, inside the function, for two reasons: it
imports this module at load time (a top-level import back would be circular),
and resolving ``_current_cron_store`` / ``_secure_file`` / ``_hermes_now`` as
``cron.jobs`` attributes at call time keeps every existing
``monkeypatch.setattr("cron.jobs._hermes_now", ...)`` applying here, just as it
did when this lived in that module.
"""

_BACKUP_KEEP_DAYS = 7


def _backup_jobs_file() -> None:
    """Backup the jobs file before each overwrite.

    Two tiers:
    - <jobs>.bak: single rolling pre-write backup (immediate recovery from
      accidental wipe or single bad write).
    - <cron>/backups/jobs-YYYYMMDD.json: first write per calendar day, pruned
      to _BACKUP_KEEP_DAYS daily snapshots (multi-day data loss).

    Called inside _save_jobs_unlocked() BEFORE the atomic replace so the backup
    always reflects the last known-good state. Never raises — backup failure
    must never block the primary write.

    Resolves the path through ``_current_cron_store()`` rather than the
    module-level ``JOBS_FILE``: when the store is repointed (tests, env
    override) the backup must track the file actually being written, not the
    real one.
    """
    from cron import jobs as _jobs

    jobs_file = _jobs._current_cron_store().jobs_file
    if not jobs_file.exists():
        return
    try:
        import shutil
        # Tier 1 — rolling single-step backup of current file
        bak = jobs_file.with_suffix(".bak")
        shutil.copy2(str(jobs_file), str(bak))
        _jobs._secure_file(bak)
        # Tier 2 — daily snapshot (one per calendar day)
        backup_dir = jobs_file.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        daily = backup_dir / f"jobs-{_jobs._hermes_now().strftime('%Y%m%d')}.json"
        if not daily.exists():
            shutil.copy2(str(jobs_file), str(daily))
            _jobs._secure_file(daily)
            # Prune oldest beyond the retention window
            for old in sorted(backup_dir.glob("jobs-*.json"))[:-_BACKUP_KEEP_DAYS]:
                old.unlink(missing_ok=True)
    except Exception:
        pass  # never fatal
