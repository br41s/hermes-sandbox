"""deploy.sh refuses to cut a cron run in flight (2026-10-04).

Moving the tag restarts the gateway; a run it interrupts fails. On 2026-10-04 a
rental's 14:12 Product Sheet run was lost to a deploy started after a check that
only confirmed the *previous* run had finished. deploy.sh now asks the pod which
executions are claimed or running, across the default store and every profile
store, before it moves the tag.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
TEXT = DEPLOY_SH.read_text(encoding="utf-8")


def _probe() -> str:
    return re.search(r"IN_FLIGHT_PY='\n(.*?)\n'\n", TEXT, re.S).group(1)


def _store(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table executions (id text, job_id text, status text, claimed_at text)")
    con.executemany("insert into executions values (?, ?, ?, ?)", rows)
    con.commit()
    con.close()


def _run(root: Path) -> list[str]:
    out = subprocess.run([sys.executable, "-c", _probe(), str(root)],
                         capture_output=True, text=True, timeout=30, check=True)
    return out.stdout.splitlines()


def test_the_probe_sees_runs_in_every_store(tmp_path) -> None:
    _store(tmp_path / "cron" / "executions.db",
           [("e1", "default-job", "completed", "t0"), ("e2", "default-job", "running", "t1")])
    _store(tmp_path / "profiles" / "bl-shoroban" / "cron" / "executions.db",
           [("e3", "b2f774557766", "claimed", "t2"), ("e4", "b2f774557766", "failed", "t3")])
    lines = _run(tmp_path)
    assert lines[-1] == "__in_flight_check_ok__"
    jobs = sorted(line.split()[:2][0] + ":" + line.split()[1] for line in lines[:-1])
    assert jobs == ["b2f774557766:claimed", "default-job:running"]


def test_an_idle_fleet_prints_only_the_marker(tmp_path) -> None:
    _store(tmp_path / "cron" / "executions.db", [("e1", "j", "completed", "t0")])
    assert _run(tmp_path) == ["__in_flight_check_ok__"]


def test_the_check_runs_before_the_tag_moves_and_the_prompt() -> None:
    check = TEXT.index("→ Checking for cron runs in flight")
    assert check < TEXT.index('confirm "Deploy $TAG? [y/N] "')
    assert check < TEXT.index("run zeabur service update tag")


def test_yes_refuses_unless_forced_and_a_missing_marker_is_not_idle() -> None:
    block = TEXT[TEXT.index("→ Checking for cron runs in flight"):TEXT.index("could not ask the pod")]
    assert 'if [ "$FORCE_IN_FLIGHT" -eq 1 ]' in block
    assert 'elif [ "$ASSUME_YES" -eq 1 ]' in block and "exit 1" in block
    # Without the marker the answer is "unknown", never "nothing running".
    assert "grep -q '^__in_flight_check_ok__$'" in block
    assert "--force-in-flight) FORCE_IN_FLIGHT=1" in TEXT
