"""Fork additions to the TERMINAL_CWD readers-writer lock tests.

Moved verbatim out of ``tests/cron/test_terminal_cwd_lock.py``, which is
upstream's file (kept byte-identical to v2026.7.20) and which upstream deletes
at v2026.8.31 together with ``_ReadWriteLock`` itself. When that merge lands,
these tests go with the lock: serializing profile/workdir jobs against EACH
OTHER is then carried by the fork's sequential lane alone
(``cron/fork_ext/dispatch.py``, gated by
``tests/cron/test_sequential_dispatch_fork.py``).
"""

import threading


def _lock():
    import cron.scheduler as sched

    return sched._ReadWriteLock()


class TestProfileJobsAreWriters:
    """A profile job takes the WRITER lock even without a workdir.

    `_run_job_impl` calls `load_hermes_dotenv` INSIDE the lock region, which
    mutates the PROCESS environment with that profile's .env; only
    `_job_profile_context` restores it, on exit. So while a profile job runs,
    any concurrently running job reads its keys. Excluding them for the
    duration is what makes that snapshot/restore pair sound.

    This held for free until upstream's dispatch rewrite moved sequential jobs
    off the tick thread onto a `cron-seq` pool that runs alongside
    `cron-parallel` — the same change that turned the `_hermes_home` module
    global into a live cross-job leak. The environment is that bug's twin.

    Measured against the live fleet when this landed: 6 of 7 profile jobs
    already took the writer lock because they carry a workdir, so exactly one
    job changed behaviour.
    """

    @staticmethod
    def _holds_write(job: dict) -> bool:
        """Mirror of the predicate in `_run_job_impl`."""
        workdir = job.get("workdir")
        return workdir is not None or bool(str(job.get("profile") or "").strip())

    def test_profile_without_workdir_is_a_writer(self):
        assert self._holds_write({"profile": "bl-shoroban"}) is True

    def test_workdir_without_profile_is_still_a_writer(self):
        assert self._holds_write({"workdir": "/srv/proj"}) is True

    def test_plain_job_stays_a_reader(self):
        """Guard against over-reach: profile-less, workdir-less jobs (the
        incident-watcher, the drift watcher) must stay concurrent."""
        assert self._holds_write({}) is False
        assert self._holds_write({"profile": None, "workdir": None}) is False
        assert self._holds_write({"profile": "   "}) is False

    def test_writer_excludes_a_concurrent_reader(self):
        """The property that matters: while a profile job holds the lock, a
        profile-less job cannot be inside its own run reading the mutated env."""
        lock = _lock()
        reader_entered = threading.Event()

        lock.acquire_write()  # stand in for the profile job

        def reader():
            lock.acquire_read()
            try:
                reader_entered.set()
            finally:
                lock.release_read()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        assert not reader_entered.wait(timeout=0.5), (
            "a profile-less job entered its run while a profile job held the "
            "writer lock — it would observe that profile's environment"
        )
        lock.release_write()
        assert reader_entered.wait(timeout=5)
        t.join(timeout=5)
