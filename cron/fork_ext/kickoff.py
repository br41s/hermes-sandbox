"""Kickoff ping: "🔄 Started: <name>" when a cron job begins running (fork).

Gives users immediate feedback before a (possibly slow) final result arrives,
in the same thread the result will land in, without the "Cronjob Response"
envelope. Non-fatal by contract: every failure is logged and swallowed.

This used to share a low-level ``_send_to_targets`` split out of upstream's
``_deliver_result``. Upstream kept growing that function (relay transports,
continuable surfaces, media policy), so the fork now leaves ``_deliver_result``
exactly as upstream ships it and sends the ping through the same primitives it
uses — ``resolve_delivery_transport`` + ``DeliveryRouter`` on the live gateway
loop, ``_send_to_platform`` standalone — rather than a copy of its loop.

``cron.scheduler`` re-exports ``_send_kickoff_ping`` and ``_send_to_targets``;
both look their collaborators (``load_config``, ``_resolve_delivery_targets``,
``_send_to_targets``, ``_confirm_adapter_delivery``) up on ``cron.scheduler``
at call time, so patches of those attributes keep applying.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import List

logger = logging.getLogger("cron.scheduler")


def _run_standalone(coro_factory):
    """Run an async send from any thread: directly, or on a fresh thread when
    this one already has a running loop."""
    coro = coro_factory()
    try:
        return asyncio.run(coro)
    except RuntimeError:
        coro.close()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(asyncio.run, coro_factory()).result(timeout=30)
        finally:
            pool.shutdown(wait=False)


def send_to_targets(job: dict, targets: List[dict], text: str, media_files: list,
                    config, adapters=None, loop=None) -> List[str]:
    """Send a short text to each resolved target; returns error strings."""
    from cron import scheduler as sched
    from gateway.config import Platform, PlatformConfig
    from gateway.delivery import resolve_delivery_transport
    from tools.send_message_tool import _send_to_platform

    errors: List[str] = []
    for target in targets:
        platform_name = target["platform"]
        chat_id = target["chat_id"]
        thread_id = target.get("thread_id")
        try:
            platform = Platform(platform_name.lower())
        except (ValueError, KeyError):
            errors.append(f"unknown platform '{platform_name}'")
            continue

        transport = None
        try:
            transport = resolve_delivery_transport(platform, config, adapters)
        except Exception:
            logger.debug("kickoff: transport resolution failed for %s", platform_name, exc_info=True)
        if transport is not None:
            pconfig, runtime_adapter = transport.config, transport.adapter
            if pconfig is None and transport.is_relay:
                pconfig = PlatformConfig(enabled=True)
        else:
            pconfig, runtime_adapter = config.platforms.get(platform), None
            if not pconfig or not pconfig.enabled:
                errors.append(f"platform '{platform_name}' not configured/enabled")
                continue

        if runtime_adapter is not None and loop is not None and getattr(loop, "is_running", lambda: False)():
            try:
                from agent.async_utils import safe_schedule_threadsafe
                from gateway.delivery import DeliveryRouter, DeliveryTarget

                route_metadata = {"job_id": job["id"]}
                if thread_id:
                    route_metadata["thread_id"] = str(thread_id)
                future = safe_schedule_threadsafe(
                    DeliveryRouter(config, adapters)._deliver_to_platform(
                        DeliveryTarget(
                            platform=platform,
                            chat_id=str(chat_id),
                            thread_id=str(thread_id) if thread_id is not None else None,
                            is_explicit=True,
                        ),
                        text,
                        route_metadata,
                    ),
                    loop,
                )
                if future is not None and sched._confirm_adapter_delivery(future.result(timeout=30)):
                    continue
            except Exception:
                logger.debug("kickoff: live adapter send failed, falling back", exc_info=True)

        try:
            result = _run_standalone(
                lambda: _send_to_platform(
                    platform, pconfig, chat_id, text,
                    thread_id=thread_id, media_files=media_files,
                )
            )
            if isinstance(result, dict) and result.get("error"):
                errors.append(f"delivery to {platform_name}:{chat_id} failed: {result['error']}")
        except Exception as e:
            errors.append(f"delivery to {platform_name}:{chat_id} failed: {e}")
    return errors


def send_kickoff_ping(job: dict, adapters=None, loop=None) -> None:
    """Post a lightweight "🔄 Started: <name>" ping the moment a job begins running.

    Gated by the job's ``progress_ping`` (True/False overrides) and otherwise
    by ``cron.progress_pings`` (default true); silent for ``local``/empty
    deliver jobs.
    """
    from cron import scheduler as sched

    try:
        per_job = job.get("progress_ping")
        if per_job is False:
            return
        if per_job is None:
            try:
                enabled = sched.load_config().get("cron", {}).get("progress_pings", True)
            except Exception:
                enabled = True
            if not enabled:
                return

        targets = sched._resolve_delivery_targets(job)
        if not targets:
            return

        task_name = job.get("name", job.get("id", "job"))
        schedule = (job.get("schedule_display") or "").strip()
        text = f"🔄 Started: {task_name}"
        if schedule:
            text += f" ({schedule})"

        from gateway.config import load_gateway_config
        try:
            config = load_gateway_config()
        except Exception as e:
            logger.warning(
                "Job '%s': kickoff ping skipped, gateway config load failed: %s",
                job.get("id", "?"), e,
            )
            return

        errors = sched._send_to_targets(job, targets, text, [], config, adapters=adapters, loop=loop)
        if errors:
            logger.warning(
                "Job '%s': kickoff ping had delivery errors: %s",
                job.get("id", "?"), "; ".join(errors),
            )
    except Exception as e:
        logger.warning("Job '%s': kickoff ping failed (non-fatal): %s", job.get("id", "?"), e)
