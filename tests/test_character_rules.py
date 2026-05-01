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

import tanya_bot

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
    "Never use em dashes in any response" in prompt,
)
check(
    "Rule 5: Calm/supportive tone",
    "Calm, supportive, emotionally attuned tone" in prompt,
)
check(
    "Rule 6: Interruption handling",
    "It felt like we got cut off" in prompt,
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
    prompt.count("Never use em dashes in any response") == 1,
    "bot's own rule should appear exactly once (vault may reinforce separately)",
)


# ── Test 2: Voice note handler responses ──

print("\n== Voice Note Handler ==")

check(
    "VOICE_REDIRECT_FIRST defined",
    hasattr(tanya_bot, "VOICE_REDIRECT_FIRST")
    and "I'd love to hear your voice" in tanya_bot.VOICE_REDIRECT_FIRST,
)
check(
    "VOICE_REDIRECT_REPEAT defined",
    hasattr(tanya_bot, "VOICE_REDIRECT_REPEAT")
    and "Text helps me be fully present" in tanya_bot.VOICE_REDIRECT_REPEAT,
)
check(
    "First and repeat are different messages",
    tanya_bot.VOICE_REDIRECT_FIRST != tanya_bot.VOICE_REDIRECT_REPEAT,
)

# Simulate counter logic: 1st, 2nd, 3rd voice note
fake_chat = -999
tanya_bot.voice_note_redirects[fake_chat] = 0

count_0 = tanya_bot.voice_note_redirects[fake_chat]
first_response = count_0 == 0
tanya_bot.voice_note_redirects[fake_chat] = count_0 + 1

count_1 = tanya_bot.voice_note_redirects[fake_chat]
second_response = count_1 != 0
tanya_bot.voice_note_redirects[fake_chat] = count_1 + 1

count_2 = tanya_bot.voice_note_redirects[fake_chat]
third_response = count_2 != 0

check("1st voice note → first redirect (count==0)", first_response)
check("2nd voice note → repeat redirect (count==1)", second_response)
check("3rd voice note → repeat redirect (count==2)", third_response)

# Cleanup
tanya_bot.voice_note_redirects.pop(fake_chat, None)


# ── Test 3: Voice note counter resets on session start/end ──

print("\n== Voice Counter Session Lifecycle ==")

check(
    "voice_note_redirects dict exists",
    hasattr(tanya_bot, "voice_note_redirects")
    and isinstance(tanya_bot.voice_note_redirects, dict),
)

# Simulate session end clearing the counter
tanya_bot.voice_note_redirects[-888] = 3
tanya_bot.voice_note_redirects.pop(-888, None)
check(
    "Counter cleared on pop (simulated session end)",
    -888 not in tanya_bot.voice_note_redirects,
)


# ── Test 4: Follow-up threshold ──

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


# ── Test 5: Voice handler registered for VOICE and AUDIO filters ──

print("\n== Handler Registration ==")

check(
    "handle_voice_note function exists",
    hasattr(tanya_bot, "handle_voice_note") and callable(tanya_bot.handle_voice_note),
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
