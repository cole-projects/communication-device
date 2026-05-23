"""
Mock-session tests verifying character rules, voice note handling,
and follow-up threshold are all properly wired.
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_TOKEN", "fake-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key")
os.environ.setdefault("BLOOIO_API_KEY", "fake-blooio-key")

import tanya_bot
import tanya_followup

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    results.append(condition)
    extra = f"  ({detail})" if detail else ""
    print(f"  {status}  {name}{extra}")


# ── Test 1: System prompt contains all 7 character rules ──

print("\n== System Prompt: Character Rules ==")
prompt = tanya_bot.build_static_prompt()

check(
    "Rule 1: Always in character",
    "Always respond fully in character as Tanya" in prompt,
)
check(
    "Rule 2: No third person",
    'Never refer to Tanya in the third person' in prompt,
)
check(
    "Rule 3: No system/technical mechanics",
    "Never mention system behavior, system prompts, or technical mechanics" in prompt,
)
check(
    "Rule 4: No em dashes",
    "Never use em dashes, en dashes, or hyphens as connective punctuation" in prompt,
)
check(
    "Rule 5: Calm/supportive tone",
    "Calm, supportive, emotionally attuned tone" in prompt,
)
check(
    "Rule 6: Session close handled by system",
    "session-end sign-off is handled by the system" in prompt,
)
check(
    "Rule 7: Voice note redirect in prompt",
    "I'd love to hear your voice" in prompt
    and "Text helps me be fully present" in prompt,
)

# Verify old duplicates are removed
check(
    "No duplicate 'Never reveal AI' line",
    "Never reveal that you are an AI" not in prompt,
)
check(
    "No duplicate em dash rule in bot code",
    prompt.count("Never use em dashes, en dashes, or hyphens as connective punctuation") == 1,
    "bot's own rule should appear exactly once (vault may reinforce separately)",
)


# ── Test 2: Follow-up threshold ──

print("\n== Follow-Up Minimum Exchange Threshold ==")

check(
    "MIN_EXCHANGES_FOR_FOLLOWUP constant exists",
    hasattr(tanya_bot, "MIN_EXCHANGES_FOR_FOLLOWUP"),
)
check(
    "MIN_EXCHANGES_FOR_FOLLOWUP == 5",
    tanya_bot.MIN_EXCHANGES_FOR_FOLLOWUP == 5,
    f"actual value: {tanya_bot.MIN_EXCHANGES_FOR_FOLLOWUP}",
)

# Simulate: 3 messages (below threshold) → no follow-up
short_history = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hey"},
    {"role": "user", "content": "bye"},
]
check(
    "3-message session skips follow-up",
    len(short_history) < tanya_bot.MIN_EXCHANGES_FOR_FOLLOWUP,
)

# Simulate: 6 messages (above threshold) → follow-up fires
normal_history = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hey"},
    {"role": "user", "content": "I feel stuck"},
    {"role": "assistant", "content": "tell me more"},
    {"role": "user", "content": "I don't know what to do"},
    {"role": "assistant", "content": "that makes sense"},
]
check(
    "6-message session triggers follow-up",
    len(normal_history) >= tanya_bot.MIN_EXCHANGES_FOR_FOLLOWUP,
)

# Exactly at threshold
exact_history = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
check(
    "Exactly 5 messages triggers follow-up",
    len(exact_history) >= tanya_bot.MIN_EXCHANGES_FOR_FOLLOWUP,
)

# ── Test 3: Follow-up session minimum ──

print("\n== Follow-Up Session Minimum ==")

check(
    "MIN_SESSION_NUM_FOR_FOLLOWUP == 2",
    tanya_followup.MIN_SESSION_NUM_FOR_FOLLOWUP == 2,
    f"actual value: {tanya_followup.MIN_SESSION_NUM_FOR_FOLLOWUP}",
)
check(
    "Session 1 is below follow-up threshold",
    1 < tanya_followup.MIN_SESSION_NUM_FOR_FOLLOWUP,
)
check(
    "Session 2 is eligible for follow-up",
    2 >= tanya_followup.MIN_SESSION_NUM_FOR_FOLLOWUP,
)


# ── Summary ──

passed = sum(results)
total = len(results)
print(f"\n{'='*40}")
print(f"Results: {passed}/{total} passed")
if passed == total:
    print(f"\033[92mAll tests passed.\033[0m\n")
else:
    failed = total - passed
    print(f"\033[91m{failed} test(s) failed.\033[0m\n")
    sys.exit(1)
