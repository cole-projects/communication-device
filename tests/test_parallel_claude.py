#!/usr/bin/env python3
"""
Concurrency smoke test: two phones handling a coaching turn at the same time
should overlap slow Claude work (not run strictly one-after-the-other).

Also verifies PID single-instance lock is still wired in main().

Run from repo root:
  cd "Communication Device" && python3 tests/test_parallel_claude.py
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_COMM_DEVICE_ROOT = Path(__file__).resolve().parent.parent
if str(_COMM_DEVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_COMM_DEVICE_ROOT))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-concurrency")
os.environ.setdefault("BLOOIO_API_KEY", "test-blooio-key")

import tanya_bot as tb  # noqa: E402


CLAUDE_SLEEP_SEC = 0.45


def verify_pid_lock_wired_in_main() -> None:
    src = inspect.getsource(tb.main)
    if "acquire_single_instance_lock()" not in src:
        raise AssertionError("main() must call acquire_single_instance_lock()")
    if "release_single_instance_lock()" not in src:
        raise AssertionError("main() must call release_single_instance_lock()")
    if "uvicorn.run(" not in src:
        raise AssertionError("main() must call uvicorn.run()")
    acquire_i = src.index("acquire_single_instance_lock()")
    run_i = src.index("uvicorn.run(")
    release_i = src.index("release_single_instance_lock()")
    if not (acquire_i < run_i < release_i):
        raise AssertionError(
            "Expected acquire_single_instance_lock, then uvicorn.run, then release in finally"
        )


async def run_parallel_coaching_test() -> tuple[bool, float]:
    """Return (overlap_ok, wall_seconds)."""
    tmp = Path(tempfile.mkdtemp())
    events: list[tuple[str, int, float]] = []

    tb.conversations.clear()
    tb.session_files.clear()
    tb.session_outlines.clear()
    tb.session_profiles.clear()
    tb.session_numbers.clear()
    tb.client_names.clear()
    tb.last_activity.clear()
    tb.timeout_tasks.clear()

    phones = ("+19100000001", "+19100000002")
    for phone in phones:
        p = tmp / f"session_{phone}.md"
        p.write_text("# Test session\n\n", encoding="utf-8")
        tb.session_files[phone] = p
        tb.session_outlines[phone] = "outline"
        tb.session_profiles[phone] = ""
        tb.session_numbers[phone] = 2
        tb.conversations[phone] = [{"role": "assistant", "content": "Hi"}]
        tb.client_names[phone] = "Test"

    call_seq = {"n": 0}

    async def slow_create(*args, **kwargs):
        i = call_seq["n"]
        call_seq["n"] += 1
        events.append(("start", i, time.perf_counter()))
        await asyncio.sleep(CLAUDE_SLEEP_SEC)
        events.append(("end", i, time.perf_counter()))
        response = MagicMock()
        response.content = [MagicMock(text=f"Reply {i}.")]
        u = MagicMock()
        u.input_tokens = 1
        u.output_tokens = 2
        u.cache_read_input_tokens = 0
        u.cache_creation_input_tokens = 0
        response.usage = u
        return response

    t_wall0 = time.perf_counter()
    with (
        patch.object(tb, "blooio_typing_on", new_callable=AsyncMock),
        patch.object(tb, "should_block_unpaid_after_free_trial", new_callable=AsyncMock, return_value=False),
        patch.object(tb, "has_tanyatalk_access", new_callable=AsyncMock, return_value=False),
        patch.object(tb, "in_first_free_trial_session", new_callable=AsyncMock, return_value=False),
        patch.object(tb, "select_frameworks_for_session", new_callable=AsyncMock, return_value=[]),
        patch.object(tb, "start_cache_warming", lambda *_a, **_k: None),
        patch.object(tb, "record_coaching_usage", lambda *_a, **_k: None),
        patch.object(tb, "reset_timeout", lambda *_a, **_k: None),
        patch.object(tb, "blooio_send_message", new_callable=AsyncMock),
        patch.object(tb, "RESPONSE_DELAY_SECONDS", 0),
        patch.object(tb, "_claude_create", AsyncMock(side_effect=slow_create)),
    ):
        await asyncio.gather(
            tb._fire_coaching_message(phones[0], "UserA", "Hello A"),
            tb._fire_coaching_message(phones[1], "UserB", "Hello B"),
        )
    wall = time.perf_counter() - t_wall0

    starts = sorted((e[2], e[1]) for e in events if e[0] == "start")
    ends = sorted((e[2], e[1]) for e in events if e[0] == "end")
    if len(starts) < 2 or len(ends) < 2:
        raise AssertionError(f"Expected at least 2 Claude calls, got {events}")

    # True concurrency: some call begins before another finishes.
    overlap_ok = max(s[0] for s in starts[:2]) < min(e[0] for e in ends[:2])
    return overlap_ok, wall


def main() -> None:
    print("=== PID lock wiring (static check of tanya_bot.main) ===")
    verify_pid_lock_wired_in_main()
    print("OK: acquire_single_instance_lock() runs before uvicorn.run();")
    print("    release_single_instance_lock() runs in finally after uvicorn.run().")
    print()

    print("=== Parallel Claude simulation (two phones, asyncio.gather) ===")
    overlap_ok, wall = asyncio.run(run_parallel_coaching_test())
    print(f"Wall-clock for 2x _fire_coaching_message (each Claude mock sleeps {CLAUDE_SLEEP_SEC}s): {wall:.3f}s")
    print(f"If serialized: ~{2 * CLAUDE_SLEEP_SEC:.2f}s+ ; if parallel: ~{CLAUDE_SLEEP_SEC:.2f}s+")
    print(f"Both starts before first end (parallel): {overlap_ok}")
    if not overlap_ok:
        raise SystemExit(1)
    if wall > 2 * CLAUDE_SLEEP_SEC - 0.15:
        raise SystemExit(
            f"Wall time {wall:.3f}s looks serialized (expected < ~{2 * CLAUDE_SLEEP_SEC - 0.15:.2f}s for parallel)"
        )
    print()
    print("PASS: two clients' Claude work overlapped in time.")


if __name__ == "__main__":
    main()
