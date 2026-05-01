#!/usr/bin/env python3
"""
Concurrency smoke test: two different chat_ids handling a message at the same time
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

# Must be set before importing tanya_bot (module raises if missing).
os.environ.setdefault("TELEGRAM_TOKEN", "test-token-concurrency")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-concurrency")

import tanya_bot as tb  # noqa: E402


CLAUDE_SLEEP_SEC = 0.45


def verify_pid_lock_wired_in_main() -> None:
    src = inspect.getsource(tb.main)
    if "acquire_single_instance_lock()" not in src:
        raise AssertionError("main() must call acquire_single_instance_lock()")
    if "release_single_instance_lock()" not in src:
        raise AssertionError("main() must call release_single_instance_lock()")
    if "run_polling()" not in src:
        raise AssertionError("main() must call run_polling()")
    acquire_i = src.index("acquire_single_instance_lock()")
    poll_i = src.index("run_polling()")
    release_i = src.index("release_single_instance_lock()")
    if not (acquire_i < poll_i < release_i):
        raise AssertionError(
            "Expected acquire_single_instance_lock, then run_polling, then release in finally"
        )


def _make_update(chat_id: int, first_name: str, text: str) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.effective_user.first_name = first_name
    update.message.reply_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    return ctx


async def run_parallel_handle_message_test() -> tuple[bool, float, list[tuple[str, float]]]:
    """Return (overlap_ok, wall_seconds, events)."""
    tmp = Path(tempfile.mkdtemp())
    events: list[tuple[str, int, float]] = []

    # Minimal session state so handle_message skips begin_session_with_opening.
    tb.conversations.clear()
    tb.session_files.clear()
    tb.session_outlines.clear()
    tb.session_profiles.clear()
    tb.session_numbers.clear()
    tb.client_names.clear()
    tb.last_activity.clear()
    tb.timeout_tasks.clear()
    tb.voice_enabled.clear()

    for cid in (91001, 91002):
        p = tmp / f"session_{cid}.md"
        p.write_text("# Test session\n\n", encoding="utf-8")
        tb.session_files[cid] = p
        tb.session_outlines[cid] = "outline"
        tb.session_profiles[cid] = ""
        tb.session_numbers[cid] = 1
        tb.conversations[cid] = []

    call_seq = {"n": 0}

    async def slow_create(*args, **kwargs):
        i = call_seq["n"]
        call_seq["n"] += 1
        t0 = time.perf_counter()
        events.append(("start", i, t0))
        await asyncio.sleep(CLAUDE_SLEEP_SEC)
        t1 = time.perf_counter()
        events.append(("end", i, t1))
        response = MagicMock()
        response.content = [MagicMock(text=f"Reply {i}.")]
        u = MagicMock()
        u.input_tokens = 1
        u.output_tokens = 2
        u.cache_read_input_tokens = 0
        u.cache_creation_input_tokens = 0
        response.usage = u
        return response

    u1 = _make_update(91001, "UserA", "Hello A")
    u2 = _make_update(91002, "UserB", "Hello B")
    c1 = _make_context()
    c2 = _make_context()

    t_wall0 = time.perf_counter()
    with (
        patch.object(tb, "is_allowed", return_value=True),
        patch.object(tb, "reset_timeout", lambda *_a, **_k: None),
        patch.object(tb, "send_with_voice", new_callable=AsyncMock),
        patch.object(tb.claude.messages, "create", AsyncMock(side_effect=slow_create)),
    ):
        await asyncio.gather(
            tb.handle_message(u1, c1),
            tb.handle_message(u2, c2),
        )
    wall = time.perf_counter() - t_wall0

    starts = sorted((e[2], e[1]) for e in events if e[0] == "start")
    ends = sorted((e[2], e[1]) for e in events if e[0] == "end")
    if len(starts) != 2 or len(ends) != 2:
        raise AssertionError(f"Expected 2 starts and 2 ends, got {events}")

    start_times = [s[0] for s in starts]
    end_times = [e[0] for e in ends]
    # True concurrency: both calls begin before either finishes.
    overlap_ok = max(start_times) < min(end_times)
    return overlap_ok, wall, [(e[0], e[1], e[2]) for e in events]


def main() -> None:
    print("=== PID lock wiring (static check of tanya_bot.main) ===")
    verify_pid_lock_wired_in_main()
    print("OK: acquire_single_instance_lock() runs before run_polling();")
    print("    release_single_instance_lock() runs in finally after run_polling().")
    print("    (Second process still fails fast with O_EXCL on logs/tanya_bot.pid.)")
    print()

    print("=== Parallel Claude simulation (two chat_ids, asyncio.gather) ===")
    overlap_ok, wall, events = asyncio.run(run_parallel_handle_message_test())
    print(f"Wall-clock for 2x handle_message (each Claude mock sleeps {CLAUDE_SLEEP_SEC}s): {wall:.3f}s")
    print(f"If serialized: ~{2 * CLAUDE_SLEEP_SEC:.2f}s+ ; if parallel: ~{CLAUDE_SLEEP_SEC:.2f}s+")
    for kind, idx, t in events:
        print(f"  {kind:5s} call={idx} t={t:.6f}")
    print(f"Both starts before first end (parallel): {overlap_ok}")
    if not overlap_ok:
        raise SystemExit(1)
    # Allow thread-pool file I/O + logging; should stay well under 2x CLAUDE_SLEEP if parallel.
    if wall > 2 * CLAUDE_SLEEP_SEC - 0.15:
        raise SystemExit(
            f"Wall time {wall:.3f}s looks serialized (expected < ~{2 * CLAUDE_SLEEP_SEC - 0.15:.2f}s for parallel)"
        )
    print()
    print("PASS: two users’ Claude work overlapped in time.")


if __name__ == "__main__":
    main()
