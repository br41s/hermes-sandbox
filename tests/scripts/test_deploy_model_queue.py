"""deploy.sh applies the model updates model_watch queued, only while Hermes is stopped (2026-11-21).

The weekly model_watch run never restarts anything: it queues same-family upgrades, and the next
deploy sets them in Coolify between the stop and the start, so the new container boots with them
and apply-pins moves the cron jobs pinned to the old model. See ops/model-watch.md.
"""
from __future__ import annotations

import re
from pathlib import Path

TEXT = (Path(__file__).resolve().parents[2] / "scripts" / "deploy.sh").read_text(encoding="utf-8")


def test_queue_is_read_before_the_prompt_and_set_between_stop_and_start() -> None:
    read = TEXT.index("-m model_watch queue-env")
    prompt = TEXT.index('confirm "Deploy $TAG? [y/N] "')
    stop = TEXT.index("run coolify POST /stop")
    env_patch = TEXT.index("if coolify PATCH /envs")
    start = TEXT.index("run coolify POST /start")
    assert read < prompt < stop < env_patch < start


def test_only_model_ids_reach_coolify() -> None:
    pattern = re.search(r"grep -E '(\^\(HERMES_DEFAULT_MODEL[^']+)'", TEXT).group(1)
    ok = re.compile(pattern)
    assert ok.match("HERMES_DEFAULT_MODEL=deepseek/deepseek-v4.1-flash-1017")
    assert ok.match("OPENROUTER_IMAGE_MODEL=qwen/qwen-image-2.1-turbo")
    # queue.json is writable by agents: no other Coolify variable may ride the queue.
    assert not ok.match("HERMES_FORK_SCOPE_PROCESS_ENV=0")
    assert not ok.match("TELEGRAM_ALLOWED_USERS=123")
    assert not ok.match("HERMES_DEFAULT_MODEL=x; rm -rf /")
    assert not ok.match('HERMES_DEFAULT_MODEL=a"b')


def test_the_queue_can_be_left_for_a_later_deploy() -> None:
    assert "--skip-model-queue) SKIP_MODEL_QUEUE=1" in TEXT
    assert 'if [ "$SKIP_MODEL_QUEUE" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then' in TEXT
