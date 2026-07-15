import os
import io
import re
import csv
import hmac
import math
import difflib
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
import hashlib
import asyncio
import logging
import datetime
import json
import shutil
import threading
import subprocess
from datetime import timezone
from pathlib import Path
from urllib.parse import quote
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import anthropic
import httpx

import tanya_followup
import billing_db

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
VAULT_PATH = os.getenv(
    "VAULT_PATH",
    str(Path(__file__).resolve().parent.parent / "tanya_brain"),
)
GITHUB_PAT = os.getenv("GITHUB_PAT", "").strip()
GITHUB_VAULT_REPO = os.getenv("GITHUB_VAULT_REPO", "https://github.com/cole-projects/tanya-brain.git")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_HAIKU_MODEL = os.getenv("CLAUDE_HAIKU_MODEL", "claude-haiku-4-5-20251001")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JZWiJzVBxv3K7zOcItDy")
STRIPE_PAYMENT_LINK = os.getenv("STRIPE_PAYMENT_LINK", "").strip()
# Optional harmless URL shown when STRIPE_PAYMENT_LINK is unset (e.g. Stripe docs home); not a live checkout.
STRIPE_PAYMENT_LINK_PLACEHOLDER = os.getenv("STRIPE_PAYMENT_LINK_PLACEHOLDER", "https://stripe.com").strip()
STRIPE_TOPUP_LINK = os.getenv("STRIPE_TOPUP_LINK", "").strip()
STRIPE_PORTAL_LINK = os.getenv("STRIPE_PORTAL_LINK", "").strip()
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_TOPUP_PRICE_ID = os.getenv("STRIPE_TOPUP_PRICE_ID", "").strip()
STRIPE_SUBSCRIPTION_PRICE_ID = os.getenv("STRIPE_SUBSCRIPTION_PRICE_ID", "").strip()
# Blooio iMessage API
BLOOIO_API_KEY = os.getenv("BLOOIO_API_KEY", "").strip()
BLOOIO_WEBHOOK_SECRET = os.getenv("BLOOIO_WEBHOOK_SECRET", "").strip()
BLOOIO_PHONE_NUMBER = os.getenv("BLOOIO_PHONE_NUMBER", "+13177282783").strip()
ADMIN_PHONE_NUMBERS = os.getenv("ADMIN_PHONE_NUMBERS", "").strip()
ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()
REPORT_ERROR_KEY = os.getenv("REPORT_ERROR_KEY", "").strip() or ADMIN_KEY
GITHUB_ISSUES_PAT = os.getenv("GITHUB_ISSUES_PAT", "").strip()
BLOOIO_BASE_URL = "https://backend.blooio.com/v2/api"
TANYA_PUBLIC_URL = os.getenv("TANYA_PUBLIC_URL", "https://worker-production-32fb.up.railway.app").rstrip("/")


def normalize_phone(phone: str) -> str:
    """Canonical E.164 (+1XXXXXXXXXX for US) — matches Blooio sender format."""
    s = re.sub(r"\s+", "", phone.strip())
    digits = re.sub(r"\D", "", s)
    if not digits:
        return s
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"


def _phones_from_env(*keys: str) -> frozenset[str]:
    raw = ""
    for key in keys:
        raw = os.getenv(key, "")
        if raw:
            break
    return frozenset(normalize_phone(x) for x in raw.split(",") if x.strip())


# After free trial, block coaching unless paid / MESH / bypass (set 0 for local dev if needed).
BLOCK_AFTER_FREE_TRIAL = os.getenv("BLOCK_AFTER_FREE_TRIAL", "1").lower() in ("1", "true", "yes")
_POST_TRIAL_BYPASS_PHONES: frozenset[str] = _phones_from_env("POST_TRIAL_BYPASS_PHONES", "POST_TRIAL_ALLOW_PHONES")
_MESH_PHONES: frozenset[str] = _phones_from_env("MESH_PHONES")
_ADMIN_PHONES: frozenset[str] = _phones_from_env("ADMIN_PHONE_NUMBERS")


def _is_bypass_phone(phone: str) -> bool:
    return normalize_phone(phone) in _POST_TRIAL_BYPASS_PHONES

# Optional: log approximate USD per API call (coaching messages). Set to 0/false to show tokens only.
# Prices are per 1M tokens — confirm against https://www.anthropic.com/pricing for your model.
LOG_SESSION_COST_USD = os.getenv("LOG_SESSION_COST_USD", "1").lower() in ("1", "true", "yes")
PRICE_INPUT_PER_MTOK = float(os.getenv("ANTHROPIC_PRICE_INPUT_PER_MTOK", "3"))
PRICE_OUTPUT_PER_MTOK = float(os.getenv("ANTHROPIC_PRICE_OUTPUT_PER_MTOK", "15"))
PRICE_CACHE_READ_PER_MTOK = float(os.getenv("ANTHROPIC_PRICE_CACHE_READ_PER_MTOK", "0.30"))
PRICE_CACHE_WRITE_PER_MTOK = float(os.getenv("ANTHROPIC_PRICE_CACHE_WRITE_PER_MTOK", "3.75"))

SESSION_TIMEOUT_MINUTES = 60
MIN_EXCHANGES_FOR_FOLLOWUP = 5
# Human-like pause before sending reply (anchor clock). Override via RESPONSE_DELAY_SECONDS in .env for quick tests (e.g. 3).
try:
    RESPONSE_DELAY_SECONDS = float(os.getenv("RESPONSE_DELAY_SECONDS", "0"))
except ValueError:
    RESPONSE_DELAY_SECONDS = 0.0

# First session free trial: cap user turns (each turn = one user message + one Tanya reply).
FREE_TRIAL_USER_MESSAGE_CAP = 25
# ~90% of 25 → warn on this user message number (after 22 completed turns).
FREE_TRIAL_90_PCT_USER_MESSAGE = 23
FREE_TRIAL_90_WARNING = (
    "Quick heads up, you have used most of your free back and forth with me. "
    "After a couple more messages from you, I will share how to keep going with TanyaTalk."
)
POST_FREE_TRIAL_BLOCK_MESSAGE = (
    "If you'd like to use TanyaTalk, it's $21 a month for 250 messages. "
    "Reply yes and I'll send you the link."
)
POST_TRIAL_RESET_DENIED_MESSAGE = (
    "That cannot start another free session. Your trial is complete. "
    "Subscribe through TanyaTalk when you are ready, and we continue from there. "
    "Reply yes if you would like me to send you the link to continue."
)

_TANYA_PHOTO_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQgJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAEsASwDASIAAhEBAxEB/8QAHAAAAQUBAQEAAAAAAAAAAAAABQQGAwIBBwAI/8QAQhAAAgEDAgMGAwQHBwQCAwAAAQIDAAQRBSESMUFRBhNhInEyFIEjoUKRB1IVsWLBMyRyQ9HhNRY2JTRTgiZjkvH/xAAZAQADAQEBAAAAAAAAAAAAAAACAAEDBAX/xAAmEQACAgICAgICAgMAAAAAAAAAAQIRIQMSMUFRIjITYQRxQoGx/9oADAMBAAIRAxEAPwBNF/U50oU56bUnjAEhxU42NIkg6VL51EpBNXAIHlSJoJBxWjODWc+tby5UieG5oho3+4JSAffS/R/9xQVUQdU3Ie9Njv5n/id1jnwGnNNyHvTY79kjupc458BowWfO0H4qlzUMRPqqTNYs0L5q/ZUWasGqFJNq8Capxr0IrwYdtIkobnXsiqK2D5dakWGST4FLDtHKkTwB542rVUMcZx7inBp3dXUb3hdYCkZHOTbNO6z7i28EQe5ZdtyXwBQ2FxObJZyyE8ADgc+HeryWFxGgPDzGcciK62NF8KHNjFCR+vjhFDru3uhE4llsXPZn+eKnILiqOWAO3EOE+1U4cHABzTnk+SuNTEcjfLsNmBOAfKih7tWN1D4sdxI7dqDhUfQ0YFDCbIPI1XJzii2q6Y+nTNE26H4SQd6ElTkYIOeVKZHgwsQdq3iqvWsqkL8ZrOKq1maSF+M17iqma0Ngg9lIl817iI61ksniSFuELnoKrmqJcMe01okYcnP51H716kScTSDk7fnW/NT/APsb86gr23bTYnY4vjPSp+tQR7SYzSgj1VSFl57VLnblUQ5ipN8c6RNx5V4Vo5ede2qie67Gl+jf7ilDyN6IaMf+5IPKkR0zch702O/v/Sd1/cNOebkPemx38/6Uuv7hrQBnzrF1q+ajjOxq2d6wZqWzW9DVM1udjUEhSMSyEFiKWnR7nAKklT1xSKM7tRzSNZaNPl5W9I+E0SVjYPXSbviC5IycU/e53dhuMXV1xEL/AE1bkPPH86i0OZdRvVVQGC7sSvIU84mkJW3iHCTt/dHbWc3TpGkFeRY8y24EduoaQnGcZ38h20pis2gXxrsma5O4Vm2Uedbawpax+OVzJ8MYP76Q3N5NerILVeKNfimOwJ8v86CzShJrOtRQKfFl8RhyVThB7CmJda9f3NwTb2ayQg78akDHvypz/sSa/uGaZiy/qpn1Uqm7uWtmglvcu4+CGPktCmE1gYOp2Ui2XzixrHKJATg8WKM6F3nvzwR3MYmRRjiKcJP1FENXSNLFOGAxJxjCgZz71Me7yeFHPbelJR6k5cDeQrS8GdZCV/pVrr+mGaFBnsxuprlur6dLplz4UgwQTg4xXU9GeawuPCkOQw4T2Gh/fTSYru2M8aguoyM9fKgjIKcTlbL6vcA1VmC/Ea2UMsrBxg53FJ5t8CtznJvET9YV7KnqK9b6e93nwQSR0FSHRrpf8J/yqkKDHbW4FVbTblfwOPpUZs7lejflSJNw17FQeBcDoawpcDoaRJ69SfM46V7xZRzX7qRFI51bApJ40g/DVvmH/VpE7Why9KScHFJo/jG9Kcg0RCwJ9qvmqCrZ2pIXxsa8MedVFb9aRNORS7R/9yj3pCMUs0n/AHOOqhHZNyHvTY7+f9K3P9w05puQ96bHfz/pW6/uGjBZ85x8jVs1VDsa9msDUtW52NUzWk5BpEoh+KpbWF5pCEGT5VCnWj3dXT/nNUXjx4aHiIPXFRukVK2PXQbL9kacoYKs8q+JK2c8C9Kc+jnjiMzgjxDgZO/D/rTeum8W7W0jbd/W+/SnJCEtbVAfibZEHU/5CuZt9nSliiW7eXVL1LKIlIFGZmHZ0Ue9Ep4E8NLWFAANsDYCo9PtjDAAqku25Pn20RUQWUZmuJAG7OyjSwRvNI9bWa2sRC48Qj1N2UgubJrtsRnHn2f60tWd7tvSCkfQHmaXxRqigACqknhDTWWNPU+7hntGAYlwNjQPQ9UltL4aRqbNxHaN35P5ZrpjKpUimR3u0eO4VLmNQHjYNmgmuOUawqfxYv1C0Cw8aLy8txQQXEd/CYXIycgr2GiVnrcSQxQ3rABxhJD1PYaSX2kH5v5q0cBjzTOzf60Np5QFVhnJNbs3tdRmidSrKxxnqP8A+UGlOXFdE796aWij1BVwSOFx1BFc7kx4orpi7RzSWRRb3ktlMksLYI++nAneUsATjPtTWc5xUg5CtFJozasdS94Yzz4T7ipP25at+CM+4ppV7NFzZOI7l1Ozc/0YzWtc6e3+GnnTQB86kUMUZgdhV5/oaHKTprn+mBUZt9PY7DFNvjbtNW8Rv1jU5L0NMN3EGnQRF3ffoO2gLyhmJUYHQVHM5Zt2JrOVC3fQSR21dnUUpHnSYHDDG9KCdqoJbHQVcZA57VQHBFXBpE3arDNVzW57aons/nS/SP8AcI80POCedLtIP/cY6RHZKdh701+/p/8Aqt1/dNOeXkPemv39P/1W5x+qaMFnzmvI1tVBr2awNS1eztVa3O1ImJk5xTv7qMtta3Ny4GVUAU0UYDpT07t2TXemLEXCJJLuxO3tQbPqHD7Dl7tW3zNy17O4VDkksQDk8hTriktfGLqTPJy4sZCjsFJFg0vTbYCR4FCjdjUc2pxNb8Vp8HMyYwoFc0pUdMYuTDTagIl3/L/IVDbJJfTeNMCB0XspqfPaXLKXHeAJcDoHyP3Uc0zULqPHjTRXER+GWLr70PJv7GsYKP17HTFEEXApUvLnSSGQyw8QqGW+S2UtK4ArdNIyabCDZHWht9D48LIwyCKTp3j0+R+DxWJ8lNK/n7OYADiGdgelRtSQpOIyNZsDNpUtuBl4229ulA7PvBfWdsFP2oQcjzx2U5tfvF0vUUll/wDFm+zk/h7DQWfS/tPn7BhKgOXjG9c+UzV0yXULsazpV9AygyRjjx9NxXJpRiThOMqSK6Tp0qR6tcrGSYZ1ZgDzXtH51zu9RUvpgrZUMcV1anZx7FQnY+oVIDUR+Kr1sZFs1tVzXs1SFqlXhFu/q9WRgdtQ17pSJua9nNYK2kSJ93rGO/KvH46xudQp20H1LgdaVEjG4pJuCDyNKCTsa0AJRVxsKiB6HepBkLikS2+M1oO1UzW5zSJjsF9VKNEug+spEAcDrSSRwqEybKKzu5FDNqxnQkgv6d6F3eBH7M2OH3pt9+UaXuxcIgyzKQKccx9I96aXf68jTu1cIr/aYIwp3FatgHENT0yPT7aIB+OY7vw8hQoKeHi4SV7cUVsjcXVt4TSqYywBXGWNHLrSv2dozXXCotiwygbO9ct1g1AttaW07CRUcKFyR2Gk+rW8MM6tbnMbrn2NS3N6trcrcWM/EHX1IRy8qGSStK7O3MnNVJ2J6FDJIqfrMBXWtL01YI4CyeiMYjTGAD2gdT5mmP3PgVr15TaeO6Y4MjIB9u2upR284tXnuTwOVIRR+H386z2ZwbQxkbWpRNq+tW9pGSYUlGcciepp8N3bsrrT/lpYeJSuCM4B8tqBd27VPTMd2VgCT20/7Xgx03rGEbyzpcqwhjHuTZRArHDJHheEcPLFKbfu9HZOTBmNDj0DlmnnPJbx/Gyj3pFK0cm0ZBz2CtZLFWROspElkng6fxN99ANWDo7TC1MwAzwinJd4S1SMDc4qIQpKMMMEdaXG8CnTs57JrWpadiWXSrcRsvEEUZYe/ZRrRO8trrTyWxgNvNGAXicb49qccmlSPsroV7GXf8xVI9Gtbc+K8MXifrKMH7qWsdEvN2NPvpY+PoFxgZdF4h5Uxu7V7e2XglZGaJ8qPIjp/pXRO90iW+g35jG3hEc+2mP3Wjjm0+SN1JZcSrvy6HFB2P8AYuvkikEl7a/ZzKCXQcjkcx2Vza9PFdyNjGTnaus3FoUYyR4YcOHAHPz+tcs1SSKW6ciNVcMRlNgRnqOho9Xky2iD8VXqMfFV63Oc2tqtbVE3NePIVlaTypE8K9mvVh5GkSP8VYedaOdZSU7aTuM0ox6RvSZmqcPxRgjajALggirKcjnUYY1ZTVEk9t6wtiq8e3nUUsgVQTtk4oW6VlStkd3M8Ubv6TGFJI6mpO6Uz/OW4MDKrAnJoJr86LEqMkm5GClGtF1CGKSCYghFGMdazTTdk8D/ALg5THaaY3f3SQNGkuYZGUgHKfr+9Om71KNNNN7wtwAZxjemxreure6Sq+GFJBYh61nKNAHO+7SNb2U8DwkSSEHi4M7dmaQ957hoD8qsbRodypbb8qc9m9wbAkOsQkyFJGMVzrUTJ8/Mss5mZWI485zXNH5OzRZEuaV6bbx3eoQQTOUjdwrMOYFJKW6TKYdVtXD8GJB6uzetWEdd0W00/SojHZ23h9sknNqn1HUPEhZY3yB1oZOxRiBOvqO5ALMaG6pqcdlAYwczNyUfzrlcrwdMY+QzZ3EsFvaspws1yOLPZyFO8akLeDiJ5Cg+iWcbpFbSpxmKIM2ejYogLdLlGjPXehdpYNYNN0yGO6utSmLPiKH8I6n3o3Y3MTSBJDhkGMGmXrz6toYS5tGja2X41MZYqO3aiqi+vLSK5jhjuIpFDB7dyrgHyNWNrJrJJ4HVeSRELlwD70NudQW0kTwmLk/EAcjFNuSK0mmVr2e6ZRt4bo2BRNb3R4Lfw47iBQNgCeH99Xk2T8fH9jntr1Jog4III51HdTKVOKa9jqAglMYkDRsfSw/dSq4vS3pU7mlbLQEtdOxq9+L3+x/JqTxS7nHYKb/dVzFMzD/COCD2EUh7zd5pX1q7gigikWNuBHOcjHP76n7nz5klaYhgw+0HlS4ySsnKLwh9zIrWTXMI2VCQB5DlXELlzJcyseZY11+Rbm0tL1I9/S3Ac7EGuOyqyyOGBBBIOe2tNZzbCi86tVFq1bmJtbVa2kT1aedYOdb1qCerD8Jraxj6aolB1rwG1YORqy8qSnZnbt51IjDw96Su1SDDoozgijfQKq8ioEUmu7rwlCLkOeuKkMowFxuOtVLqeyhzJYFqmRwX63EjBSOFdjvyNWuG4uAEZA9RoHeRi01CW54gsDLmQA9e2o4dR9fjrKQj7KGO1YuTXxELu9vqewnw8XNcUKe5MWr29nEGCcQeRsZwKUftS20+xlnQRkzbsev0pDLdXUdul8FiHFvnrw+dDYjxm1ma606axt3JdwQHkGOEeVNfU0uItBNp+0BIttglgmN+zNDbDXxNcyKYmaRmAjK/fTh1WG3te691MZENx13yDmijyl2D0cwutRu5x4b3LtGOS52FIq9nJJrw3rSqNDQCeQqe1hlklHhKSw3BFba2slzKkcaMxLYwOtdM0nukbW3EzxskQUHhxlmPnQylXQUVbA2r395bWsMcTsjsgLtncbfdQjQo2vNdgEpLrxcTlj0G9EdX+ZvLyWOJGIU4K45DzrdJthaGYmMtO6+FGewkb1zKVHW4hifvtNol+GggSZbhWLA9N9iKN90NfbUrVhNgXMLepe1TyrnuooJdUKKMiPCg+1et9Qn0fUkvIDuuzL0YdRVbtUKVOzulxCl1ahWGQdsjmKRw2ctouESTAHpkt2wfqpqXRb2PUNNSROTKGwedHIowUzgYrSKsLnxVMBJPc9Zr5gDkAwqPvxQ2+7vprk4fUI28FccKM+c+ZAp3SxJjOKSSBIIzIfoKrTFTXhAlNJsbCALFEsaINgBQPX9QTSNJnu2P2nDhF8+lF7m6BOXOd8hR1rn/AH/mka1tw52ZycewrnpciturYxEVri69W5YlmOeZp2aLbSWg8R9kxwjHLBoR3csvmtQkBGeCIt9eldFtdOjl0aNCNwuM0WyWaRnrSStnopHu9KJXeVVMe/UiuYa7aPFeMxUq7AMynoa6hZRfL2LI5IIfiB8qGd4O78eu2vzVk0bXSLnhVv6g8vOi1yoznG+jlQq1STQtFI6sMMpwwPQ1HXUcx6tzWV6kTRzr1eHOvUierH5VtVflSJXpVxyqnSrjlUKdbfjPwoT7VNEvpXxAynzHOh73kkUmUYrU66td8AZpF8P+Jc5NaOwVRtyXhlUgbdfaqxXAmLY6HGaHSa493rMVmIg4YephS8wxWaZQO5JyR0WslKm6CasG6k6PI8PCWYr99A/lJ7bFvdPjj/ognrTmaFnuUjhUGRjmh+pWDXGsBr6PiSNccKnes2QSpoF1d2ximuIsoeQPSq3Ultp81ulxI0luDgrmp1itxeR2ltAyTrvxSORxCoJobO9knS5mWOS3JwvQ1P7IOLTZdNh1KC+trYSW49LELyzU3etoprG+YWTwrweksuAfOkOk30sGj2llFZrGsfr8duZweyiveHW21Pu3Lb+ATIVxxjYGji49Ngs411pVZwCW4RChcscKi82PQUat+7CSWiyS3SpJglkBG1Ob9GHdd5u8LX92n2dqvFED+JjsDRJqTpB2Pfuf3Ii0q3S4vI0NywzwAZEflntp5G1R1OFG23tSVtTPzHgwIpUHDSHqfKiJkS2hXiIUYySeVaR4tUiyUl2BX0W0EzN4KDPPbnTYv9Djl1ItbBcICTw7YJ7aK6n3v08u8EDMzHYsBjFNvUu9UUFsyRRrCo2/ib361z7OB0a4z7GddxLayuWPFK7GhN5HxRyEcwAPz3oit1JqV2cgLEu5JHTsqjRh0YY3Zs1zXR1JWPXutqMlvo9tIu+BwkU8YdchKZbiQ9lc67rSBbKW2J9Ub5HtTqSPOCBkdlMZNPBZRXkcD61blcmYnyxQXUNWmupPDhQqnaaxwWOFj+6qrEMkkDNE5tgpI9DCSQzbntpl/pGHCLXsBNdAiQBPamP37gNzb5/UYfWpHDRHmwf3IgXxLqZvhKhacmn6hw6cHcDYkHHZmm33Vm8BZYSd2Gd+mKXW0vDA1ux+Icanlv1FDJ9kig8shk+yBDRSnhPsaT6l3bjt7NpLAyQyKeIAOcGotFLO0MDZLLJ9w3p8LZveZihALEdeQ8zRQTkhlUTlen92n72aqbed3hlCktOI8kjzplapp8ml6pc2MrKzwSGMsvI4619M6LoMWkkxqnitICTNjBBoR3r7jaf3jt38SNIr0LiO5UYOfPtFd0INRz2cOyScsdHzjXqLd4O7mqd2rv5fUbfhDfBKu6OO0Gg/EaQS4rKrxV7ipEtVX51bIxnIqhOTSU92VeqdRVqgnQ7iYcWDzpLJqSLaOA2HTfhqCSUl+dJL+BTCJWUgn76LY6RI+wel7Os6TRycDA7sOdGbDUryXjiX1MzfG1CYLN3i2MYLn08XP6VJLFNY5Ri6SsOp5eYrmvwHlZHLNaarp+qxXVuyOBHl8nakj3txqOrGaVEQAZGD2UJh1O7WF5bqZpI/g2bFJ2uvElRV2UfeKmQK9js1q0OqQ20sJEbpuWHM1Ktlp2llReqvFcj0SMM70HtdTtNNtRJLcvK4faOva/qq6pFCtvE0sjbgLvwVVYNNYYRkv7LhaV5iZo24FjO3H7Uj1PW7v5YwS26Roo5Z3ohBotgUilvl/tESg44tjS29/ZFjpDXMsPiBzlcji3q4QjT0+xaeFvCtHl4vUxLcq6j+j8kaFfzGPwzG3hL9B/rTG07UVt5Jb3g8O1dNkFP7uPwN3GknR2YTXDtlufMCrF5bLHLDNrFwOg8xV++1xJbd27h4iwfgIGK2PbhPnW97omudGeCLeR1yB5dauvEGdOzM0ct0TSLrVFNzNKyxEZULyx5VLNpVmWKJH4j4wXd88PsBtmq6fqL6Zo11p5k4XLYXoQDzFLNOj4oWO+w22rjk14OxJ+egRcWsNlAY4wBtliOZoWFaNISc8T8Tfmadt5pErqMITkcRP+VN/UYuCaMYxjAFCn7NMPotYXAstSjfOI51x9aflsVliV0bmOVc7mAazQFc4bZh0px6PdzfKK6nJAww8xTdFcbQ6goI3OBVduMAbChP7VmyFMZzSuB3c+I59qLkjJwaCEkgRNudNPvOhlgWMfExpxqWlf2puaq3jat4Kn+kpZiO2q2SKobSMNOm4jkqycJx1NHL2yItIGU4YDY/TIoY9p81qcNuBksyry5DrTt1S2X9lSiIZa3IU+1RrA3UiHQsoyzso4ZFGG6r5Gui6U4t4m44W8QgHbfI8qa/djTHAthgFE3csMj2p5RgWzcGBwn4fIV16I0rZyb5W6QshuEnLKrbr8QIwRVyAxwRtSO4xBPFOuyk8D+x5UvG4zXWco3u8WjWmqafJZX8Qktn2zjeM9GFcC1XuddabqE9q0EreG2A6jIYdDX01KgkiORnA3HaKaOtQLBiRiOFNsnqp5flyoZOlYrs+f5NDmTmko90NQNpcinnj3FdyRrWTmYz9KuLOxlzmKFvoKw/KvQfF+zgx06XoVqhspl/D+Vd1Oi6RKSHtoc+wpPN3T0STOLdB7Gr+WPoeLOGOjI+GGDXq6redz9IncqVdeHkVakB7g6axytxKB70D3QD/HIDWksPzY+bDLH0HnVdRushm41MWeBU7Km1pfCmimVOIY5LQZVgfiM5Yq24VOYplnsitOhXBdJGwd1VjGuFxQ671I3tyZHLKRsOu1RtepDxxxglW24jzxVoY7eVJH4SNvRv1qVWWD+ikkihMRlmTqGFeLSOqNwgKoxnlUT3ReHwzw5HUDelbWxS3jnYkwMRyNE8DVhDTtQsLWIi4ijnf8ORyrdP1rwL2ZoLZW4+i/hoVPHDcSBoUMagcu2oYnubCfjhyC3pGRzpUUVv/Ecb6pBNdE3k0hTGNhyPZVJdYRoXtHmb5Pkisu+KEJY6gLhndcSAcZDdangS31KL7U+FOjb4+HFTigKEsk9xwmAy/ZjdRnpXeO5lt4H6ObMZByOM/U1w27t4rV5U8UP6QQR1rvHc1hN+jmzK8vBz99aR6ZV2hbGdhRkRrIY2bcGPh3oFGScUetDx2QJ/CTQ6fKNt3g5N3o0n5bWGVUGOPIHbS7u/Zh1LSJjLZo7rdmdW1y2RRwlDlj7c6TRqLKeRcbqTgVyShUr8HUp3BLyKNQkS2tQf8ST0p5DqaYV/A93qCpGDleRp4rBLql03GTwA8I9hSy+0u00nTXkyjyhuLjPxGpKLlnwgoSUHXlnP9RsflrdAu7gZOKLaBCwi4ihCuM4xyNVt7eXU78yvGRGdlB7KddrAiKq8AC4wduXnWKydUnSBb2zCRSAOHrnbFLI4XCDbc8qLS2gSP1LjPU1JFb+EodsYI9JByDWi1uzF7FQOaJoLctjDYznspmWn2+qXGDkkEDzp665drbabKQfWwwKZmjwOWacc85FWXdIV9W2K9HsyO8QdlwqIW360cuS0EqPHggsfEU/iBqAFEvY5BgcY4CezqK3VuKPhnX4QATj33ol0YPMhy6FFK8DcEvBHniUjnjsIohFObsRRqxd0kJZwNsChejkz2nBxcMe2QPxGnDZRpGHjUAYfGAOQru15ijjniTJ5YhLbujDZhiq2M5ltlB+JfS3uKmcgDApJaKYLq6DfCSHHuRWxkLyQo/fTR70KJdGn2zmJsH7xR7Urw22nyy/iK8K+5pq94Lj5TufLK/4LfO/tQt+C15Odwu3Du5B96mN3PDukrD602Yu8caj1RGpP+SWz7FGFcPFnRaHELm4I4/EfJ86k+fulH9Z/zpvp3itVAzn2qT9u2j82IoXFhKSDCajOvEOLOe2o01C6QYBBoWmo280vCkgzXmldWIzmgkqDi7LapCZF4GcIMbmgunypDcyrDFx4GFJpx3WLyLYY2xTaVm0/Ull4fSD6geyumSTsxSp2IZ9Ku3nIjhJDb56CpLeIWxZJt8Dl2U4tRHzaBrKYcLjPCOhpvmKe2SYXMbFzsD5U22Sca6E72YZfGTAjzuCaWSzRfKJFDGvCu5PnSG0DzuIB15DtqdspcCKLpzVupqtO6YN10K7YKkRaRiDzB4djSG/uzPMpReBY+vUmipiunsHVmiVcZ4OpoDJweJwbrg+rNMZWWWvj8l5HFaMLuWHw5XlkYYbHSvXtrHFJciMKEEeWXHWk2jXNzZPJLaQcYO2T2VLcajbRadeRNxG6nbGOwUpXKgUnFchvJbzz8TRxs4HPHQV9Dfo2YzdwLaM8xEw/ImvnmOSWPaN2UHY4Nd+/RDL4ndNEOTwvIm/vn+dbxBYVgO1F7CQi3lQ9N6FKvBcyJ2MRRGyws4VjswxWGrEqOjarjZHa2yx3DTMPUeRNCL61WbUJGQjcgfWj1+5CFIQS47OgpFaxq89sF3BBbPbRTjfxAjKsiSx0qe3kZTLw8RyNuhoi2kW7TRiZTcSNnAblj2opJEAnHjkMVlmMtJK3PZR7UUdUVgF7JPIDv9EhsY/mLdUSMfEmOXtSDjGfTinhdlPAcSjKMOE+eaD/ALAU258M+HOSeFOLK47Ky2aG3cTfXvVVIQm6hcvxhlaQbk7hahMuMqGBXOTw8s1660+5t5jGYi7YBzGCwpKkUssnAFYNkAjs965253TR0xUatMEa8xuMR4PCKR6ZGYoWQgj1EU7L3u/wRmR5wXUZwBtQh4AiSELjbNA4Si7ZfyxlGkI2IubJ0YYI2PQ+9WgvjcWXh3GPFj+zfz7D9RUY+znD9GG48qg4AbqWEfjTKeYp5A8R6aCFPAifCqgkU4YiY72QHk6hh+6m73RBfThMw9TY+6j10SLm1KAl+IjA7COtelq+qPP2fZi5Rk8R6Ujtys93cT5JjDBFHaRUzvxAjOI13J7aTxSLZab479hcD761MwR3iufGu47NDkR7vj9Y8h9BTW/Sjd/J91vllIBmdY8eQ50d02NrvVUeXdmYyufvrnv6WtRM+qWlmDsimQjzJrBO7kayVVE5vy6V4Y54q7qSBjG1UXJoSHl+LAFWkkZOYFVB4GrzsGbNImJJwvxA4byo9aXLvbKScmm8w2otaem3UE0OxYCj2HrO64Ywedbd2SXiFxjPZQmxuQjYbnR2O4TbHIism6wdKSaGyDPplxxx7qDutEJZ4NVQvxcMmPUpNLJore4duEZJG4PKhM2llZmMTcBHSi7BqseC9hpUa6kpEhjwMqTyzUGp6dcvdF0xJ0JG1RM95D8RJA7asusSogVowcVfldg1GqNFhflcKvDkYyWolYaBb8XiXcniPzIob+3Zl3WIfWk8uo3l0x9XAp58NVJk+Ib1LVYrHNvaBAcY9NNWZnaUscktuTSnweEcTHJxWxOqncUcUBNtiMMwPwn8q7x+huTPdltiOG4b+Vcjgmi4wCAPcV2f9F4U6PLwYwZGO30raKyYsNXaeHqcw/izUqPwsrdhqTVo+DUeMD40BqFT6a5vrNnWswQaZVZOJRzFIURTeLwbNGCuKV2bcdvjs2qKKD/uEr55AbV1VZyXQqmP2J251lsP7OMdTXptoz5Cr24/s0fmM1RIL0g+Ep6sKUgZx02pHetwz2x6ceKVFuFGY8gKpBPaPnxZN8lyCc4zXrtsPEWxws4DDbJ7N6js8i2Q8uJyfvquoZMtsR/7RUaEy/BNs4wMU1LlcDAPQinfeACzlY/qmmvcQnhQEcJxy8q5d0To1MDtEBIoO+2KT3EIiiW4zjwhzpe8bNLC3Qk5qtzCZ7SWPG2wrkUTq5Dr7uRpFpEGORXP570tt3eaac9OLhB8hQ3SHMFgYXyTB6T59lF7GNorNGlGJG9TAdp6V6cPqjz5ZZO8XFDwDYHYny60H1+5AtkhX/E6fwijWOMEMcIOf+VM7Urk3uoSSKPQDwRjyFDtlUQ9UbkL9JAgsbq8bmR4a/zrgPfTUDqHeu9l4shH8NfptXetXkXTtHaAED5eIyPnkXPIVwGbu5qE0jygxSM5LHD9TVjBqKQMppybAnE3aa0SMORpe+gaghI+XJx2HNJ30y8j+K3kH0p4P0S0QcbDrXvEOasbWdecTj/41Qxup3BHuKlDZvGaVRai0UYQKNqRYNewajS8lTCdtMCcE+rpR2B+GMKRvTXTZh0IogLxlUb5FYyijeEqHEvh4I5HFVlZo+EcIbiFCUvkkIByPrSkXBYg52FZKJtysmmt3aMM4AzyFIRZK5bbNKpLzjUDJxV4pYljxn1UStAumIDYIp3G1RzRpGh5DFKLm8CAgYNCZrgtuedVRbBbSKyOWOF3pVa2finDkIfOkltvMCR1o7EBP7LWnRIxUlkuujmQD7SM45b1179GFp8ro3ATuZH/AJVy2GBm3C7Cuq/o4c/sVSwAPjuB91aa3ky2JJDm1lPTA/ZlTSCPlRjVk4rInHwsDQeBS7BV5nYUGxfIPW/iE9OYFcDmV39wamReHUSf10/dUdpEI+KQcuQpQ6gvDKdihx9DXQujnfZFenhhYdu350oReFFHYMUlvDmaFR1cUsqkB+obyW4A/wAQVNcnhs5D5Gor0/2q2H/5P5Va+2syB1IH30ibEvDFCvYBVJxxXVuvYxP3VKnxqOwVXHFfp/ChNIlb8f2CTHtSOewWRB1YrjJ6UvvRm0I7WH76kKjb2oXFPsqdDUubN1njVFJOMD/OtS1eC4VTHuyk79aNXqcNxaYHN+E/lXtRhSZBADwysfSeorNaldhubqi1vAJ1AGQisCSPxeVLRIHnaNRsgwTVIlEEKRJzAAq0UeOIA7scse2tUZsR6zdC3sGVNml9C+3U0D0mBJL0O4+yt18V/pyH51fVboXd65U/ZR+hfYczU9lF4ViiEYa4bxX/ALg+EfWub77P0jp+mv8AbGr+ke9mt+7TRojPPeS+oAchzP8AIVx0T3EXOKVfzrpH6Q+8psddhs1j4xHFxN7mmtH3ttyMTWYP0rpOUBrqM0e/HKp9zV/2zcH/AB327aOHXtFn/rWg/wD1qVNS7uOD9hGM9q059j/oCxa/cpt4qEfxLUn/ACCVj6obZ/dKLwxd17hjxhRnsOKkXQ+7dztFMVPk9W5expAYazbv/U023PmNquNQ0kjLaWufI0Xfudpjg+HfOp9wahHcaM8tS2/u08pDxQ0Z7SWCU8z59tRFyDuMU/xGZ1AeKLOOykkumQSAh7ZM9oFc9o6uDGaJPOrrPIPxbU6G7vWcw2BjPkaqO6du3K5YfSmkDlDf+afFRm5cZ9VOgd0LQnBvXH0qWHuhZM+BclyOYp4jbGcZHkO2TS6y0qSZg8owOynhcaHbrwx2bQKF5hjvmqfs27RsKsbY6KwpFKwSukcQABC+eKIWti1omPCWQdc0paC+jXa1bfqN6iYXSeqZXQHtFQMzU7m3sbLjTBdxyB5Gn/8AozLr3bsy/wATySPXF9SuTeXwjTdVOPeu/wDc/TDp2m2Nrv8AZQZc/wATb1rrVGE5WOyZRPZyJ1Kmh1jZ5Xfrzb+QpReXAtLZ5DvthVH4ialtJVmgXHpYAZWipXYNuv0S+GoAA5dlRX2Ftxg4IYUowqqWJ9/KhrcUzCaQ7fgToPOiIbO4M9sc78X8qW8WDigjz/b2zDfEhFFuOkRJef8AmWp6cZ/dUl8fREv6zgVBdsxuLXH/ALMVNdEm6t1HaTSQnT4zUcRzfy+SAVImxNQQ5F7OfJaSkt5/4/8A8h++lCrSS8b+yjP64/fSsfDmkRJdDEsA/jpE+Tqryt8MceB7k0QmHFLGefDk0j8IrKiNu0jl2x0A6VBFkSHhGfiPOoNVuvkrFuE4kf0L9etLFVj7U2NYufmdSZFJKQ+kDz60GyXGJprjykJ7S3NzcJF+En1Hy60VjIkaS4OyscKP1UGwqG0jMNg8mPXL6V9qluExa+CuxcY9hU0xqN+y7pXKvRxjXb1dR7wXs/AjL4hAzvkDYUORba6JQwKrDsHSifefSRpmuTwb+HJ61YDoaHhTAFRMAldyetbUYElro+nzq8bFUcHmetRjQrL1jO+9RwSrAzSS+p1PpAO1LGuGeDBVQeL1HsFNIQPLpsUMbOFJJO1IvlDHhiGXPI06TcRKI04VYVBxxy3LRSFeFR6cjlUcS2NwySL8M7g+TVYX18gwt1Lj3qt9bGK6kC4Kg9OtQgTEekbVm3QSOmMmDjwSo7SK0QiQHhwDjYVH8/doSTJx7dRVV1meOMeLFG2Tt6cVlaOypET28iscAe1e+XbhyPSaUrqyuhZ7NCPI1o1HTnT128qeYNUmfKELqQAoJLHb60pkH7N08gn+0Sc/IUvtbS0KC+HiED4Vekk0JupXklXOTseymiWm6ADW5bJJ3O+c1BhomyjOPMGnDLZIFHDueVRfIL8OVyTQ8Q+SEEdxdooImkx70m1HvC0OnzQeOZJ2PCFI+EdtLdVnisrRjkAqNvM0xCxnleR2O++aNIxnLwLdGha41mygAJeWdB99fT1iBDAzHbfnXzx3Eh8bvppo3YCTi38ga7rq138rZLCGw8m351qnSbMKt0iK4u2vbwvk+Gm0Y/nS60uDEVycEH0k8vY+VBrZlVBmlkUjzMEjXiY9AM1yRm3LkdktaUeI5J1aW1doviK7LQvVnuIdGla1A8ZUx7dppdpkN5EnDMF8Ij0gn1L/AKVsunmUSRNOyq4wCF3GfeuxO0cTwxoWV8EjtpLlvs0O7eZpzrMCAQdqD3XdC4S1aGC5WRMg+sYPt2VJF4tsgjmRkYfrVUSwoMSXMX8JJrSQ+oj+FM0itrjNxz5CpYJM6hOT0UCqIRXmaghP9rnPsKkVxk1Bbtm4nP8AEB91Ikl4c2pP8Q/fUrSlU+lRTjitH8t6TandrbaZ4vbjeoUUo5Y55k8qkgAaRpmIx8K+3Whdj4i6Y0jyccjoTkcl8hS62RjbR5P4RSIoubtILWSQfgUmmjY27XMyqc8TniY9naacGpQs+nzKu+2fvpLDENOsmdwPGYcuzsFYzi5SS8G0JKMW/JLgSThF2iiGKhdjJPnGQN61CYrbDH1n4j59aQlGvUlVXZA4K8SnpW6OdnNO+mqG61xlXgIiHAvDzNNtpWbJkXhHDgZPM0v1PTrjTtTlilDPIjEBj++k7orunEhJKcvPtqiVVrUW5OMybYGakgkiDKZB6QcnflXreCAylSg487DNXmt4izxjAw2+Tikhf5dJWUxvlSCdulU8CMzZLY3A5c6mspOH7BQC/Cd17KjvUdrmIxARrtz7aSgS9GbqYEnyFIeKT8PKi+pRvHfeG3DkrzWhiAY3PWsmshofpHiR7YqjIzKMjPlS+O9bkbeJvupR83A4HiWa/wDxNY0jscmvAHHhxqQ6nPOthtWuJAipsd38hRhY7C6IRIZA7bZHSskFvp8bQeKI3Yc2Gc0SiA51jya90jBYkAEcYxgVXYwsx7eQpNE8QGEuITnmWpUQjr9m8TL5PSDhCQyKFJpMZS7FhgAedLZISmB4fmMHOapZ2nzM/EycCg7jlmmgm0snP9d1A3d2YwfRGce5oUCcYwceVP3XbC2k1Dgj08bfE4U70POjWByTCVHUcRFXoypvIb/RDo8t3rs+pMD4NqnCMjmzf6U+O8kzHV4FOdifuFV/RlaRadoVzIqt4T3GR6skkCnPLp1jPeC6nTjcZ4FO+PpWjjyjQEXxlYN0vTpLtBLLmOHt6t7U6rK0it0zGiqOmOvvQ5HlllHhqGVDuqneiEd3GGw5EZ7HGDUhrUFgs9jk8iuaRIYjJJIEUUHm1tw5EKgr+s3Whl9fyXly5LDw1OFAO2KT8VYT3O6RvDSqthaz1Qxjhm3BJORSnUIre+sSGkwDurg4INN4nPI1LHPJCcq2Bn6VY7n5GWm+hNHaT2lykiTyFV5oTkEUpTUYINR4JXCNMMLxbZI6Uoku1umUtwowGNutDp7YXWoRkx8SRA5bG2TXTGSfRySi49h4zgDINZpyvNczgZWPIPFQwRSbLGx32CnenNaQ+DCke2w396IhotB4bL4hIbtFDNR0ie40yS0V45Aw4d9jiijSTxqXRBIp6DYihVxqNyz4Rljwdxw70EpJdhxi30SwWjQWIgEb4VOEZG52pRBbvHDGr8wuMULbVLqNDwks3T14/lQy51XUbgcEjtEv8DHf61m90UaLTJjgm1CC3u4rUuDLIcBR+H3pFeRyPdRhz6FBkPmelNl3aCRJRzVw2TzO9O27IZUcc8EfmKdWznY7dfCgZfO5iWOL43OAeztNK4LdYoUUNhlGBnrSaI8dwDjZRUl67l42RsBd62OdjL/SLpcZih1JZDGwPhuo5N2Vz5OBZ0Qy+oJsOe9GO+fe9tTvPkYgHtreTnn427fam2t3DIWcRESH4TnlSmi0Fza+DKy4JfAy2eVQt4LPJGT402fSTtgdlQJqai24A3rY5btqX51AiGNFy27MedFaIS29zFFF8JEu/EV6DsqzYn8Nmfbg4mB3NJYXRnaSU4GcsMUNXVBDJJwbhtgT0FRuuypC3VVSB0KSFmzghudBicMwPPNSz3QnmR2IYjm3bVLlledmTHCazfYaOmtECoAA36ikxj8MEcj0pexOOWAKvYQ/MTl33iQ7Z61glZ2SdKxRZRLpti9zOAGIyd+VNO9vHvbtpmTOTsOwUX17UBdSG2jceFH8WDjJoGhXLEK2O00b9GCt5ZrHIGVAXG2KqqDdl+ua0tlD2ViFQCuR7mkI23Sae6SFWYszYAzyFOnUGTSLFOBiZCMAHt7ar3d05IYmvpANx6SRyHbQ7VLv5y7LAjgXZaLpAdsxNWvTwlpFIx1WpDrDM32lrCwx2UPyOE5I9q8fSPSQD5UNsKkHNO74vpKvElmvgO2TGD17RT6sNQGr2S3Fs4MbDkmxHka5BcHOcDJp092/F0ywfw7h0YqZWXGaJN+QJRXg6PZcEbjZgw2JWiLupRjlmYA44hTFsu9EvBxlriSTHWDH0zS5O+0TsIri3aFidmk2X6kU/kj7H8UvRBHOBnfqam8bKigmp3TWUzXAXitZGyrowYDyyKjttbgmYANXnSbTpnpRjcbQ4AcmrFARzpNHOhXOaR6teyRwiO1BMr9R0HU1pBcnSMZvgrFM97Bbq7NIqqnxO3If5nyqKDUb+4uViije3t8ZLMBx+WRyGeznQezMepIjQNM8kfPgH9PoSBvxFuW/KjVrYtPOEIdG4eHhztGvXHaT213rjDCOFuU8sUnVb+KXgtLcXDqcGdx8P0HOj3dqe7vLOaa8nLuXICkAcIHlSNlisouCNCSo5KOQ7SagmuprdnktUMkikgNkqhGNiOvOgWx9yDetJUh1CKXA/tDAeSik17aC4RpA32ijoMZpFp2sGS3T5p08fH2iqMD3FFGuIowC7oud92ArTEkZq4sbTMKTSsm/Ig1JrbrbyiVQVjmyVFN6bUdzvXnz+MqPR1/JWKr141hZuQXenTcIzWNrKp9LhWz9K5zc3j3LfLopYnniui2jv/xiwEwzIFVTXR/HTyzn/ktYRHaQ+HAXfZm39hUL8U9sSw+IMB59lKLgn5dwDgY/KoppkggGRnbAA611nEfNuq20lnqt1bzR8DpKwIzjrSQN2cQ+tdT/AEhd3IbiKDUsrHck8EmPxDpTAfQnAHBMrfSsXhmqTaBwYgZ42H0rwnkUgiQ8qWNpNwo9LIR/eqn7NvFBIiJHkall4ka3s4z9oNxg+Yr0bAROcAkjbIzio2RwMMrD3Wq+oDAYDypuyUSqiEdDW+Gn6tQhfJT9a3hb9U/nVIdajiluJlhjHCW6nspTqt2mlWIt4ceMwx5+9K7Oa3t7D5p5VywyOLr5U1b24e8u5JZM56eQ7KFKkbylzdeEDeGVX4yMk71OobPHyx2irFfWOEnBH5VIjOuQQpHYahSA/aSF2HXlyzUlpYtqGoCNRhSfVjkBW8AMgVm4Q/PhGcU69OtLfSNPedz6iMlj91ElYMnQj1m+SytEs4GwcYI7BQLKMQ/HzHU1W5c3lzLcO3xHl2CoihXADAClsUqR53CvuRUYlwSDkDO2OtSOkecsG8sVCFdpAsYLOThVUZOfKoUW6PbtdagC6ngT1EmnbY2017ezIqcMYUJxtsOe+Kl7saGLOx8W+BDv6irHl70eQtJMFhULEvXH7qFyxQSg7tka6ZHbAKXDfSrNaowx4St9KXJHvsue0mrlQBjGPasmjRNjY1Du806EW32LHqjY+4UMsf0fPHIZbrUbhznIVMKB92afawkHONqnEYxvU42FzaG89rJaQcGfECjA4+f5027u7x8y8isvDFwY/hLDiIx5U+byIcDZBxjauf67G9uXGSCw50wqE02M09kGl2LbQW9jDEbW4HDcSl04s5aMbAe4p0WtzHaaX8yW4pZtwO3srk83eO4juIxdzFXjRk3GeMHkwP03rpGgst/LGZAQsEa4UnqRXQjl/wChuyilmVnZ5VjlUcUbNzP+VK5IVEeAu3ZUqkZx0rGJBxzFA8miwNnVbfhidhnhI6dKP2P29hbmBlMhRdnPMdvnUVxAsisjDKtypv2wv9MunszdFYMloC6Blx1XtFTW+Mh2LlEn72CaL5ZZ5g4JbhOMY8qb0FtLeTCK1heWQ8zjYDtzTtknuLiMC6S0n4fhBTi/fVk1d7WeKBbOPDg/AeEADyo3qUp8mDHc4w4oGWHdO5hIBuB4jn1sEyR9TTtvYVt7KCBTkKQB9BSCTV5YYXk4EUgbDc5PSqtPLMoedsvjfsFdCSXRzNt9lJ3MjxwA/Ect7CkuozeFE0oG6ISvkakt28aSS46H0p7UM1q4UwSLnmQg88c6rIhlXOsWcl3JHqCSyEHck5rUfu3IC3E6EnsO1N/UlI1OcH9fnVAnCATup54rnbOhIcZ03Q5X4o9RUZ5A9Ksvd61b1QajC2eWWFNxQA265Gds1fwwAHKkI3IipYVP2OL/AIxdYPD4Lg9c0ll7sXAzxWqOO0AU59E+07toV4uLgIHbTbjur9JWQTzqQdwTyqtIGLbEb91QvxaexzywKSnu3FnexkH0NOFNW1SPZbgtjkGQb1b9v6kObR56+mhx7NM+kCdWnLd1LbhbhbbJHOmkL26Uf13PTnTw1BFPc9Bj8OfvpkHYAjrQyvAcKyLhrV6oALggdCKkGu3DDDRxn7qGDesfnttQ2w+KHRomqrPqtrGY+J2kGxbYU5+9V68aRRnIiwWbHPyrnmkEjWLPf/FFO7vfNIbmKPiwpXcCtOTUWzHinNIDR34kVwvBGoHNjzqeCZV4nZgVwPIUIKjOKmEjfKFDggcvKgUzR66Cksi4LIfT5HNLe7/jSawggkCSBTkkZ2pobqx4WI9jXTO5Wn28Wmw3gUtcTgh3Y5OB0HZS5kUHfY5tI06eS44ry6Mu+VHDgD6dacLRBBheEfSgUMzi4A27M0aiYsNzmpFoKdvJc+lR1rAwzyx71YjA2qNVBOTzqgkvFtWcQJ51ACWdgScCqcR4iOg5UkLSn15O4pld7mgaRcMFxniJO1E+8GpXFlZzSQsAVG2RQXR7KK/xd3ZaaU7gudh7CsZy5YOnVHj8mNZNPvLpw8VviIHPiSDH5Cun6cpWfjB/CM/lSZ7SJgY8EAgjb2qmizHxxCFUARnJHNt+tFBUjPY+TsdPF6QavucGo03iGeyrQsSu9amJ5gDtSHULVbqHw2G5+EiiLAcBPYarwBuEHlTQgLSoI0Q2pLrNH2t8YqS4s1SY3EchDYwwbfbyrNW+wu7eePaQOBntFRz28VxcTPKgY8fDueQrbW7VMw2Rp48lbVXvZRPKCsKHKA83Pb5ClV3IxURocNIcDyHWqSHwYSEAAA2FI9Pme5ZppDlslQByArWzKhdJKIIgi/ERwqP5003u11DVJo4yDFbjhU9rdaWd47ua2067njIEg9APYPKgfdNQFuDjJwDvQSeaDSxYA1yNE1aYIN+u/WkoGI8lhuNsDNLNcJ/bN0OnFyqG3AmYI/I9lYvs2XRCAWHU+1KY0kljEQOSpyF/fVJlEUrBBgVXJ236c6Qh9d1cNoRVjsGYUBA4JXXPF6jv5Uc7lgNpc2Ryc0LngX9ozLlscZ2zRvpAR7ZFkHcYHbWZh/Eu9TGCMN8Od+tTrEhHwigNaR//2Q=="
)

def _build_vcard(phone: str) -> str:
    header = "PHOTO;ENCODING=b;TYPE=JPEG:" + _TANYA_PHOTO_B64
    lines = [header[:75]]
    rest = header[75:]
    while rest:
        lines.append(" " + rest[:74])
        rest = rest[74:]
    photo_field = "\r\n".join(lines)
    parts = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        "FN:TanyaTalk",
        "N:;TanyaTalk;;;",
        f"TEL;TYPE=CELL:{phone}",
        photo_field,
        "NOTE:The conversation that changes your day. Add this contact for TanyaTalk.",
        "END:VCARD",
        "",
    ]
    return "\r\n".join(parts)


TANYA_VCARD = _build_vcard(BLOOIO_PHONE_NUMBER)

CONTACT_SAVE_PROMPT = "Save my contact so you can always find me."
CONTACT_SAVE_PROMPT_RETURNING = (
    "In case you didn't save my contact the first time, here it is again."
)

NEW_CLIENT_MINIMAL_OPENER = (
    "Save my contact so you can always find me. So glad you reached out, what's on your mind today?"
)

RETURNING_CLIENT_MINIMAL_OPENER = "So glad you're back. What's on your mind today?"

SERVICE_INQUIRY_RESPONSE = (
    "TanyaTalk is your personal coaching experience, built on Tanya's heart and master-level coaching expertise, "
    "available 24/7 right in your pocket. Whether you're working through something hard, building on something good, "
    "or simply trying to move through life with more intention, just text me. "
    "You can learn more at tanya-talk.com."
)

PRICING_INQUIRY_RESPONSE = (
    "You get 25 free messages to start. After that, TanyaTalk is $21/month for 250 messages, 24/7 access, "
    "and personal follow-up texts a couple days after your sessions. "
    "You can see everything included at tanya-talk.com."
)

OPENER_INTRO = (
    "Hi, I'm Tanya. I've spent years helping people work through what's on their mind, "
    "and I'm here for you too. Everything here is built with my heart, so you always "
    "have someone to come to whenever you need it, 24/7.\n\n"
    "All conversations are stored securely and encrypted. "
    "They may be privately reviewed only when necessary by me "
    "to maintain quality, functionality, and improve the TanyaTalk experience. "
    "They are not casually read or shared. You can cancel your subscription at any time by sending "
    "'cancel subscription' and your profile and conversations will be kept so I remember you if you return. "
    "If you ever want everything permanently deleted, send 'delete my data' and it will all be wiped. "
    "So by continuing, you acknowledge and agree to these terms.\n\n"
    "TanyaTalk supports reflection, clarity, and growth, but it is not medical, legal, "
    "financial or emergency advice. Use of TanyaTalk is at your own discretion, and you "
    "are solely responsible for any decisions or actions you take based on my guidance. "
    "By sending your first message, you are confirming your agreement to our terms.\n\n"
    "When you're ready, the more you share with me, the deeper we can go together. "
    "You don't have to have everything figured out, just start wherever you are."
)

# Brief pause between separate new-client opener bubbles (after typing delay).
NEW_CLIENT_OPENER_BEAT_SEC = 1.0

FREE_TRIAL_CLOSE_TEXT = (
    "Unfortunately, this is where our time wraps up. That was a great session. "
    "If you want to keep going, it's $21 a month for 250 messages. "
    "Reply yes and I'll send you the link."
)

FREE_TRIAL_STRIPE_DECLINED = (
    "That's completely okay. Whenever you feel ready, I'll be right here. "
    "Take care of yourself."
)

STRIPE_CONFIRMATION_UNCLEAR_REPLY = (
    "The link has the details on what is included. "
    "Just say yes if you would like me to send it, or no if you are not ready yet."
)

# Optional mid-session referral line: first eligible session number, minimum sessions between nudges.
REFERRAL_NUDGE_FIRST_ELIGIBLE_SESSION = 4
REFERRAL_NUDGE_MIN_SESSIONS_BETWEEN = 4
REFERRAL_NUDGE_MARKER = "<<<REFERRAL_NUDGE>>>"

# Debounce window: rapid messages arriving within this window are merged into one Claude call.
DEBOUNCE_SECONDS = 2.0
# Typing bubble appears this many seconds after a message arrives, before debounce fires.
TYPING_BUBBLE_DELAY_SEC = 3.0
# Re-fire typing_on this often while waiting for Claude, so the indicator doesn't expire.
_TYPING_KEEPALIVE_SEC = 20.0

# Subscription cancellation flow.
CANCEL_TRIGGERS: frozenset[str] = frozenset({
    "cancel subscription",
    "cancel my subscription",
    "unsubscribe",
    "stop my subscription",
    "end my subscription",
    "i want to cancel",
})
CANCEL_MESSAGE_WITH_LINK = (
    "Of course. You can cancel anytime through your billing portal. "
    "Everything will still be here if you ever want to come back.\n\n"
    "{portal_link}"
)
# Right-to-deletion flow.
DELETE_TRIGGERS: frozenset[str] = frozenset({
    "delete my data",
    "delete my information",
    "delete my info",
    "delete my account",
    "erase my data",
    "remove my data",
    "forget me",
    "delete everything",
})
DELETE_CONFIRMATION_PROMPT = (
    "This one I want to get right. Everything we've built together, your sessions, your profile, "
    "all of it, would be gone for good. If that's what you want, just say yes and I'll take care of it. "
    "If not, just say so and we keep going."
)
DELETE_CONFIRMATION_PROMPT_TRIAL = (
    "Just to confirm. Saying yes will permanently delete our conversation and any session notes tied to you. Want to go ahead?"
)
DELETE_CONFIRMED_MESSAGE = (
    "Done. It's all gone. Your sessions, your profile, everything. "
    "If you ever find yourself back here, I'll meet you fresh. Take care of yourself."
)
DELETE_CANCELLED_MESSAGE = (
    "Nothing touched. I'm still here whenever you need me."
)

# Daily new-user onboarding cap — configurable via env var.
DAILY_NEW_USER_CAP = int(os.getenv("DAILY_NEW_USER_CAP", "50"))
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "America/New_York")


def _round_up_to_10min(dt: datetime.datetime) -> datetime.datetime:
    remainder = dt.minute % 10
    if remainder == 0:
        return dt.replace(second=0, microsecond=0)
    return (dt + datetime.timedelta(minutes=10 - remainder)).replace(second=0, microsecond=0)


def build_daily_cap_message() -> str:
    try:
        tz = ZoneInfo(DISPLAY_TIMEZONE)
    except Exception:
        tz = ZoneInfo("America/New_York")
    target = _round_up_to_10min(datetime.datetime.now(tz) + datetime.timedelta(hours=24))
    time_str = target.strftime("%I:%M%p").lstrip("0").lower()
    return (
        f"I can only take on a limited number of new people each day. "
        f"Once you're in, you're in, this is the only time you'll ever see this. "
        f"Text me back tomorrow at {time_str} and we'll get started."
    )

# Monthly message cap for paid subscribers ($20/month = 250 messages).
MONTHLY_MESSAGE_CAP = 250
MONTHLY_CAP_WARNING_AT = 230
MONTHLY_CAP_WARNING_MESSAGE = (
    "Just so you know, we're getting close to your 250 messages for this month. "
    "A little room left, so let's make it count."
)
MONTHLY_CAP_BLOCK_MESSAGE = (
    "You've reached your 250 messages for this month. "
    "Each $5 adds 60 more if you'd like to keep going. "
    "Would you like me to send you the link?"
)
TOPUP_LINK_DECLINED = (
    "No problem at all. I'll be right here when your next month starts."
)
TOPUP_UNCLEAR_REPLY = (
    "The link has the details on what is included. "
    "Just say yes if you would like me to send it, or no if you would like to wait until your subscription refills."
)
TOPUP_CREDITED_MESSAGE = (
    "You're all set. Your messages have been added and we can keep going whenever you're ready."
)

# Per-session message cap — prevents marathon sessions from eating the monthly budget.
# 60 is roughly 3x a typical coaching session; hard to reach naturally.
SESSION_MESSAGE_CAP = 60
SESSION_CAP_WARNING_AT = 57  # 3 messages remain — spec: warn client when nearing cap
SESSION_CAP_WARNING_MESSAGE = (
    "We've been going deep today. Just so you know, we have a little room left in this session."
)
SESSION_CAP_BLOCK_MESSAGE = (
    "We've covered a lot of ground together today. I'm saving our session now. "
    "Come back when you're ready and we'll pick up from here."
)

# Stay well under Anthropic's ~5 min prompt-cache TTL if a ping is skipped.
CACHE_WARM_INTERVAL_SEC = 120

ARCHIVE_REFERENCE_SIGNALS = [
    "remember when", "back when", "earlier session", "a while ago",
    "we discussed", "you mentioned", "last time we", "previous session",
    "early on", "at the beginning", "first session", "originally",
]

ARCHIVE_STOP_WORDS = {
    "i", "a", "an", "the", "and", "or", "but", "in", "on", "at", "to",
    "for", "of", "with", "my", "me", "was", "is", "are", "were", "be",
    "been", "have", "had", "do", "did", "that", "this", "it", "he", "she",
    "we", "they", "you", "your", "about", "when", "what", "how", "why",
    "who", "s", "re", "ve",
}

if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY not set")
if not BLOOIO_API_KEY:
    raise RuntimeError("BLOOIO_API_KEY not set")

claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


async def _claude_create(**kwargs) -> anthropic.types.Message:
    """Wrapper around claude.messages.create with exponential backoff on transient errors."""
    delays = [1, 2, 4, 8, 16]
    for attempt, delay in enumerate(delays, 1):
        try:
            return await claude.messages.create(**kwargs)
        except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
            if attempt == len(delays):
                raise
            logger.warning("Claude API transient error (attempt %d): %s — retrying in %ds", attempt, e, delay)
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


_http = httpx.AsyncClient()

# ---------------------------------------------------------------------------
# Blooio transport helpers
# ---------------------------------------------------------------------------

def phone_to_hash(phone: str) -> str:
    """SHA-256 of normalized E.164 phone. Used for ALL file paths and log identifiers."""
    return hashlib.sha256(normalize_phone(phone).encode()).hexdigest()


def _phone_key(phone: str) -> str:
    """16-char SHA-256 prefix — safe short identifier for logs (mirrors tanya_followup)."""
    return phone_to_hash(phone)[:16]


def is_admin_phone(phone: str) -> bool:
    return normalize_phone(phone) in _ADMIN_PHONES


async def blooio_typing_on(phone: str) -> None:
    """Show typing indicator to client."""
    chat_id_encoded = quote(phone, safe="")
    url = f"{BLOOIO_BASE_URL}/chats/{chat_id_encoded}/typing"
    try:
        await _http.post(
            url,
            headers={"Authorization": f"Bearer {BLOOIO_API_KEY}"},
            timeout=10.0,
        )
    except Exception as e:
        logger.debug("Typing on error: %s", e)


async def blooio_typing_off(phone: str) -> None:
    """Stop typing indicator."""
    chat_id_encoded = quote(phone, safe="")
    url = f"{BLOOIO_BASE_URL}/chats/{chat_id_encoded}/typing"
    try:
        await _http.delete(
            url,
            headers={"Authorization": f"Bearer {BLOOIO_API_KEY}"},
            timeout=10.0,
        )
    except Exception as e:
        logger.debug("Typing off error: %s", e)


async def _blooio_post(url: str, payload: dict, *, label: str = "send", idempotency_key: str | None = None) -> None:
    """POST to Blooio with 2 attempts (3s delay). Retries on 5xx and network errors; not on 4xx."""
    headers = {
        "Authorization": f"Bearer {BLOOIO_API_KEY}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    for attempt in range(1, 3):
        try:
            resp = await _http.post(url, headers=headers, json=payload, timeout=30.0)
            if resp.status_code in (200, 202):
                return
            if resp.status_code < 500:
                logger.error("Blooio %s failed %d: %s", label, resp.status_code, resp.text[:200])
                return
            logger.warning("Blooio %s transient %d (attempt %d): %s", label, resp.status_code, attempt, resp.text[:200])
        except Exception as e:
            logger.warning("Blooio %s network error (attempt %d): %s", label, attempt, e)
        if attempt < 2:
            await asyncio.sleep(3)
    logger.error("Blooio %s failed after 2 attempts", label)


async def blooio_send_message(phone: str, text: str, *, idempotency_key: str | None = None) -> None:
    """Send a text message via Blooio API v2."""
    await blooio_typing_off(phone)
    chat_id_encoded = quote(phone, safe="")
    url = f"{BLOOIO_BASE_URL}/chats/{chat_id_encoded}/messages"
    await _blooio_post(url, {"text": text}, label="send_message", idempotency_key=idempotency_key)


async def _retry_device_unreachable(phone: str, text: str, original_message_id: str) -> None:
    """Retry a message that failed with device_unreachable after a short delay."""
    await asyncio.sleep(15)
    idempotency_key = f"{original_message_id}-retry"
    logger.info("Retrying device_unreachable message to phone_key=%s original_id=%s", _phone_key(phone), original_message_id)
    await blooio_send_message(phone, text, idempotency_key=idempotency_key)


async def blooio_send_vcard(phone: str) -> None:
    """Send TanyaTalk contact card on first interaction so clients can save the number."""
    chat_id_encoded = quote(phone, safe="")
    url = f"{BLOOIO_BASE_URL}/chats/{chat_id_encoded}/messages"
    vcf_url = f"{TANYA_PUBLIC_URL}/tanya.vcf"
    await _blooio_post(
        url,
        {"attachments": [{"url": vcf_url, "name": "My Contact.vcf", "type": "text/vcard"}]},
        label="send_vcard",
    )


def verify_blooio_signature(raw_body: bytes, signature_header: str) -> bool:
    """Verify HMAC-SHA256 from X-Blooio-Signature header. Rejects if no secret is configured."""
    if not BLOOIO_WEBHOOK_SECRET:
        logger.warning("BLOOIO_WEBHOOK_SECRET not set — rejecting unsigned webhook request")
        return False
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        ts = parts.get("t", "")
        v1 = parts.get("v1", "")
        signed = f"{ts}.{raw_body.decode('utf-8', errors='replace')}"
        expected = hmac.new(
            BLOOIO_WEBHOOK_SECRET.encode(),
            signed.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, v1)
    except Exception:
        return False


async def create_stripe_portal_url(phone: str) -> str | None:
    """Generate a customer-specific Stripe Billing Portal session URL."""
    if not STRIPE_SECRET_KEY:
        return None
    try:
        import stripe as stripe_lib
        stripe_lib.api_key = STRIPE_SECRET_KEY
        ph = phone_to_hash(phone)
        customer_id = await billing_db.get_stripe_customer_id(ph)
        if not customer_id:
            # Fallback: search by phone stored in customer metadata at payment time
            results = await asyncio.to_thread(
                stripe_lib.Customer.search,
                query=f"metadata['phone']:'{phone}'",
                limit=1,
            )
            if results.data:
                customer_id = results.data[0]["id"]
                await billing_db.store_stripe_customer_id(ph, customer_id)
        if not customer_id:
            return None
        session = await asyncio.to_thread(
            stripe_lib.billing_portal.Session.create,
            customer=customer_id,
            return_url="https://www.tanya-talk.com",
        )
        return session.url
    except Exception as e:
        logger.warning("Stripe portal URL creation failed: %s", e)
        return None


async def get_stripe_customer_name(phone: str) -> str:
    """Look up Stripe customer by phone number; return display name or 'Client'."""
    if not STRIPE_SECRET_KEY:
        return "Client"
    try:
        import stripe as stripe_lib
        stripe_lib.api_key = STRIPE_SECRET_KEY
        customers = await asyncio.to_thread(
            stripe_lib.Customer.search,
            query=f'phone:"{phone}"',
            limit=1,
        )
        if customers.data:
            name = (customers.data[0].get("name") or "").strip()
            if not name:
                name = (customers.data[0].get("email") or "").strip()
            return name or "Client"
    except Exception as e:
        logger.warning("Stripe name lookup failed: %s", e)
    return "Client"


# ---------------------------------------------------------------------------
# Failure-polling: catch messages missed during restarts/deploys
# ---------------------------------------------------------------------------

_last_failure_poll_at: float = 0.0
_processed_message_ids: set[str] = set()
_MAX_SEEN_IDS = 500


async def _blooio_failure_poll_loop() -> None:
    """Poll Blooio webhook logs every 30s for failed deliveries and replay them."""
    global _last_failure_poll_at
    await asyncio.sleep(10)  # brief startup delay
    while True:
        await asyncio.sleep(30)
        try:
            since_ts = int(_last_failure_poll_at * 1000) if _last_failure_poll_at else 0
            resp = await _http.get(
                f"{BLOOIO_BASE_URL}/webhook-logs",
                headers={"Authorization": f"Bearer {BLOOIO_API_KEY}"},
                params={"status": "failed", "limit": 100, "since": since_ts},
                timeout=15.0,
            )
            if resp.status_code == 404:
                # Endpoint not available on this plan — poll silently
                pass
            elif resp.status_code == 200:
                data = resp.json()
                entries = data if isinstance(data, list) else data.get("data", [])
                _last_failure_poll_at = datetime.datetime.now(timezone.utc).timestamp()
                for entry in entries:
                    payload = entry.get("payload") or entry.get("body") or {}
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            continue
                    event = entry.get("event") or payload.get("event") or ""
                    if event != "message.received":
                        continue
                    msg_id = payload.get("message_id", "")
                    if not msg_id or msg_id in _processed_message_ids:
                        continue
                    sender = payload.get("sender", "")
                    text = payload.get("text", "")
                    if sender and text:
                        logger.info("Replaying missed webhook message_id=%s", msg_id)
                        await handle_inbound_message(sender, text)
                        _processed_message_ids.add(msg_id)
                        if len(_processed_message_ids) > _MAX_SEEN_IDS:
                            # Keep only the most recent half
                            ids_list = list(_processed_message_ids)
                            _processed_message_ids.clear()
                            _processed_message_ids.update(ids_list[-(_MAX_SEEN_IDS // 2):])
            else:
                logger.warning("Blooio failure poll returned %d", resp.status_code)
        except Exception as e:
            logger.warning("Blooio failure poll error: %s", e)


# ---------------------------------------------------------------------------
# Per-phone message serialization
# ---------------------------------------------------------------------------

# Serialize message-driven work per phone so concurrent users do not block each other.
_chat_locks_guard = asyncio.Lock()
_chat_message_locks: dict[str, asyncio.Lock] = {}


async def _get_chat_message_lock(phone: str) -> asyncio.Lock:
    async with _chat_locks_guard:
        if phone not in _chat_message_locks:
            _chat_message_locks[phone] = asyncio.Lock()
        return _chat_message_locks[phone]


def _write_path_utf8(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")

_BOT_DIR = Path(__file__).resolve().parent
AUDIO_DIR = _BOT_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)
AUDIO_RETENTION_SEC = 3600  # keep MP3s long enough for Blooio/CDN retries
COACHING_BLEND_CONFIG_PATH = _BOT_DIR / "logs" / "coaching_blend.json"
PID_FILE_PATH = _BOT_DIR / "logs" / "tanya_bot.pid"
USAGE_CSV_PATH = _BOT_DIR / "logs" / "tanya_usage.csv"
_USAGE_CSV_LEGACY = _BOT_DIR / "tanya_usage.csv"  # old location; migrated on first write
_PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
REFERRAL_STATE_PATH = _BOT_DIR / "logs" / "referral_nudges.json"


_USAGE_CSV_LOCK = threading.Lock()
_REFERRAL_STATE_LOCK = threading.Lock()
_VAULT_GIT_LOCK = threading.Lock()
_VAULT_INDEX_LOCK = asyncio.Lock()


def ensure_vault() -> None:
    """Clone the tanya-brain vault from GitHub using git so changes can be pushed back."""
    if not GITHUB_PAT:
        return
    vault = Path(VAULT_PATH)
    repo_url = GITHUB_VAULT_REPO.replace("https://", f"https://{GITHUB_PAT}@")

    if vault.exists() and (vault / ".git").exists():
        logger.info("Vault exists, pulling latest...")
        try:
            # Abort any in-progress rebase left over from a previous failed pull
            if (vault / ".git" / "rebase-merge").exists() or (vault / ".git" / "rebase-apply").exists():
                subprocess.run(["git", "-C", str(vault), "rebase", "--abort"], capture_output=True, timeout=30)
                logger.info("Aborted stuck rebase before pulling")

            result = subprocess.run(
                ["git", "-C", str(vault), "pull", "--rebase"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                logger.error("git pull --rebase failed: %s", result.stderr)
                # Abort the failed rebase and hard-reset to origin/main so the vault is usable
                subprocess.run(["git", "-C", str(vault), "rebase", "--abort"], capture_output=True, timeout=30)
                subprocess.run(["git", "-C", str(vault), "fetch", "origin"], capture_output=True, timeout=60)
                subprocess.run(
                    ["git", "-C", str(vault), "reset", "--hard", "origin/main"],
                    capture_output=True, timeout=30,
                )
                logger.info("Vault hard-reset to origin/main after rebase conflict")
            else:
                logger.info("Vault updated: %s", result.stdout.strip())
        except Exception as e:
            logger.error("Failed to pull vault: %s", e)
        return

    logger.info("Cloning vault from GitHub...")
    if vault.exists():
        shutil.rmtree(vault)
    vault.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["git", "clone", repo_url, str(vault)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logger.error("git clone failed: %s", result.stderr)
            return
        subprocess.run(["git", "-C", str(vault), "config", "user.email", "tanyabot@railway.app"], capture_output=True)
        subprocess.run(["git", "-C", str(vault), "config", "user.name", "TanyaBot"], capture_output=True)
        logger.info("Vault cloned to %s", vault)
    except Exception as e:
        logger.error("Failed to clone vault: %s", e)


_vault_dirty: bool = False


def mark_vault_dirty() -> None:
    global _vault_dirty
    _vault_dirty = True


def _vault_has_unpushed_work(vault: Path) -> bool:
    """True if the vault has uncommitted changes or commits not yet pushed."""
    try:
        status = subprocess.run(
            ["git", "-C", str(vault), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        if status.stdout.strip():
            return True
        ahead = subprocess.run(
            ["git", "-C", str(vault), "rev-list", "--count", "@{upstream}..HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if ahead.returncode == 0:
            count = ahead.stdout.strip()
            if count.isdigit() and int(count) > 0:
                return True
    except Exception as e:
        logger.warning("Could not check vault git status: %s", e)
    return False


def _do_vault_push() -> bool:
    """Run git add/commit/push synchronously inside a thread. Caller holds _VAULT_GIT_LOCK."""
    vault = Path(VAULT_PATH)
    try:
        subprocess.run(["git", "-C", str(vault), "add", "."], capture_output=True, timeout=30)
        status = subprocess.run(
            ["git", "-C", str(vault), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        has_uncommitted = bool(status.stdout.strip())
        if has_uncommitted:
            result = subprocess.run(
                ["git", "-C", str(vault), "commit", "-m", "Vault sync"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.error("git commit failed: %s", result.stderr)
                return False

        # Check for committed-but-unpushed work (e.g. push was rejected previously)
        ahead = subprocess.run(
            ["git", "-C", str(vault), "rev-list", "--count", "@{upstream}..HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        has_unpushed = (
            ahead.returncode == 0
            and ahead.stdout.strip().isdigit()
            and int(ahead.stdout.strip()) > 0
        )

        if not has_uncommitted and not has_unpushed:
            logger.info("No vault changes to push")
            return True

        # Pull --rebase so Railway can merge any commits pushed from elsewhere
        pull = subprocess.run(
            ["git", "-C", str(vault), "pull", "--rebase"],
            capture_output=True, text=True, timeout=60,
        )
        if pull.returncode != 0:
            logger.error("git pull --rebase failed: %s", pull.stderr)
            return False

        result = subprocess.run(
            ["git", "-C", str(vault), "push"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.error("git push failed: %s", result.stderr)
            return False
        logger.info("Vault pushed successfully")
        return True
    except Exception as e:
        logger.error("Failed to push vault changes: %s", e)
        return False


async def _immediate_vault_push() -> None:
    """Fire-and-forget push triggered immediately after session finalize."""
    if not GITHUB_PAT:
        return
    vault = Path(VAULT_PATH)
    if not (vault / ".git").exists():
        return
    try:
        def _push_locked() -> bool:
            with _VAULT_GIT_LOCK:
                return _do_vault_push()
        success = await asyncio.to_thread(_push_locked)
        if success:
            global _vault_dirty
            _vault_dirty = False
    except Exception as e:
        logger.error("Immediate vault push failed: %s", e)


async def _vault_push_loop() -> None:
    """Background loop: push vault to GitHub every 2 minutes when dirty."""
    await asyncio.sleep(60)  # brief startup delay
    while True:
        await asyncio.sleep(120)
        global _vault_dirty
        if not _vault_dirty:
            continue
        if not GITHUB_PAT:
            continue
        vault = Path(VAULT_PATH)
        if not (vault / ".git").exists():
            continue

        def _push_locked() -> bool:
            with _VAULT_GIT_LOCK:
                return _do_vault_push()

        success = await asyncio.to_thread(_push_locked)
        if success:
            _vault_dirty = await asyncio.to_thread(_vault_has_unpushed_work, vault)
        # On failure, leave _vault_dirty True so the next cycle retries.


SESSION_SNAPSHOT_PATH = _BOT_DIR / "logs" / "session_snapshot.json"
PENDING_FINALIZE_PATH = _BOT_DIR / "logs" / "pending_session_finalize.json"
ONBOARDING_CHECKPOINT_PATH = _BOT_DIR / "logs" / "onboarding_checkpoints.json"
_pending_finalize_file_lock = threading.Lock()
_onboarding_checkpoint_lock = threading.Lock()
_WHOLE_SESSION_FILE_RE = re.compile(r"^Session ([1-9]\d*)\.md$")
_DABBLE_SESSION_FILE_RE = re.compile(r"^Session 0(?:\.(\d+))?\.md$")
_finalize_tasks: set[asyncio.Task] = set()
USAGE_CSV_FIELDNAMES = [
    "log_id",
    "timestamp",
    "phone_hash",
    "user",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "approx_usd",
]
last_coaching_usage: dict[str, dict] = {}


def _pacific_now_iso(*, timespec: str = "seconds") -> str:
    """Usage CSV timestamps in US Pacific (PST/PDT via America/Los_Angeles)."""
    return datetime.datetime.now(_PACIFIC_TZ).isoformat(timespec=timespec)


def acquire_single_instance_lock() -> None:
    """Exclusive PID file so a second process fails fast."""
    PID_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            fd = os.open(str(PID_FILE_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            stale = ""
            try:
                stale = PID_FILE_PATH.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            if attempt == 0 and stale.isdigit():
                stale_pid = int(stale)
                # In Railway containers the app runs as PID 1. After a crash the lock
                # file persists on the volume with PID 1, and the new container also
                # starts as PID 1 — so os.kill(1, 0) succeeds even though it's stale.
                # If the PID matches our own, the lock is definitely from a prior run.
                if stale_pid == os.getpid():
                    try:
                        PID_FILE_PATH.unlink(missing_ok=True)
                    except (TypeError, OSError):
                        if PID_FILE_PATH.exists():
                            PID_FILE_PATH.unlink()
                    continue
                try:
                    os.kill(stale_pid, 0)
                except ProcessLookupError:
                    try:
                        PID_FILE_PATH.unlink(missing_ok=True)
                    except TypeError:
                        if PID_FILE_PATH.exists():
                            PID_FILE_PATH.unlink()
                    except OSError:
                        pass
                    continue
                except PermissionError:
                    pass
            extra = f" It contains PID {stale}." if stale else ""
            raise RuntimeError(
                "Another Tanya bot may already be running (lock file "
                f"{PID_FILE_PATH}).{extra} Stop the other process, or delete the lock file if it crashed."
            ) from None
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return


def release_single_instance_lock() -> None:
    try:
        if PID_FILE_PATH.exists():
            text = PID_FILE_PATH.read_text(encoding="utf-8").strip()
            if text == str(os.getpid()):
                PID_FILE_PATH.unlink()
    except OSError:
        pass


def extract_usage_from_response(response) -> dict | None:
    """Return token counts and approx_usd for one Claude response, or None."""
    u = getattr(response, "usage", None)
    if u is None:
        return None
    inp = int(getattr(u, "input_tokens", 0) or 0)
    out = int(getattr(u, "output_tokens", 0) or 0)
    cr = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    cw = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    approx_usd = (
        inp * PRICE_INPUT_PER_MTOK
        + out * PRICE_OUTPUT_PER_MTOK
        + cr * PRICE_CACHE_READ_PER_MTOK
        + cw * PRICE_CACHE_WRITE_PER_MTOK
    ) / 1_000_000
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
        "approx_usd": approx_usd,
    }


def log_claude_usage(tag: str, info: dict) -> None:
    """Log token usage (and optional rough USD line) from an extract_usage dict."""
    logger.info(
        "%s | Claude usage | input=%d output=%d cache_read=%d cache_write=%d",
        tag,
        info["input_tokens"],
        info["output_tokens"],
        info["cache_read_input_tokens"],
        info["cache_creation_input_tokens"],
    )
    if LOG_SESSION_COST_USD:
        logger.info(
            "%s | ~USD (approx; set ANTHROPIC_PRICE_* in .env to match anthropic.com/pricing): $%.5f",
            tag,
            info["approx_usd"],
        )


def _ensure_usage_csv_log_id_schema_unlocked() -> None:
    """If tanya_usage.csv exists without a log_id column, rewrite with log_id 1..n. Caller must hold _USAGE_CSV_LOCK."""
    if not USAGE_CSV_PATH.exists():
        return
    with USAGE_CSV_PATH.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        if not fields or "log_id" in fields:
            return
        old_rows = list(reader)
    tmp = USAGE_CSV_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=USAGE_CSV_FIELDNAMES, lineterminator="\n")
        w.writeheader()
        for i, old in enumerate(old_rows, start=1):
            row = {"log_id": i}
            for k in USAGE_CSV_FIELDNAMES:
                if k != "log_id":
                    row[k] = old.get(k, "")
            w.writerow(row)
    tmp.replace(USAGE_CSV_PATH)
    logger.info("Migrated usage CSV to include log_id (%d rows)", len(old_rows))


def _usage_csv_legacy_rename_unlocked() -> None:
    """Move legacy root CSV into logs/. Caller must hold _USAGE_CSV_LOCK."""
    if _USAGE_CSV_LEGACY.exists() and not USAGE_CSV_PATH.exists():
        try:
            _USAGE_CSV_LEGACY.rename(USAGE_CSV_PATH)
            logger.info("Moved usage log from %s to %s", _USAGE_CSV_LEGACY, USAGE_CSV_PATH)
        except OSError as e:
            logger.warning("Could not move legacy usage CSV: %s", e)


def _compute_next_usage_log_id_unlocked() -> int:
    """Next log_id after schema is OK. Caller must hold _USAGE_CSV_LOCK."""
    if not USAGE_CSV_PATH.exists():
        return 1
    with USAGE_CSV_PATH.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "log_id" not in reader.fieldnames:
            return 1
        rows = list(reader)
    if not rows:
        return 1
    best = 0
    for r in rows:
        v = (r.get("log_id") or "").strip()
        if v.isdigit():
            best = max(best, int(v))
    return best + 1


def init_usage_csv_file() -> None:
    """Create logs/ and an empty tanya_usage.csv with headers so the file is always findable."""
    USAGE_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _USAGE_CSV_LOCK:
        if _USAGE_CSV_LEGACY.exists() and not USAGE_CSV_PATH.exists():
            try:
                _USAGE_CSV_LEGACY.rename(USAGE_CSV_PATH)
                logger.info("Moved usage log from %s to %s", _USAGE_CSV_LEGACY, USAGE_CSV_PATH)
                _ensure_usage_csv_log_id_schema_unlocked()
                return
            except OSError as e:
                logger.warning("Could not move legacy usage CSV: %s", e)
        if not USAGE_CSV_PATH.exists():
            with USAGE_CSV_PATH.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=USAGE_CSV_FIELDNAMES, lineterminator="\n")
                w.writeheader()
            logger.info("Usage CSV initialized at %s", USAGE_CSV_PATH)


def record_coaching_usage(phone: str, username: str, response) -> None:
    """Log + remember + append CSV for one Tanya reply."""
    info = extract_usage_from_response(response)
    if not info:
        return
    ph = phone_to_hash(phone)
    tag = f"coaching phone_hash={ph[:12]} user={username}"
    log_claude_usage(tag, info)
    try:
        with _USAGE_CSV_LOCK:
            USAGE_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
            _usage_csv_legacy_rename_unlocked()
            _ensure_usage_csv_log_id_schema_unlocked()
            log_id = _compute_next_usage_log_id_unlocked()
            row = {
                "log_id": log_id,
                "timestamp": _pacific_now_iso(),
                "phone_hash": ph,
                "user": username,
                "model": CLAUDE_MODEL,
                **info,
            }
            new_file = not USAGE_CSV_PATH.exists()
            with USAGE_CSV_PATH.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=USAGE_CSV_FIELDNAMES,
                    extrasaction="ignore",
                    lineterminator="\n",
                )
                if new_file:
                    w.writeheader()
                w.writerow(row)
        last_coaching_usage[phone] = row
    except Exception as e:
        logger.error("Could not write %s: %s", USAGE_CSV_PATH, e)


FREE_TRIAL_MIN_MSGS_FOR_CLOSE = FREE_TRIAL_USER_MESSAGE_CAP


# ---------------------------------------------------------------------------
# Billing helpers — thin async wrappers around billing_db
# ---------------------------------------------------------------------------

async def has_tanyatalk_access(phone: str) -> bool:
    """Memory-first check; falls back to SQLite (caches result for session duration)."""
    if paid_tanyatalk_access.get(phone):
        return True
    if mesh_tanyatalk_included.get(phone):
        return True
    if _is_bypass_phone(phone):
        return True
    ph = phone_to_hash(phone)
    if await billing_db.has_access(ph):
        paid_tanyatalk_access[phone] = True
        return True
    return False


async def should_block_unpaid_after_free_trial(phone: str) -> bool:
    if not BLOCK_AFTER_FREE_TRIAL:
        return False
    if _is_bypass_phone(phone):
        return False
    if not await _has_completed_free_trial(phone):
        return False
    if await has_tanyatalk_access(phone):
        return False
    return True


async def _has_completed_free_trial(phone: str) -> bool:
    """Memory-first; falls back to SQLite to survive restarts."""
    if free_trial_completed.get(phone):
        return True
    ph = phone_to_hash(phone)
    if await billing_db.has_completed_trial(ph):
        free_trial_completed[phone] = True
        return True
    return False


async def mark_free_trial_completed(phone: str) -> None:
    ph = phone_to_hash(phone)
    free_trial_completed[phone] = True
    await billing_db.mark_trial_completed(ph)
    tanya_followup.cancel_all_followup_jobs_for_chat(phone)


async def in_first_free_trial_session(phone: str) -> bool:
    if session_numbers.get(phone) != 1:
        return False
    return not await _has_completed_free_trial(phone)


async def _is_at_monthly_message_cap(phone: str) -> bool:
    """True when a paid subscriber has used all base + top-up messages this billing period."""
    if not await has_tanyatalk_access(phone) or _is_bypass_phone(phone):
        return False
    ph = phone_to_hash(phone)
    count = await billing_db.get_monthly_message_count(ph)
    extra = await billing_db.get_extra_messages(ph)
    return count >= MONTHLY_MESSAGE_CAP + extra


async def _check_monthly_cap_for_followup(phone: str) -> bool:
    """Async cap check passed to tanya_followup — True when client has no messages left."""
    return await _is_at_monthly_message_cap(phone)


conversations: dict[str, list[dict]] = {}
voice_enabled: dict[str, bool] = {}
client_names: dict[str, str] = {}       # phone -> display name (from Stripe)
last_activity: dict[str, datetime.datetime] = {}
timeout_tasks: dict[str, asyncio.Task] = {}
session_files: dict[str, Path] = {}     # phone -> active session file path
session_numbers: dict[str, int] = {}   # phone -> current session number
session_outlines: dict[str, str] = {}  # phone -> coaching outline (loaded once per session)
session_profiles: dict[str, str] = {}  # phone -> client profile (loaded once per session)
voice_note_redirects: dict[str, int] = {}  # not used over iMessage; kept for schema compat
free_trial_user_msg_count: dict[str, int] = {}
free_trial_90_warned: dict[str, bool] = {}
free_trial_completed: dict[str, bool] = {}  # survives end_session; do not pop
awaiting_stripe_confirmation: dict[str, bool] = {}
awaiting_contact_save: dict[str, bool] = {}
pending_first_message_opener: dict[str, bool] = {}
paid_tanyatalk_access: dict[str, bool] = {}  # True when Stripe subscription active
mesh_tanyatalk_included: dict[str, bool] = {}  # True when client is in MESH with TanyaTalk included
referral_nudge_used_this_session: dict[str, bool] = {}
cache_warm_tasks: dict[str, asyncio.Task] = {}
_cached_static_prompts: dict[str, str] = {}
_pending_messages: dict[str, list[str]] = {}  # debounce buffer
_pending_phones: dict[str, str] = {}          # phone -> phone (replaces _pending_updates)
_debounce_tasks: dict[str, asyncio.Task] = {}
_typing_tasks: dict[str, asyncio.Task] = {}   # 3-second typing bubble timer
awaiting_delete_confirmation: dict[str, bool] = {}
awaiting_topup_confirmation: dict[str, bool] = {}
topup_link_sent: dict[str, bool] = {}  # link delivered; cap gate stays until Stripe webhook credits
new_client_voice_followup_snippet: dict[str, str] = {}  # kept for opener context; TTS not sent over iMessage

MAX_HISTORY = 40

# Whole-message session closes after normalize_session_end_candidate (not Claude interpretation).
SESSION_END_NORMALIZED = frozenset(
    {
        "end session",
        "end the session",
        "ok end session",
        "okay end session",
        "please end session",
        "end session please",
        "end session now",
        "lets end session",
        "done",
        "im done",
        "i'm done",
        "all done",
        "done for today",
        "done for now",
        "done for the day",
        "im all done",
        "i'm all done",
        "done session",
        "done with session",
        "session done",
        "end the session",
        "close session",
        "close the session",
        "finish session",
        "finished session",
    }
)


def sanitize_name_for_path(name: str) -> str:
    """Remove path-traversal characters from a client name before using it in file paths."""
    cleaned = re.sub(r'[/\\\x00]', '', name)    # strip directory separators and null bytes
    cleaned = re.sub(r'\.{2,}', '.', cleaned)   # collapse .. to prevent traversal
    cleaned = cleaned.strip('. ')               # remove leading/trailing dots and spaces
    cleaned = cleaned[:60]                      # cap length
    return cleaned or "Client"


def normalize_session_end_candidate(text: str) -> str:
    """Lowercase, commas → spaces, strip apostrophes (let's → lets), collapse whitespace, strip trailing ?!."""
    t = text.strip().lower().replace(",", " ")
    for ch in ("\u2019", "\u2018", "'"):  # curly + straight apostrophe
        t = t.replace(ch, "")
    t = " ".join(t.split()).rstrip("!.?")
    return t


SESSION_END_TAIL_PHRASES = frozenset(
    {"end session", "end the session", "end session now", "end session please"}
)


def is_session_end_message(text: str) -> bool:
    """True when the message is, contains, or closely resembles a recognized session-end phrase."""
    normalized = normalize_session_end_candidate(text)
    # Exact match
    if normalized in SESSION_END_NORMALIZED:
        return True
    # Contains or ends with a known tail phrase ("end session I mean", "please end session")
    if any(phrase in normalized for phrase in SESSION_END_TAIL_PHRASES):
        return True
    # Fuzzy match for misspellings — compares full normalized text against each known phrase
    for phrase in SESSION_END_NORMALIZED:
        if difflib.SequenceMatcher(None, normalized, phrase).ratio() >= 0.82:
            return True
    return False


# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------

# Tanya's cloned voice: set ELEVENLABS_VOICE_ID in .env (see .env.example).
ELEVENLABS_TTS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"


async def synthesize_voice(text: str) -> bytes | None:
    if not ELEVENLABS_API_KEY:
        return None
    try:
        resp = await _http.post(
            ELEVENLABS_TTS_URL,
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.71,
                    "similarity_boost": 0.85,
                    "style": 0.64,
                    "use_speaker_boost": False,
                    "speed": 0.95,
                },
            },
            timeout=30.0,
        )
        if resp.status_code == 200:
            return resp.content
        logger.error("ElevenLabs error %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("ElevenLabs request failed: %s", e)
    return None


async def synthesize_and_host_voice(text: str) -> str | None:
    """Synthesize text via ElevenLabs, save MP3 to AUDIO_DIR, return public URL."""
    import uuid as _uuid
    audio_bytes = await synthesize_voice(text)
    if not audio_bytes:
        return None
    filename = f"{_uuid.uuid4().hex}.mp3"
    audio_path = AUDIO_DIR / filename
    audio_path.write_bytes(audio_bytes)
    return f"{TANYA_PUBLIC_URL}/audio/{filename}"


def _cleanup_stale_audio_files(max_age_sec: float = AUDIO_RETENTION_SEC) -> int:
    """Remove hosted voice MP3s older than max_age_sec."""
    if not AUDIO_DIR.exists():
        return 0
    cutoff = datetime.datetime.now().timestamp() - max_age_sec
    removed = 0
    for path in AUDIO_DIR.glob("*.mp3"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    return removed


async def _audio_cleanup_loop() -> None:
    """Periodically delete expired hosted voice files."""
    while True:
        await asyncio.sleep(600)
        try:
            removed = await asyncio.to_thread(_cleanup_stale_audio_files)
            if removed:
                logger.info("Cleaned up %d stale audio file(s)", removed)
        except Exception as e:
            logger.warning("Audio cleanup failed: %s", e)


async def blooio_send_audio(phone: str, audio_url: str) -> None:
    """Send a hosted MP3 as an iMessage audio attachment via Blooio."""
    chat_id_encoded = quote(phone, safe="")
    url = f"{BLOOIO_BASE_URL}/chats/{chat_id_encoded}/messages"
    try:
        resp = await _http.post(
            url,
            headers={
                "Authorization": f"Bearer {BLOOIO_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"attachments": [{"url": audio_url, "type": "audio/mpeg"}]},
            timeout=30.0,
        )
        if resp.status_code not in (200, 202):
            logger.warning("Blooio audio send failed %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Blooio audio send error: %s", e)


# ---------------------------------------------------------------------------
# Vault loading
# ---------------------------------------------------------------------------

def load_file(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""


def load_core_context() -> str:
    """Always-loaded files sent with every message."""
    vault = Path(VAULT_PATH)
    core_files = [
        ("Tanya Voice Profile", vault / "00-MOC" / "Tanya-Voice-Profile.md"),
        ("Client Response Protocol", vault / "00-MOC" / "Client-Response-Protocol.md"),
        ("Conversation Guidelines", vault / "00-MOC" / "CONVERSATION-GUIDELINES.md"),
    ]
    sections = []
    for label, filepath in core_files:
        content = load_file(filepath)
        if content:
            sections.append(f"### {label}\n\n{content}")
    return "\n\n---\n\n".join(sections)


def load_session_outline() -> str:
    """Load the coaching session outline once per session — not every message."""
    path = Path(VAULT_PATH) / "01-Frameworks" / "Coaching-Session-Outline.md"
    content = load_file(path)
    if content:
        logger.info("Coaching session outline loaded (%d chars)", len(content))
    else:
        logger.warning("Coaching-Session-Outline.md not found at %s", path)
    return content


def _safe_vault_path(base: Path, *parts: str) -> Path | None:
    """Resolve a path inside the vault and return it only if it stays within the vault root."""
    try:
        resolved = (base / Path(*parts)).resolve()
        if resolved.is_relative_to(base.resolve()):
            return resolved
    except Exception:
        pass
    logger.warning("Path traversal attempt blocked: %s under %s", parts, base)
    return None


def load_client_profile(phone_hash: str) -> str:
    """Load client profile by phone hash."""
    profile_path = Path(VAULT_PATH) / "02-Client-Sessions" / "Client Profiles" / f"{phone_hash}.md"
    content = load_file(profile_path)
    if content:
        logger.info("Loaded profile for hash: %s", phone_hash[:12])
    return content


def profile_path_for(phone_hash: str) -> Path:
    return Path(VAULT_PATH) / "02-Client-Sessions" / "Client Profiles" / f"{phone_hash}.md"


def strip_archived_section(profile_text: str) -> str:
    """Remove the ## Archived Sessions section and everything after it."""
    marker = "## Archived Sessions"
    idx = profile_text.find(marker)
    if idx == -1:
        return profile_text
    return profile_text[:idx].rstrip() + "\n"


def cap_profile_sessions(profile_text: str) -> str:
    """Keep the 10 most recent session rows; move older rows to ## Archived Sessions."""
    sessions_marker = "## Sessions"
    idx = profile_text.find(sessions_marker)
    if idx == -1:
        return profile_text

    section_start = idx + len(sessions_marker)
    next_section = re.search(r"\n## (?!Sessions)", profile_text[section_start:])
    if next_section:
        section_end = section_start + next_section.start()
    else:
        section_end = len(profile_text)

    section_body = profile_text[section_start:section_end]
    lines = section_body.strip().splitlines()

    header_lines = []
    data_rows = []
    seen_separator = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            is_separator = all(c == "" or set(c) <= {"-", ":"} for c in cells)
            if is_separator:
                seen_separator = True
                header_lines.append(line)
            elif not seen_separator:
                # Any pipe row before the separator is a column header, not data
                header_lines.append(line)
            else:
                data_rows.append(line)
        elif not data_rows:
            header_lines.append(line)

    if len(data_rows) <= 10:
        return profile_text

    keep_rows = data_rows[-10:]
    archive_rows = data_rows[:-10]

    new_sessions = sessions_marker + "\n" + "\n".join(header_lines) + "\n" + "\n".join(keep_rows) + "\n"

    archived_marker = "## Archived Sessions"
    arch_idx = profile_text.find(archived_marker)

    existing_archive_rows: list[str] = []
    if arch_idx != -1:
        arch_section_start = arch_idx + len(archived_marker)
        arch_body = profile_text[arch_section_start:].strip()
        for line in arch_body.splitlines():
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if not all(c == "" or set(c) <= {"-", ":"} for c in cells):
                    existing_archive_rows.append(stripped)

    existing_keys = set()
    for row in existing_archive_rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) >= 2:
            existing_keys.add((cells[0], cells[1]))

    merged_archive = list(existing_archive_rows)
    for row in archive_rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        key = (cells[0], cells[1]) if len(cells) >= 2 else None
        if key and key not in existing_keys:
            merged_archive.append(row.strip())
            existing_keys.add(key)

    archive_table_header = "| Date | Key Theme | File |\n|---|---|---|"
    archive_section = f"\n\n{archived_marker}\n{archive_table_header}\n" + "\n".join(merged_archive) + "\n"

    before_sessions = profile_text[:idx]
    after_section = profile_text[section_end:]
    if arch_idx != -1:
        after_section_clean = after_section.replace(profile_text[arch_idx:], "")
    else:
        after_section_clean = after_section

    return before_sessions + new_sessions + after_section_clean.rstrip() + archive_section


def parse_archived_sessions(profile_text: str) -> list[dict]:
    """Parse the ## Archived Sessions table into a list of dicts."""
    marker = "## Archived Sessions"
    idx = profile_text.find(marker)
    if idx == -1:
        return []

    section_body = profile_text[idx + len(marker):]
    next_section = re.search(r"\n## ", section_body)
    if next_section:
        section_body = section_body[:next_section.start()]

    rows = []
    for line in section_body.strip().splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(c == "" or set(c) <= {"-", ":"} for c in cells):
            continue
        if len(cells) < 3:
            continue
        date_val = cells[0]
        theme_val = cells[1]
        file_cell = cells[2]
        wikilink_match = re.search(r"\[\[([^|]+?)(?:\|[^\]]+)?\]\]", file_cell)
        file_path = wikilink_match.group(1) if wikilink_match else ""
        rows.append({"date": date_val, "theme": theme_val, "file_path": file_path})
    return rows


def find_matching_archived_sessions(message: str, archived_rows: list[dict]) -> list[dict]:
    """Return up to 2 archived rows whose theme overlaps with the message keywords."""
    msg_tokens = set(re.findall(r"[a-z']+", message.lower())) - ARCHIVE_STOP_WORDS
    scored = []
    for row in archived_rows:
        theme_tokens = set(re.findall(r"[a-z']+", row["theme"].lower())) - ARCHIVE_STOP_WORDS
        overlap = len(msg_tokens & theme_tokens)
        if overlap > 0:
            scored.append((overlap, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:2]]


async def load_archive_context(matched_rows: list[dict]) -> str:
    """Read session files for matched archived rows and format them for context injection."""
    sections = []
    for row in matched_rows:
        file_path = row["file_path"]
        if not file_path.endswith(".md"):
            file_path += ".md"
        full_path = _safe_vault_path(Path(VAULT_PATH), file_path)
        if not full_path:
            continue
        content = await asyncio.to_thread(load_file, full_path)
        if content:
            sections.append(
                f"[ARCHIVED SESSION — {row['date']} — {row['theme']}]\n"
                f"{content}\n"
                f"[END ARCHIVED SESSION]"
            )
    return "\n\n".join(sections)


def profile_indicates_prior_session(content: str) -> bool:
    """True when the profile reflects at least one completed session, not a blank template."""
    if not content or len(content.strip()) < 80:
        return False
    # Session row from end_session / profile updates (ISO date in Sessions table)
    if re.search(r"\|\s*20\d{2}-\d{1,2}-\d{1,2}\s*\|", content):
        return True
    # Obsidian link to a numbered session file in the vault
    if re.search(r"\[\[02-Client-Sessions/[^\]]+/Session\s+\d+", content):
        return True
    return False


def _whole_session_files(session_dir: Path) -> list[Path]:
    """Session N.md only — excludes dabble files like Session 0.md, Session 0.1.md."""
    if not session_dir.exists():
        return []
    return [f for f in session_dir.iterdir() if _WHOLE_SESSION_FILE_RE.fullmatch(f.name)]


def _dabble_session_files(session_dir: Path) -> list[Path]:
    if not session_dir.exists():
        return []
    return [f for f in session_dir.iterdir() if _DABBLE_SESSION_FILE_RE.fullmatch(f.name)]


def _session_one_completed_on_disk(phone_hash: str) -> bool:
    """True when Session 1.md exists and was closed normally (not a quiet dabble archive)."""
    session_file = Path(VAULT_PATH) / "02-Client-Sessions" / phone_hash / "Session 1.md"
    if not session_file.exists():
        return False
    try:
        return "<!-- session:closed -->" in session_file.read_text(encoding="utf-8")
    except OSError:
        return False


def is_returning_client(phone_hash: str) -> bool:
    """True after a completed whole session — not after quiet free-trial dabbles only."""
    session_dir = Path(VAULT_PATH) / "02-Client-Sessions" / phone_hash
    whole = _whole_session_files(session_dir)
    for f in whole:
        m = _WHOLE_SESSION_FILE_RE.fullmatch(f.name)
        if m and int(m.group(1)) >= 2:
            return True
    if _session_one_completed_on_disk(phone_hash):
        return True
    # Dabble-only clients (Session 0, 0.1, 0.2, …) stay on the new-client onboarding path.
    if _dabble_session_files(session_dir) and not whole:
        return False
    path = profile_path_for(phone_hash)
    if not path.exists():
        return False
    return profile_indicates_prior_session(load_file(path)) and bool(whole)


def _detect_interrupted_previous_session(phone_hash: str, current_session_num: int) -> bool:
    """True if the most recent previous session ended without a clean close and was recent.

    A clean close writes '<!-- session:closed -->' to the session file. If that marker is
    absent and the file was modified within SESSION_TIMEOUT_MINUTES*2, a crash or transport
    outage likely cut the session short.
    """
    if current_session_num <= 1:
        return False
    prev_num = current_session_num - 1
    session_dir = Path(VAULT_PATH) / "02-Client-Sessions" / phone_hash
    prev_file = session_dir / f"Session {prev_num}.md"
    if not prev_file.exists():
        return False
    try:
        age_minutes = (datetime.datetime.now().timestamp() - prev_file.stat().st_mtime) / 60
        if age_minutes > SESSION_TIMEOUT_MINUTES * 2:
            return False
        return "<!-- session:closed -->" not in prev_file.read_text(encoding="utf-8")
    except Exception:
        return False


def template_path() -> Path:
    return Path(VAULT_PATH) / "Templates" / "Client-Profile-Template.md"


def wound_to_framework_map_excerpt() -> str:
    """Excerpt from Client-Response-Protocol for profile Session Framework Routing."""
    text = load_file(Path(VAULT_PATH) / "00-MOC" / "Client-Response-Protocol.md")
    start = text.find("## Wound-to-Framework Map")
    if start == -1:
        return ""
    return text[start : start + 4000]


# Framework routing
FRAMEWORK_ROUTES = [
    (["stuck", "don't know what to do", "paralyz", "frozen", "lost", "nowhere"],
     ["4 step reset.md", "Belief Excavation.md"]),
    (["scar", "anxious", "anxiety", "afraid", "fear", "worried", "worry", "not safe"],
     ["Scaffolding vs Bypassing.md", "4 step reset.md"]),
    (["sabotag", "self-sabotag", "keep getting in my own way", "upper limit"],
     ["Scaffolding vs Bypassing.md"]),
    (["deserve", "worthy", "not enough", "enough", "imposter", "fraud", "who am i"],
     ["Belief Excavation.md"]),
    (["deflect", "can't receive", "can't take a compliment", "block it", "can't let it in",
      "scanning for approval", "need their approval", "performing for", "prove myself",
      "shrink", "over-explain", "feel like i belong", "feel worthy", "claim my worth",
      "embody worth", "belong to myself"],
     ["FutureYou/FutureYou on Self Worth Presentation.md", "Belief Excavation.md"]),
    (["i know i can but", "believe it but", "ready but", "hesitant", "doesn't feel like me",
      "feels weird", "feels foreign", "feels unfamiliar", "feels strange", "step into",
      "rehearse", "freeze when", "freeze in the moment", "can't seem to start",
      "cognitively i know", "mentally i know", "i get it but"],
     ["FYF/FYF The Born Identity.md", "FutureYou/FutureYou Actualization vs Acquisition.md"]),
    (["exhaust", "overwhelm", "burned out", "burnout", "tired", "too much"],
     ["Dips and Sips.md", "Alignment Formula.md"]),
    (["alone", "nobody", "no support", "unsupported", "no one gets it"],
     ["Power Reclamation (Franks question).md", "Expectations.md"]),
    (["relationship", "partner", "husband", "wife", "boyfriend", "girlfriend", "family"],
     ["Expectations.md", "Power Reclamation (Franks question).md"]),
    (["can't decide", "decision", "don't know which", "confused about what to choose"],
     ["Alignment.md", "Frequency choices.md"]),
    (["low energy", "dip", "off", "not myself", "flat", "disconnected"],
     ["Dips and Sips.md", "Coherence Protocol.md"]),
    (["breakthrough", "clicked", "shift", "something opened", "it landed"],
     ["FutureYou/FutureYou Anchoring.md", "FYF/FYF Moments of Me2.0.md"]),
    (["don't know what i want", "what do i want", "no clarity on", "purpose", "lost my why"],
     ["FYF/FYF Desire Excavation.md", "FYF/FYF Vision Timeline.md"]),
    (["manifest", "law of attraction", "attracting", "frequency", "vibration", "not working"],
     ["FutureYou/FutureYou Frequency 101.md", "Alignment Formula.md"]),
    (["future", "who i'm becoming", "next version", "future me", "futureyou"],
     ["FutureYou/FutureYou Actualization vs Acquisition.md"]),
    (["belief", "thought", "story i tell myself", "narrative", "pattern"],
     ["Belief Excavation.md", "4 step reset.md"]),
    (["emotion", "feeling", "feel like", "emotions", "feelings"],
     ["Emotions vs Feelings.md"]),
    (["align", "alignment", "off track", "not aligned"],
     ["Alignment.md", "Alignment Formula.md"]),
    (["neutral", "accept", "resistance", "allow", "letting go"],
     ["Neutrality and Wholness.md"]),
]

# Full vault registry: relative path under 01-Frameworks/ → 1-line coaching description.
# Used by the Haiku framework-selection call so it can reason across every framework,
# not just the keyword-routed subset.
FRAMEWORK_DESCRIPTIONS: dict[str, str] = {
    # --- Standalone frameworks ---
    "4 step reset.md": "Four-step reset: name the feeling, find the thought driving it, reframe it, build airtime for the new perspective",
    "Alignment Formula.md": "Two-step formula: soften the body's resistance through presence, then rewrite the thought driving it",
    "Alignment.md": "Return to the aware self beneath thoughts and emotion — the stable inner ground underneath all identity",
    "Belief Excavation.md": "Dismantle limiting beliefs by naming the emotion, identifying the root belief, finding counterexamples, anchoring new truth",
    "Coherence Protocol.md": "Collapse indecision between two aligned paths by checking energetic coherence — which choice your body already knows",
    "Dips and Sips.md": "Reframe frustration as the feeling of learning; resilience comes from staying in the discomfort longer, not escaping",
    "Emotions vs Feelings.md": "Emotions are body data moving as energy; feelings are the meaning your mind assigns — know the difference",
    "Expectations.md": "Reframe unmet expectations as alignment filtering — what falls away isn't failing you, it's serving you",
    "Frequency choices.md": "Choose between paths using your nervous system as compass — which option your body recognizes as aligned",
    "Future casting vs Rewriting.md": "Rewriting clears resistance; future casting activates the next identity — each does a different job",
    "Meditation Alignment RYTE Origin.md": "Guided meditation to drop into the Still Core beneath thought and access the meta-awareness where alignment accelerates",
    "Negative manifestations.md": "Locate the body-level energy around what you want, identify the blocking belief, shift the frequency through imagination",
    "Neutrality and Wholness.md": "Move beyond labeling experiences as lack or abundance — find the neutral wholeness underneath where neither pole controls you",
    "Origin.md": "Seven origin-point practices for embodying your Source-self through identity, not effort — remembering what you actually are",
    "Patience Embodiment_.md": "Build patience as a felt identity — body safety, micro-proofs, and identity lock-in, not just behavioral waiting",
    "Power Reclamation (Franks question).md": "Reclaim yourself as the source of results — stop outsourcing to tools, other people, or circumstances",
    "Scaffolding vs Bypassing.md": "Scaffolding consciously rehearses a new identity; bypassing uses external fixes to avoid the inner work behind them",
    "The Alignment Arc.md": "15-minute guided arc through belief excavation, nervous system reset, future-identity embodiment, and aligned action",
    "Ubiquitous Assimilation.md": "Recognize pervasive cultural conditioning and doublethink that block authentic alignment — reclaim individual thinking",
    "Yin Yang.md": "Balance Source-grounded being (Yin) with aligned action (Yang) — neither completes the bridge without the other",
    # --- FutureYou frameworks ---
    "FutureYou/FutureYou 7 Day Challenge.md": "Stabilize a new identity through 7 daily believable actions aligned with who you're becoming",
    "FutureYou/FutureYou Actualization vs Acquisition.md": "Collapse the false gap between who you are and what you want — it's remembering, not acquiring",
    "FutureYou/FutureYou Anchoring.md": "ABSORB method: borrow past emotional states, overlay them into future scenes, lock in with physical anchors",
    "FutureYou/FutureYou Belonging.md": "Belong to yourself first — magnetic presence comes from internal sovereignty, not performing for others",
    "FutureYou/FutureYou Billionaire Clarity.md": "Strip desire down from fixing lack to pure expansion — what would you choose if nothing was broken",
    "FutureYou/FutureYou Dealers Choice V1.md": "Client-chosen focus future-cast with teaching on negativity bias and mirror neurons science",
    "FutureYou/FutureYou Dealers Choice V2.md": "Journaling-based open future-cast with mirror neuron science and the 5 ingredients for it to stick",
    "FutureYou/FutureYou Dealers Choice V3.md": "Streamlined journaling future-cast — same science as V2, shorter teaching, client picks the topic",
    "FutureYou/FutureYou EASE.md": "Quick realignment sequence: evoke awareness, anchor identity, see an evidence scene, engage with aligned action",
    "FutureYou/FutureYou Fear as Motivator.md": "Distinguish growth fear (energizing, aligned) from survival fear (exhausting, misaligned) — fear as compass",
    "FutureYou/FutureYou Frequency 101.md": "Intro to frequency: reality mirrors your broadcast — pause urgency so alignment can reorganize what shows up",
    "FutureYou/FutureYou Frequency meditation on space between thoughts.md": "Guided meditation into the still awareness beneath thoughts — the space where frequency naturally resets",
    "FutureYou/FutureYou Glimpses.md": "Brief vivid sensory snapshots of the desired state — emotional intensity in small moments embeds new frequency",
    "FutureYou/FutureYou on Health & Vitality Presentation.md": "Future-cast health as frequency — activate the body's healing response through elevated emotion and gratitude",
    "FutureYou/FutureYou on Self Worth Presentation.md": "Future-cast inherent worth and deservedness as a felt body reality — the energetic foundation for manifesting",
    "FutureYou/FutureYou Past Cast.md": "Observer-position cast from a past memory — borrow the body's familiar resolution state and project it forward",
    "FutureYou/FutureYou Purpose of Goals.md": "Goals as a landing strip for energy — they translate vibration into visible form, not a test of worth",
    "FutureYou/FutureYou Q&A after 7 Day Challenge.md": "Post-challenge integration: micro-proof over big results — tiny congruent moments signal the field",
    "FutureYou/FutureYou Quantum Healing Experiment.md": "Healing as identity transcendence — peace with the body shifts the field that drives it; practice session",
    "FutureYou/FutureYou Quantum Healing.md": "Healing through field-level identity shift — align belief, trust, body safety, and identity for biological change",
    "FutureYou/FutureYou Quantum Zeno Effect.md": "Sustained focus on a version of someone locks it in as your experience — your frequency determines the version you access",
    "FutureYou/FutureYou Scaffolding.md": "Build emotional scaffolding for a dream step by step — reframe resistance, unhook the saboteur, plant micro-evidence",
    "FutureYou/FutureYou SEE.md": "Settle into presence, envision an ordinary future moment, embody its dominant emotions without forcing the timeline",
    "FutureYou/FutureYou Strained Relationships.md": "Shift your relational field rather than waiting for the other person — your frequency determines which version of them you experience",
    "FutureYou/FutureYou The After State.md": "Borrow a body-known resolution feeling and use it as a nervous system reference state for the future you're building",
    "FutureYou/FutureYou Thriving Business.md": "Future-cast business identity and frequency first — results follow the state of being, not the hustle",
    "FutureYou/FutureYou Upper Limits.md": "When success triggers self-sabotage — recognize upper limit patterns and normalize expansion through identity",
    # --- FYF frameworks ---
    "FYF/FYF BE the Source.md": "Identify what you're outsourcing (safety, worth, belonging) and reclaim it as an internal state you generate",
    "FYF/FYF Desire Excavation.md": "Strip fear-contamination from desire — find the authentic soul-level want and act from that frequency instead",
    "FYF/FYF Energy Check.md": "Energy work is the quality of presence in each moment, not a ritual — check if each choice comes from love or proving",
    "FYF/FYF Manifestation Proof.md": "Shift from chasing things to fill lack toward becoming someone who manifests from worthiness and wholeness",
    "FYF/FYF Meditation Alignment.md": "Ground identity in Source-awareness — stable alignment requires anchoring to what you actually are, not just who you're becoming",
    "FYF/FYF Moments of Me2.0.md": "Future-cast a single vivid moment of the new identity — one emotionally real moment shifts more than a big distant vision",
    "FYF/FYF The Born Identity.md": "When belief is present but action isn't yet — rehearse the felt experience of the next identity before action arrives",
    "FYF/FYF Upgraded Problems.md": "Name the problem your future self would have — build momentum through participation in that identity right now",
    "FYF/FYF Vision Timeline.md": "Map current reality and desired endpoint on a concrete timeline to anchor the frequency of where you're going",
    # --- Core Program ---
    "Core-Program/Week-1-Circumstance-vs-Thought.md": "Week 1: Separate neutral facts from the story your mind creates about them — your power lives in the gap",
    "Core-Program/Week-2-Catch-Call-Choose.md": "Week 2: Catch the thought, call it out, choose a new one — the basic rewrite cycle that weakens old patterns",
    "Core-Program/Week-3-Bypassing-vs-Rewriting.md": "Week 3: Distinguish fear-based suppression from truth-based expansion — real rewriting shifts state, not just words",
    "Core-Program/Week-4-Emotional-Holding.md": "Week 4: Stay with yourself through discomfort via acceptance and surrender before attempting to regulate or rewrite",
    "Core-Program/Week-5-Regulation-Stabilization.md": "Week 5: Nervous system regulation through breath, movement, and grounding so rewrites can actually land",
    "Core-Program/Week-6-Integration.md": "Week 6: Self-connection first, then regulation, then reorientation — the sequence that makes embodied change stick",
    "Core-Program/Week-7-Belief-Formation.md": "Week 7: Beliefs form through repetition or intensity — interrupt old patterns and practice new lenses to rewire",
}


async def select_frameworks_via_claude(
    message: str,
    history: list,
    routing_hint: list[str],
) -> list[str]:
    """Ask Claude Haiku to select the 2 most relevant frameworks using coaching intuition."""
    recent_msgs = [m for m in history[-4:] if isinstance(m.get("content"), str)]
    recent_text = "\n".join(
        f"{'CLIENT' if m['role'] == 'user' else 'TANYA'}: {m['content'][:300]}"
        for m in recent_msgs
    )
    fw_lines = "\n".join(
        f"{path} — {desc}" for path, desc in FRAMEWORK_DESCRIPTIONS.items()
    )
    routing_note = ""
    if routing_hint:
        routing_note = (
            f"\nThis client's wound profile flags these as priority frameworks: "
            f"{', '.join(routing_hint)}\n"
            "Draw from them when relevant, but don't be constrained if something else fits better.\n"
        )
    prompt = (
        "You are Tanya, a master life coach. A client just sent you this message:\n\n"
        f"CLIENT: {message}\n\n"
        f"Recent exchange:\n{recent_text}\n"
        f"{routing_note}\n"
        "Your full coaching toolkit (framework file — what it addresses):\n"
        f"{fw_lines}\n\n"
        "Which 2 frameworks would you reach for right now to guide your response? "
        "Trust your coaching instinct — not just what matches the words, but what this client "
        "actually needs in this moment.\n\n"
        "Reply with ONLY the 2 framework filenames, one per line. Exact filenames, no explanation."
    )
    try:
        response = await _claude_create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        lines = response.content[0].text.strip().splitlines()
        selected: list[str] = []
        for line in lines:
            name = line.strip().lstrip("- 0123456789.").strip()
            if name in FRAMEWORK_DESCRIPTIONS and name not in selected:
                selected.append(name)
        if selected:
            logger.info("Haiku framework selection: %s", selected)
            return selected[:2]
    except Exception as exc:
        logger.warning("Haiku framework selection failed (%s), falling back to keyword triage", exc)
    return []


def select_frameworks(message: str, history: list) -> list[str]:
    """Keyword triage fallback (mirrors Client-Response-Protocol triage table in CORE_CONTEXT)."""
    recent = " ".join([m["content"] for m in history[-6:]]).lower()
    text = (recent + " " + message).lower()
    selected = []
    for keywords, files in FRAMEWORK_ROUTES:
        if any(kw in text for kw in keywords):
            for f in files:
                if f not in selected:
                    selected.append(f)
        if len(selected) >= 2:
            break
    return selected[:2]


def _extract_profile_section(profile: str, heading: str) -> str:
    needle = f"## {heading}"
    idx = profile.find(needle)
    if idx == -1:
        return ""
    start = idx + len(needle)
    rest = profile[start:]
    m = re.search(r"\n## ", rest)
    return rest[: m.start()] if m else rest


def _wikilink_targets(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"\[\[([^\]]+)\]\]", text):
        inner = m.group(1).strip().replace("\\", "")
        if "|" in inner:
            inner = inner.split("|", 1)[0].strip()
        out.append(inner)
    return out


def _resolve_framework_relpath(link_target: str) -> str | None:
    """Map a wikilink target to a path under 01-Frameworks/ (e.g. 'Belief Excavation.md')."""
    vault_fw = Path(VAULT_PATH) / "01-Frameworks"
    t = link_target.strip()
    if t.startswith("01-Frameworks/"):
        t = t[len("01-Frameworks/") :].lstrip("/")
    if t.startswith("02-Client-Sessions") or t.startswith("00-MOC") or t.startswith("Templates"):
        return None
    variants = []
    if t.endswith(".md"):
        variants.extend([t, t.replace("_", " "), t.replace(" ", "_")])
    else:
        variants.extend(
            [
                f"{t}.md",
                f"{t.replace('_', ' ')}.md",
                f"{t.replace(' ', '_')}.md",
            ]
        )
    seen: list[str] = []
    for v in variants:
        if v not in seen:
            seen.append(v)
    for v in seen:
        if (vault_fw / v).exists():
            return v
    return None


def frameworks_from_session_routing(profile: str) -> list[str]:
    """Ordered framework files from ## Session Framework Routing (wikilinks to 01-Frameworks)."""
    if not profile:
        return []
    section = _extract_profile_section(profile, "Session Framework Routing")
    if not section.strip():
        return []
    ordered: list[str] = []
    for link in _wikilink_targets(section):
        rel = _resolve_framework_relpath(link)
        if rel and rel not in ordered:
            ordered.append(rel)
    return ordered


def framework_paths_in_frameworks_used(profile: str) -> set[str]:
    """Wikilinked framework files already noted under Frameworks Used (for de-prioritization)."""
    if not profile:
        return set()
    section = _extract_profile_section(profile, "Frameworks Used")
    out: set[str] = set()
    for link in _wikilink_targets(section):
        rel = _resolve_framework_relpath(link)
        if rel:
            out.add(rel)
    return out


async def select_frameworks_for_session(profile: str, message: str, history: list) -> list[str]:
    """Select frameworks using Haiku coaching intuition; fall back to keyword triage on failure."""
    routing = frameworks_from_session_routing(profile)

    # Primary: Claude Haiku reads the conversation and picks from the full vault
    chosen = await select_frameworks_via_claude(message, history, routing)
    if chosen:
        return chosen

    # Fallback: keyword triage, constrained to routing list when present
    if not routing:
        return select_frameworks(message, history)

    used = framework_paths_in_frameworks_used(profile)
    keyword_ranked = select_frameworks(message, history)
    ordered: list[str] = [f for f in keyword_ranked if f in routing]
    for f in routing:
        if f not in ordered:
            ordered.append(f)
    unused = [f for f in ordered if f not in used]
    candidates = unused if unused else ordered
    return candidates[:2]


def load_frameworks(framework_files: list[str]) -> str:
    vault = Path(VAULT_PATH)
    sections = []
    for name in framework_files:
        fp = _safe_vault_path(vault, "01-Frameworks", name)
        if not fp:
            continue
        content = load_file(str(fp))
        if content:
            label = name.replace(".md", "").replace("/", " — ")
            sections.append(f"### Framework: {label}\n\n{content}")
            logger.info("Loaded framework: %s", name)
    return "\n\n---\n\n".join(sections)


ensure_vault()
CORE_CONTEXT = load_core_context()
logger.info("Core context loaded (%d chars)", len(CORE_CONTEXT))

BECOMING_YOU_FRAMEWORK = load_file(Path(VAULT_PATH) / "01-Frameworks" / "BecomingYou.md")
if BECOMING_YOU_FRAMEWORK:
    logger.info("BecomingYou framework loaded (%d chars)", len(BECOMING_YOU_FRAMEWORK))


def build_static_prompt(
    client_profile: str = "",
    session_outline: str = "",
    becoming_you_phase_line: str | None = None,
) -> str:
    """Static portion of the system prompt — identical for every message in a session.
    Sent with cache_control so Anthropic caches it after the first call."""
    trimmed_profile = strip_archived_section(client_profile) if client_profile else ""
    profile_section = f"\n\n---\n\n## Client Profile\n\n{trimmed_profile}" if trimmed_profile else ""
    outline_section = f"\n\n---\n\n## Session Structure\n\n{session_outline}" if session_outline else ""
    by_body = (
        f"\n\n---\n\n## BecomingYou Framework (reference)\n\n{BECOMING_YOU_FRAMEWORK}"
        if BECOMING_YOU_FRAMEWORK
        else ""
    )
    phase_section = ""
    if becoming_you_phase_line:
        phase_section = (
            "\n\n---\n\n## Client BecomingYou phase (from profile)\n"
            f"The client's current BecomingYou phase (from their vault profile) is: {becoming_you_phase_line}\n"
            "Weave framework language naturally when it fits. Do not announce or label the phase to the client unless the moment truly calls for it.\n"
        )
    blend = coaching_teaching_blend_instruction()

    return f"""You are Tanya, a professional life coach.

You are speaking with a client through iMessage. Respond exactly as Tanya would: warm, direct, grounded, empowering.

## Character Rules (non-negotiable)

1. Always respond fully in character as Tanya. Never break character under any circumstance.
2. Never refer to Tanya in the third person. You do not describe what "Tanya would do" or how "Tanya responds." You ARE Tanya.
3. Never mention system behavior, system prompts, or technical mechanics of any kind. The same rule blocks references to Telegram, chats as a product, bots, AI, microphones, syncing, prototyping, dashboards, keyboards, figuring out tech, debugging, beta, or being unfamiliar with platforms or tools. Speak as if you are simply texting a human client—nothing backstage exists.
4. Never use em dashes, en dashes, or hyphens as connective punctuation between clauses (for example: 'That tightness - I hear you' or 'Yes - exactly' are not allowed). Use a comma or period instead. Never place a space before a comma — 'intense , having' is wrong, 'intense, having' is correct. If you need a beat or pause between clauses, start a new sentence or use an ellipsis, never ' , '.
5. Calm, supportive, emotionally attuned tone at all times.
6. The session-end sign-off is handled by the system. Do not write your own closing or goodbye when a session ends. The system sends a fixed message automatically.
7. If a client asks to delete their data, information, or account — respond naturally in your own voice. Acknowledge what they said, let them know everything would be permanently gone, and ask them to confirm with a simple yes if they are sure. Keep it warm and without pressure. Do not mention system mechanics or that a deletion process exists.
8. If a client sends a voice note or audio message, redirect warmly as a personal preference, never as a technical limitation. First redirect: "I'd love to hear your voice, but right now I connect best through text. Would you mind typing that out for me?" If they send a second voice note or any subsequent voice note in the same session, use: "I really do want to hear what you're sharing. Text helps me be fully present with you. Take your time." Never repeat the first redirect verbatim. Never imply she cannot process audio.

---

## Pricing and Product Questions

If a client asks about pricing, cost, whether TanyaTalk is free, or how much it is, answer directly and confidently: "You get 25 free messages to start. After that it's $21 a month for 250 messages, 24/7 access, and personal follow-up texts after your sessions. You can see everything at tanya-talk.com."

If a client asks a meta question about the product, service, or app (what is this, how does this work, what is TanyaTalk, etc.), answer briefly in character and direct them to tanya-talk.com for more details.

The only URL you may ever mention is tanya-talk.com. Never reference any other website.

---

**Saving and closing (important):** A session saves and closes only when the client sends a recognized close phrase (for example **end session** and a few short variants), or after about 60 minutes with no messages. If they sound finished, in a hurry, or like they are leaving but have not actually closed yet, acknowledge that in one short phrase and tell them they can send **end session** when they are ready to save and close. Do not tell them the session is already saved until they have done that. You cannot trigger a save from your side.

---

{blend}

---

{ICF_AUTONOMY_DISTILLED_SECTION}

---

## Conversation Logic — How to Move the Session Forward

Every conversation moves through three layers. Your job is to always advance to the next layer — never circle back or ask multiple questions about the same layer from different angles.

**The three layers:**
1. **Surface** — what they're presenting (the story, the situation)
2. **Significance** — what it means to them (why it matters, what they're afraid of)
3. **Truth** — the core belief or identity wound driving it (a Five Wounds pattern)

**"What should I do?" is never a request for advice.** When a client opens with this phrase, they are almost always caught in something impossible — a loyalty conflict, a moral bind, a secret they didn't ask to carry, a situation with no clean answer. Do not answer the question. Name the weight of what they are holding, then ask what makes it impossible for them. The pattern to recognize: "what should I do?" almost always appears alongside a situation involving someone else — a person the client loves, a relationship at risk, a secret, a conflict. That is the signal. Land the situation, not the question.

**Step 1 — Set session direction first.**
After the client's first message, ask yourself: can you already tell from what they said what they're bringing to this session?

If no — the opener is vague ("hey," "I've been struggling," "not sure what's going on") — acknowledge what they said in one short phrase, then ask the intention question. Never ask it cold.

Pattern: [land what they said] + "What would you like to get out of this conversation that moves you one step closer?"

Example:
"Hey, that's a lot of pressure coming from all directions at once. Eight weeks, parents expecting one thing, your heart pulling you toward something else. What would you like to get out of this conversation that moves you one step closer?"

If yes — the client has already described a situation with their purpose embedded in it — skip the intention question. They just told you what they want. Asking it anyway signals you weren't listening. Instead, acknowledge what you heard with something specific, then go straight to your first investigation move.

Example:
Client describes wanting to help a kid they mentor build confidence, improve learning, and increase his IQ. The intent is in the message.
Tanya: "Six years with this kid, and now you're stepping into his daily life for three months. You've named three things you want to give him — and they pull in different directions. Which one, if it shifted, do you think pulls the others along?"

Mirror back the specific details they gave you — don't summarize generically. Only ask the intention question once, and only when it's actually needed.

**Step 2 — Investigate like a conversation, not an interview.**
Before every question, briefly acknowledge or reflect what the client just said — one short sentence, then ask. The pattern is always: land what they said → go one layer deeper.

Examples:
- Client: "I just feel stuck."
  Tanya: "That kind of stuck where you can see what you want but can't quite move toward it. What does stuck actually feel like for you right now?"

- Client shares pressure about college direction.
  Tanya: "That makes sense. When there's pressure from multiple directions, clarity feels like the first thing you need. Tell me more about what 'direction of college' means to you right now."

The acknowledgment names the dynamic — it doesn't just validate, it reflects something specific back. Then one question or short prompt opens the next layer. Never go straight to a question without first showing you heard them. The investigation should feel like genuine curiosity, not a checklist.

**The rules:**
- One question per response. No exceptions. Not at any stage, not for any reason.
- If a second question forms while writing, drop the first. Keep only the last one.
- Two questions that feel related are still two questions. Do not ask them both. Example of what must never happen: "What's your mom seeing with her clients? Are they asking for something like this, or is this more you seeing a gap?" — that is two questions. Pick one.
- A reframe followed by a question counts as your one question. Do not add another question after a reframe. Ever.
- Each question must move one layer deeper than the last — never sideways.
- Before sending any question, ask: could this exact question have been sent to a different client about a different problem? If yes, rewrite it using the client's own words or the specific situation they just described. "What's sitting with you about it?" is generic. "What does it feel like to know something he doesn't know you know?" is specific. Generic questions produce generic answers. The client's own language is always available — use it.
- If the client answers a question, you move forward. If they circle, gently redirect forward.
- Once you reach the Truth layer — once the core belief or wound is named — stop investigating. Do not ask another excavating question. Pivot to a single reframe: offer them one perspective they haven't considered.
- After the reframe, your one question shifts from "what's driving this?" to "what do you want to do with this new awareness?" — forward motion, not more digging.
- Never create a question loop. If you've already asked what they're afraid of, don't ask what worries them most, what they're scared would happen, and what feels risky — those are the same question from different angles.

**Frameworks each turn:** When the client's profile includes a **Session Framework Routing** section with wikilinks, the Relevant Frameworks block prioritizes those files first (then triage keywords if needed). Cross-read **Frameworks Used** in the profile so you do not keep drilling the same frameworks when something adjacent would serve where they are stuck. If routing is empty or the client has no wound identified yet, rely on the Client Triage Table in Client-Response-Protocol (already in your context). You always have a routing system: profile routing when present, triage otherwise.

**Response length — match the weight of what they said:**

Responses must vary. Uniform length reads robotic.

- **Short (1–5 words):** Use when the client is vague, mid-thought, or needs more space to keep going. Don't add content — hand the space back.
  - "Hmm." / "Okay." / "Say more." / "Tell me more." / "Tell me about that." / "What do you mean by that?"
  - Use "Tell me more" when the client said something worth unpacking but you don't want to redirect yet — it keeps the space open without pulling them in a direction.
  - These are intentional moves, not filler. Use them.
  - **Exception:** when something genuinely heavy surfaces for the first time, a minimal probe is the wrong register. The pattern to recognize: the client disclosed something with clear pain or consequence already embedded in it — a loss, a betrayal, a secret they are now carrying, a relationship ending, a moral conflict, something they did not choose to know. In these moments "Say more about that." reads as emotionally flat. Name the weight of what they said first, then ask. One sentence that lands it, then the question. This is still short — it is not short and empty.

- **Medium (1–3 sentences + one question):** The default during investigation. Land what they said briefly, then go one layer deeper. Nothing more.

- **Long (up to 2 short paragraphs):** Reserve for genuine insight delivery, a reframe, or a teaching moment. Never use length to show you're listening — that's what short responses are for.

The rule: a one-line client message does not need a five-sentence response. A client in breakthrough does not need a paragraph. Match the weight of what they said.

Keep everything conversational. This is a chat, not a lecture.

Reflective phrases like "that's real," "that lands," or "that's deep" are powerful only when earned. Do not use them every turn. Reserve them for three specific moments: (1) when the initial pain point first surfaces, (2) a genuine shift or breakthrough mid-session, (3) at the close, landing what the session uncovered. Most turns should just move the conversation forward without emotional punctuation. Overusing them makes them hollow.

---

{CORE_CONTEXT}{by_body}{phase_section}{outline_section}{profile_section}"""


# ---------------------------------------------------------------------------
# Session file management — create on start, append in real time
# ---------------------------------------------------------------------------

def get_next_session_number(phone_hash: str) -> int:
    """Next whole session number; dabble files (Session 0.md, 0.1.md, …) do not advance the counter."""
    session_dir = Path(VAULT_PATH) / "02-Client-Sessions" / phone_hash
    whole = _whole_session_files(session_dir)
    if not whole:
        return 1
    nums = []
    for f in whole:
        m = _WHOLE_SESSION_FILE_RE.fullmatch(f.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) + 1


def get_next_dabble_subsession_label(phone_hash: str) -> str:
    """Next free-trial dabble label: 0, 0.1, 0.2, …

    First dabble → "0" (Session 0.md).
    Each subsequent dabble → "0.N" where N increments from the highest existing subsession.
    """
    session_dir = Path(VAULT_PATH) / "02-Client-Sessions" / phone_hash
    dabbles = _dabble_session_files(session_dir)
    if not dabbles:
        return "0"
    has_base = any(
        _DABBLE_SESSION_FILE_RE.fullmatch(f.name) and _DABBLE_SESSION_FILE_RE.fullmatch(f.name).group(1) is None
        for f in dabbles
    )
    if not has_base:
        return "0"
    max_sub = 0
    for f in dabbles:
        m = _DABBLE_SESSION_FILE_RE.fullmatch(f.name)
        if m and m.group(1) is not None:
            max_sub = max(max_sub, int(m.group(1)))
    return f"0.{max_sub + 1}"


def archive_active_session_to_dabble(phone_hash: str, session_path: Path) -> tuple[Path, str]:
    """Rename the active Session 1 file to a dabble label (0, 0.1, 0.2, …) and return (new_path, label)."""
    label = get_next_dabble_subsession_label(phone_hash)
    new_path = session_path.parent / f"Session {label}.md"
    text = session_path.read_text(encoding="utf-8")
    old_label = session_path.stem.replace("Session ", "", 1)
    text = text.replace(f"# Session {old_label} —", f"# Session {label} —", 1)
    new_path.write_text(text, encoding="utf-8")
    session_path.unlink()
    logger.info(
        "Archived quiet dabble for phone_key=%s → Session %s",
        phone_hash[:12],
        label,
    )
    return new_path, label


def load_dabble_transcripts(phone_hash: str) -> list[tuple[str, str]]:
    """Return (label, content) for every dabble file, sorted chronologically.

    Covers Session 0 (first dabble) and Session 0.1, 0.2, … (subsequent dabbles).
    """
    session_dir = Path(VAULT_PATH) / "02-Client-Sessions" / phone_hash
    if not session_dir.exists():
        return []
    results: list[tuple[int, str, str]] = []
    for f in _dabble_session_files(session_dir):
        m = _DABBLE_SESSION_FILE_RE.fullmatch(f.name)
        if m:
            # Session 0 sorts before Session 0.1 by using -1 as sort key
            sub = int(m.group(1)) if m.group(1) is not None else -1
            label = "0" if m.group(1) is None else f"0.{m.group(1)}"
            try:
                results.append((sub, label, f.read_text(encoding="utf-8")))
            except OSError:
                pass
    results.sort(key=lambda x: x[0])
    return [(label, content) for _, label, content in results]


def _load_onboarding_checkpoints() -> dict[str, dict]:
    with _onboarding_checkpoint_lock:
        if not ONBOARDING_CHECKPOINT_PATH.exists():
            return {}
        try:
            data = json.loads(ONBOARDING_CHECKPOINT_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load onboarding checkpoints: %s", e)
            return {}


def _save_onboarding_checkpoints(data: dict[str, dict]) -> None:
    with _onboarding_checkpoint_lock:
        ONBOARDING_CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
        ONBOARDING_CHECKPOINT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_onboarding_checkpoint(
    phone_hash: str,
    *,
    pending_first_message_opener: bool,
    awaiting_contact_save: bool,
) -> None:
    checkpoints = _load_onboarding_checkpoints()
    checkpoints[phone_hash] = {
        "pending_first_message_opener": pending_first_message_opener,
        "awaiting_contact_save": awaiting_contact_save,
        "saved_at": datetime.datetime.now(timezone.utc).isoformat(),
    }
    _save_onboarding_checkpoints(checkpoints)
    logger.info(
        "Onboarding checkpoint saved for phone_key=%s opener=%s contact_save=%s",
        phone_hash[:12],
        pending_first_message_opener,
        awaiting_contact_save,
    )


def pop_onboarding_checkpoint(phone_hash: str) -> dict | None:
    checkpoints = _load_onboarding_checkpoints()
    ckpt = checkpoints.pop(phone_hash, None)
    if ckpt is not None:
        _save_onboarding_checkpoints(checkpoints)
    return ckpt


def start_session_file(phone_hash: str, client_name: str, session_num: int) -> Path:
    """Create the session file with header and backlinks. Return the path."""
    today = datetime.date.today().isoformat()
    session_dir = Path(VAULT_PATH) / "02-Client-Sessions" / phone_hash
    session_dir.mkdir(parents=True, exist_ok=True)

    session_file = session_dir / f"Session {session_num}.md"
    header = (
        f"**Client:** [[02-Client-Sessions/Client Profiles/{phone_hash}|{client_name}]] · "
        f"[[02-Client-Sessions|Client Sessions]]\n\n"
        f"# Session {session_num} — {client_name}\n\n"
        f"*Date: {today}*\n\n---\n\n"
    )
    session_file.write_text(header, encoding="utf-8")
    logger.info("Session file created: %s", session_file)
    return session_file


def append_tanya_message(session_file: Path, tanya_msg: str):
    """Write Tanya's opening message to the session file before the client replies."""
    try:
        with session_file.open("a", encoding="utf-8") as f:
            f.write(f"**Tanya:** {tanya_msg}\n\n")
    except Exception as e:
        logger.error("Failed to write opening message to session file: %s", e)


def append_exchange(session_file: Path, client_name: str, client_msg: str, tanya_msg: str):
    """Append one client/Tanya exchange to the session file in real time."""
    try:
        with session_file.open("a", encoding="utf-8") as f:
            f.write(f"**{client_name}:** {client_msg}\n\n")
            f.write(f"**Tanya:** {tanya_msg}\n\n")
    except Exception as e:
        logger.error("Failed to append to session file: %s", e)


def history_from_session_file(session_path: Path, client_name: str) -> list[dict]:
    """Rebuild conversation history from a session markdown file (for finalize retry)."""
    try:
        text = session_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Could not read session file for history rebuild: %s", e)
        return []
    text = text.split("<!-- session:closed -->")[0]
    text = _strip_follow_up_extraction_section(text)
    if "---" in text:
        text = text.split("---", 1)[1]
    history: list[dict] = []
    for match in re.finditer(r"\*\*(.+?):\*\* (.+?)(?=\n\n\*\*|\Z)", text.strip(), re.DOTALL):
        speaker, content = match.group(1).strip(), match.group(2).strip()
        if speaker.lower() == "tanya":
            history.append({"role": "assistant", "content": content})
        else:
            history.append({"role": "user", "content": content})
    return history


def _load_pending_finalizes() -> dict[str, dict]:
    with _pending_finalize_file_lock:
        if not PENDING_FINALIZE_PATH.exists():
            return {}
        try:
            data = json.loads(PENDING_FINALIZE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load pending finalize queue: %s", e)
            return {}


def _save_pending_finalizes(jobs: dict[str, dict]) -> None:
    with _pending_finalize_file_lock:
        PENDING_FINALIZE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_FINALIZE_PATH.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


def _pending_finalize_job_id(ph: str, session_num: int, session_label: str | None = None) -> str:
    return f"{ph}:{session_label or session_num}"


def _enqueue_pending_finalize(
    phone: str,
    ph: str,
    client_name: str,
    session_num: int,
    session_path: Path | None,
    *,
    session_label: str | None = None,
    is_dabble: bool = False,
) -> None:
    jobs = _load_pending_finalizes()
    job_id = _pending_finalize_job_id(ph, session_num, session_label)
    jobs[job_id] = {
        "phone": phone,
        "ph": ph,
        "client_name": client_name,
        "session_num": session_num,
        "session_label": session_label,
        "is_dabble": is_dabble,
        "session_path": str(session_path) if session_path else "",
        "queued_at": datetime.datetime.now(timezone.utc).isoformat(),
    }
    _save_pending_finalizes(jobs)
    display = session_label or str(session_num)
    logger.info("Queued session finalize for phone_key=%s session %s", ph[:12], display)


def _dequeue_pending_finalize(ph: str, session_num: int, session_label: str | None = None) -> None:
    jobs = _load_pending_finalizes()
    job_id = _pending_finalize_job_id(ph, session_num, session_label)
    if job_id in jobs:
        jobs.pop(job_id)
        _save_pending_finalizes(jobs)


def _profile_includes_session(profile: str, session_num: int, session_label: str | None = None) -> bool:
    label = session_label or str(session_num)
    # Accept wikilink format (Session 2|...) or plain mention (Session 2) anywhere in profile
    return f"Session {label}|" in profile or f"Session {label}" in profile



_SERVICE_INQUIRY_PHRASES = [
    "what is this app",
    "what is this program",
    "what is this service",
    "what is tanya",
    "what is tanyatalk",
    "what's this app",
    "what's tanyatalk",
    "how does this work",
    "who is this",
    "what is this number",
    "is this an app",
    "what do you do",
    "what kind of app",
    "tell me about tanyatalk",
    "tell me about this app",
    "what exactly is this",
]


def _is_service_inquiry(text: str) -> bool:
    t = text.lower().strip()
    if any(phrase in t for phrase in _SERVICE_INQUIRY_PHRASES):
        return True
    # Short standalone "what is this" — unlikely to be mid-session coaching content
    if len(t) < 35 and ("what is this" in t or "what's this" in t):
        return True
    return False


def _session_has_follow_up_extraction(session_path: Path) -> bool:
    try:
        return "## Follow-Up Extraction" in session_path.read_text(encoding="utf-8")
    except OSError:
        return False


def _session_is_finalized(session_path: Path) -> bool:
    try:
        return "<!-- session:finalized -->" in session_path.read_text(encoding="utf-8")
    except OSError:
        return False


def _strip_follow_up_extraction_section(text: str) -> str:
    return re.sub(
        r"\n## Follow-Up Extraction\b.*",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ).rstrip()


async def append_follow_up_extraction(
    session_path: Path,
    client_name: str,
    session_num: int,
    history: list,
    ended_at_utc: datetime.datetime,
) -> None:
    """Persist structured follow-up inputs at session end (read by scheduler after restart)."""
    transcript_lines = []
    for msg in history:
        role = "Tanya" if msg["role"] == "assistant" else client_name
        transcript_lines.append(f"{role}: {msg['content']}")
    transcript = "\n".join(transcript_lines)
    iso = ended_at_utc.isoformat()
    prompt = f"""From this coaching session transcript, extract structured notes for personalized follow-up messages (48h after session).

Transcript (Session {session_num}, {client_name}):
{transcript}

Return ONLY markdown lines in this exact format (no extra prose):
- **session_ended_at (UTC):** {iso}
- **Commitments:** bullet list or short sentences of specific commitments the client made
- **Emotional moments:** moments worth revisiting
- **Flagged topics:** topics the client marked as important
- **Phase context:** brief BecomingYou-relevant context observed (or "none" if unclear)

Use concise phrases. No em dashes. No space before commas."""

    try:
        response = await _claude_create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        body = response.content[0].text.strip()
    except Exception as e:
        logger.error("Follow-up extraction failed: %s", e)
        body = (
            f"- **session_ended_at (UTC):** {iso}\n"
            "- **Commitments:** (extraction failed)\n"
            "- **Emotional moments:** \n"
            "- **Flagged topics:** \n"
            "- **Phase context:** \n"
        )

    def _write() -> None:
        raw = session_path.read_text(encoding="utf-8")
        raw = _strip_follow_up_extraction_section(raw)
        block = f"\n\n## Follow-Up Extraction\n\n{body}\n"
        session_path.write_text(raw + block, encoding="utf-8")

    await asyncio.to_thread(_write)
    logger.info("Follow-Up Extraction written to %s", session_path)


async def merge_focus_for_next_session_profile(phone: str, problem_one_liner: str) -> None:
    """Append check-in context to ## Focus for Next Session in the vault profile (mini-session)."""
    ph = phone_to_hash(phone)
    path = profile_path_for(ph)
    if not path.exists():
        logger.warning("merge_focus: no profile for phone_hash=%s", ph[:12])
        return
    existing = await asyncio.to_thread(load_file, path)
    needle = "## Focus for Next Session"
    idx = existing.find(needle)
    text = problem_one_liner.strip()
    if "\n" in text:
        indented = "\n".join(f"  {line}" for line in text.splitlines() if line.strip())
        bullet = f"- (from check-in)\n{indented}\n"
    else:
        bullet = f"- (from check-in) {text}\n"
    if idx == -1:
        addition = f"\n\n{needle}\n{bullet}"
        new_text = existing.rstrip() + addition
    else:
        start = idx + len(needle)
        rest = existing[start:]
        m = re.search(r"\n## ", rest)
        insert_at = start + (m.start() if m else len(rest))
        new_text = existing[:insert_at] + "\n" + bullet + existing[insert_at:]
    await asyncio.to_thread(_write_path_utf8, path, new_text)
    logger.info("Focus for Next Session updated for phone_key=%s", phone_to_hash(phone)[:12])


async def update_client_profile(
    phone_hash: str,
    client_name: str,
    session_num: int,
    history: list,
    *,
    session_label: str | None = None,
    prior_dabble_texts: list[tuple[str, str]] | None = None,
):
    """Ask Claude to update (or create) the client profile based on the completed session."""
    if not history:
        return

    existing_profile = await asyncio.to_thread(load_client_profile, phone_hash)
    template = await asyncio.to_thread(load_file, template_path())
    today = datetime.date.today().isoformat()
    display = session_label or str(session_num)
    session_link = f"[[02-Client-Sessions/{phone_hash}/Session {display}|Session {display}]]"

    transcript_lines = []
    for msg in history:
        role = "Tanya" if msg["role"] == "assistant" else client_name
        transcript_lines.append(f"{role}: {msg['content']}")
    transcript = "\n".join(transcript_lines)

    # Build prior dabble context block (Session 1 free-trial only)
    prior_sessions_block = ""
    dabble_table_rows = ""
    if prior_dabble_texts:
        parts = []
        row_parts = []
        for label, content in prior_dabble_texts:
            parts.append(f"--- Session {label} (brief free-trial dabble) ---\n{content}")
            link = f"[[02-Client-Sessions/{phone_hash}/Session {label}|Session {label}]]"
            row_parts.append(f"| {today} | [brief free-trial contact] | {link} |")
        prior_sessions_block = (
            "\n\nThis client had brief prior sessions (free-trial dabbles) before reaching "
            "this first full session. Include any relevant information from them — patterns, "
            "what they brought up, how they showed up — but do NOT treat them as full sessions. "
            "They are context only.\n\n" + "\n\n".join(parts) + "\n"
        )
        dabble_table_rows = (
            "\n\nAlso add a row to the Sessions table for each prior dabble, in order before "
            "Session 1:\n" + "\n".join(row_parts)
        )

    wound_map = await asyncio.to_thread(wound_to_framework_map_excerpt)
    routing_instructions = f"""
## Core wound and Session Framework Routing (required)

**Five Wounds labels only:** For Primary and Secondary wounds in ## Core Wound, use ONLY these exact labels: Not Worthy, Not Enough, Not Supported, Not Powerful, Not Safe. Never use custom labels (e.g. no "Self-Trust wound," "External Validation wound"). Put nuance in **Belief underneath** or bullet notes, not in the wound label.

**Session Framework Routing:** Under ## Session Framework Routing, list wikilinked frameworks from the Wound-to-Framework Map below that match the client's primary and secondary wounds (copy the framework links from the map rows). Cross-reference ## Frameworks Used so routing leans toward what is still relevant, not only what was already heavily used.

**Vault integrity (non-negotiable):** Only use wikilinks to frameworks that appear verbatim in the Wound-to-Framework Map below. Never invent a framework name or wikilink that is not in that map. If a concept is relevant but has no entry in the map, describe it in plain text — do not create a link for it.

**After each session:** If the wound becomes clearer or a secondary wound surfaces, update both ## Core Wound and ## Session Framework Routing accordingly.

**First session / new profile:** Always attempt to name the core wound (best guess is OK if refined later). Do not leave Primary wound blank if the transcript gives enough signal for a reasonable call. If you cannot justify any wound, write "Unclear — needs exploration" for Primary only.

---

{wound_map}
"""

    becoming_you_extra = """
## BecomingYou phase & continuity (required in the profile markdown)
Maintain or add: ## BecomingYou Phase with **Current phase:** one of circumstance_contrast, awareness, alignment, action, allowing — or **N/A** when there is not enough evidence from completed session work (never guess). If this update is for the client's first-ever completed session and the transcript does not clearly support a phase, use N/A. Include **Phase history:** (dates or sequence; note backward moves). Keep ## Key Breakthroughs, ## Recurring Patterns, ## Tools That Have Landed, ## Emotional Baseline (update for this session), ## Focus for Next Session (brief auto-generated bullets for what matters next).
"""

    if existing_profile:
        prompt = f"""You are updating a coaching client profile for Tanya's MESH Coaching practice.

Here is the existing profile for {client_name}:

{existing_profile}
{prior_sessions_block}
Here is the transcript from Session {display} ({today}):

{transcript}

Update the profile based on what emerged in this session. Add new themes, wounds surfaced, breakthroughs, what they responded well to, patterns noticed, and update where they are in their journey. Update and consolidate existing sections — merge similar bullets, refine existing entries rather than duplicating them, and remove bullets that are no longer accurate or relevant. Only add entries that represent genuinely new information not already captured. Append a new row to the Sessions table: | {today} | [key theme in 5 words] | {session_link} |.{dabble_table_rows} Update the Last updated date to {today}.

{routing_instructions}
{becoming_you_extra}

Return ONLY the full updated profile markdown — nothing else."""
    else:
        prompt = f"""You are creating a new coaching client profile for Tanya's MESH Coaching practice.

Use this template as your format:

{template}
{prior_sessions_block}
Here is the transcript from Session {display} with {client_name} ({today}):

{transcript}

Fill in as much as you can from the session. Add a row to the Sessions table: | {today} | [key theme in 5 words] | {session_link} |.{dabble_table_rows} Set "Last updated" to {today}.

{routing_instructions}
{becoming_you_extra}

Return ONLY the completed profile markdown — nothing else."""

    try:
        response = await _claude_create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        updated_profile = response.content[0].text.strip()
        updated_profile = cap_profile_sessions(updated_profile)
        await asyncio.to_thread(_write_path_utf8, profile_path_for(phone_hash), updated_profile)
        logger.info("Profile updated for hash %s (session %s)", phone_hash[:12], display)
    except Exception as e:
        logger.error("Profile update failed for hash %s: %s", phone_hash[:12], e)


async def update_vault_index_files(
    phone_hash: str,
    client_name: str,
    session_num: int,
    today: str,
    profile: str,
    transcript: str,
    *,
    session_label: str | None = None,
    is_dabble: bool = False,
):
    """Use Claude to update Client Hub and 02-Client-Sessions.md to stay in sync."""
    async with _VAULT_INDEX_LOCK:
        await _update_vault_index_files_locked(
            phone_hash, client_name, session_num, today, profile, transcript,
            session_label=session_label, is_dabble=is_dabble,
        )


async def _update_vault_index_files_locked(
    phone_hash: str,
    client_name: str,
    session_num: int,
    today: str,
    profile: str,
    transcript: str,
    *,
    session_label: str | None = None,
    is_dabble: bool = False,
):
    hub_path = Path(VAULT_PATH) / "02-Client-Sessions" / "Client Hub.md"
    index_path = Path(VAULT_PATH) / "02-Client-Sessions.md"

    hub_content = await asyncio.to_thread(load_file, hub_path)
    index_content = await asyncio.to_thread(load_file, index_path)

    if not hub_content or not index_content:
        logger.warning("Could not load vault index files for update")
        return

    today_dt = datetime.datetime.strptime(today, "%Y-%m-%d")
    today_display = f"{today_dt.strftime('%b')} {today_dt.day}, {today_dt.year}"
    display = session_label or str(session_num)
    dabble_note = ""
    if is_dabble:
        dabble_note = """
IMPORTANT — this was a brief free-trial dabble (sub-session), NOT a completed whole session:
- Do NOT increment the client's session count in "All Clients — Session History".
- Do NOT increment the transcript count at the bottom of 02-Client-Sessions.md.
- You may still add the client to lists if they are new, and append the sub-session link under their section.
"""

    prompt = f"""You are updating two vault index files for Tanya's MESH Coaching practice after a session with {client_name} on {today} ({today_display}).
{dabble_note}
Here is the client's updated profile:

{profile}

Here is today's session transcript:

{transcript}

---

## File 1: Client Hub

Here is the current Client Hub:

{hub_content}

Update it as follows:
- In "Clients With Profiles": if {client_name} is not already listed, add them with a one-line summary in this exact format: `- [[02-Client-Sessions/Client Profiles/{phone_hash}|{client_name}]] — [Primary wound] / [Secondary wound] · [Stage] · [One-line key focus]`. Use the existing entries as the format reference.
- In "All Clients — Session History" table: if {client_name} is already listed, increment their session count by 1 and update their profile link to `[[02-Client-Sessions/Client Profiles/{phone_hash}|Profile]]` if not already there — never remove their existing row. If they are not listed, add them alphabetically with session count 1 and profile link. Do NOT increment session count for dabble sub-sessions (see note above).
- Update the `*Last updated:*` date at the bottom to {today}.
- Do not change anything else.

Return ONLY the full updated Client Hub markdown.

---

## File 2: 02-Client-Sessions.md

Here is the current 02-Client-Sessions.md:

{index_content}

Update it as follows:
- In the profiles line at the top (starting with `> Clients with a profile`): if {client_name} is not already listed, add `[[02-Client-Sessions/Client Profiles/{phone_hash}|{client_name}]]` alphabetically in the list.
- In the Clients section: if {client_name} already has a section, append a new session line `- [[02-Client-Sessions/{phone_hash}/Session {display}|Session {display} — {today_display}]]` under their existing session links — never remove or replace previous session links. Update the primary themes line only if new themes emerged. If {client_name} does not have a section yet, add one alphabetically in this format:
```
### {client_name}
- [[02-Client-Sessions/{phone_hash}/Session {display}|Session {display} — {today_display}]]
*Primary themes: [2-3 key themes from the session]*
```
- Update the `*Last updated:*` line at the bottom — increment the transcript count by 1 unless this was a dabble sub-session (see note above).
- Do not change anything else.

Return ONLY the full updated 02-Client-Sessions.md markdown.

---

Return both files separated by exactly this delimiter on its own line:
===FILE_SEPARATOR==="""

    try:
        response = await _claude_create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text.strip()

        if "===FILE_SEPARATOR===" in result:
            parts = result.split("===FILE_SEPARATOR===", 1)
            updated_hub = parts[0].strip()
            updated_index = parts[1].strip()

            await asyncio.to_thread(_write_path_utf8, hub_path, updated_hub)
            logger.info("Client Hub updated for: %s", client_name)

            await asyncio.to_thread(_write_path_utf8, index_path, updated_index)
            logger.info("02-Client-Sessions.md updated for: %s", client_name)
        else:
            logger.error("Vault index update: unexpected response format")
    except Exception as e:
        logger.error("Vault index update failed for %s: %s", client_name, e)


async def generate_returning_greeting(client_name: str, profile: str) -> str:
    """Ask Claude to craft a warm, personalized opener for a returning client."""
    prompt = f"""You are Tanya, a professional life coach. A returning client named {client_name} has just started a new session.

Here is their client profile:

{profile}

Write a single short opening message — 2 to 4 sentences — that:
- Greets them warmly by first name
- References something specific from where they left off using ONLY facts, themes, or session notes that appear verbatim in the profile above. If you cannot name something concrete from the profile, keep the opener general: glad they are here and ask what is on their mind.
- Ends with one open question
- Never mentions Telegram, bots, AI, microphones, tech, platforms, debugging, beta, prototypes, or app mechanics

Do not invent prior conversations, topics, or sessions that are not clearly supported by the profile text. Do not say you reviewed their file. Speak naturally, as if you simply remember them. No em dashes. No filler phrases like "I've been thinking about you."

Never ask questions that assume the client has been struggling, unwell, or in need of care — for example, never say anything like "who's been taking care of you lately," "has someone been looking after you," or "have you been okay." These feel presumptuous and clinically off. Only reference difficulty if the profile explicitly describes it.

This is a returning client only. Never imply a first meeting. Return only the greeting — nothing else."""

    try:
        response = await _claude_create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip().replace("\u2014", ",").replace("\u2013", ",")
    except Exception as e:
        logger.error("Returning greeting generation failed: %s", e)
        return f"Hey {client_name}, good to have you back. What's on your mind today?"


async def generate_session_start_nudge(client_name: str, profile: str) -> str:
    """One short varied sentence inviting the returning client to start — different every session."""
    system = f"""You are Tanya, a life coach. {client_name} just opened a new session and heard your voice note opener.

Write ONE short opening question or statement that invites them in. It should sound like something Tanya would naturally text — warm, casual, present. A question is fine and often better. Keep it under 9 words.

Good examples of the right tone:
- "Where would you like to start?"
- "What's been taking up the most space recently?"
- "How are you feeling today?"
- "What's on your mind?"
- "Just start wherever feels right."

Rules:
- One sentence only, 9 words max
- Warm and natural — never clinical, formal, or stiff
- Vary the phrasing every session — never repeat the same line
- No em dashes
- Do not start with "I"
- Do not reference prior sessions, client details, or specific themes
- Return only that sentence, nothing else"""

    try:
        response = await _claude_create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=40,
            system=system,
            messages=[{"role": "user", "content": profile[:500] if profile else "returning client"}],
        )
        return response.content[0].text.strip().replace("—", ", ").replace("–", ", ")
    except Exception as e:
        logger.error("Session start nudge generation failed: %s", e)
        return "Just start wherever feels right."


async def generate_new_client_opener_bridge(first_message: str) -> str:
    """Short lead-in before OPENER_INTRO for every new client — same two-message flow for all first lines."""
    system = """You are Tanya, a life coach. A brand-new client just sent their first message via iMessage (below).

Write a SHORT opening (1–2 sentences max) they will see immediately BEFORE her fixed welcome text. That welcome always starts with "Hi, I'm Tanya" and then covers privacy, terms, and how she works — you must NOT quote or repeat any of that welcome.

If they shared something concrete (a worry, a person, a situation, something they want help with), respond briefly and warmly in plain language — the vibe of "yes, that's absolutely something we can talk about" — without coaching, solving, or asking a deep question yet.
If they only said hi/hello or something minimal, give a brief warm line (e.g. glad they reached out). Do not invent details they didn't mention.

Rules:
- 1–2 sentences only
- No em dashes
- Standard punctuation spacing: space after a comma, never before it
- Do not start with "I"
- No privacy, terms, legal, or encryption talk
- Never mention tech, Telegram, bots, AI, microphones, syncing, prototypes, keyboards, figuring out platforms, debugging, beta, or app mechanics
- Return only this opening, nothing else"""

    try:
        response = await _claude_create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=150,
            system=system,
            messages=[{"role": "user", "content": first_message}],
        )
        return response.content[0].text.strip().replace("\u2014", ",").replace("\u2013", ",")
    except Exception as e:
        logger.error("New client opener bridge failed: %s", e)
        return "Thanks for being here."


async def generate_new_client_opener_followup_line(first_message: str) -> str:
    """Second outbound line for new clients: one coaching question that follows from what they said."""
    system = """Tanya already sent her short personalized opener. You write ONLY her single next iMessage line.

Write for the ear: natural spoken English, no bullet lists, no markdown, no parentheses with stage directions.

If the client shared something specific (a worry, a person, a situation, a feeling), ask ONE direct coaching question that goes one layer deeper into what they shared. Do not ask what is on their mind — they already told you. Ask about the thing they said.

If they only said hi or something minimal, ask what is on their mind right now. Variations: "What's on your mind today?", "What's weighing on you today?", "What's coming up for you right now?".

Rules:
- One sentence, at most 15 words
- No em dashes
- Standard punctuation spacing: space after a comma, never before it
- Do not start with "I"
- Never mention Telegram, bots, AI, microphones, syncing, prototyping, dashboards, keyboards, figuring out tech, debugging, beta, apps, platforms, or product mechanics.
- Return only that line, nothing else"""

    try:
        response = await _claude_create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=80,
            system=system,
            messages=[{"role": "user", "content": first_message}],
        )
        return response.content[0].text.strip().replace("\u2014", ",").replace("\u2013", ",")
    except Exception as e:
        logger.error("New client opener follow-up line failed: %s", e)
        return "What's on your mind today?"


_GREETING_PHRASES = {
    "hey", "hi", "hello", "hiya", "yo", "sup", "howdy",
    "hey there", "hi there", "hello there",
    "what's up", "whats up", "what up",
    "hey hey", "hi hi",
}

def _is_minimal_opener(text: str) -> bool:
    """True when the first message is a pure greeting with no question or substance."""
    import re
    normalized = re.sub(r"\btanya\b", "", text.lower(), flags=re.IGNORECASE)
    normalized = re.sub(r"[^\w\s]", "", normalized).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized in _GREETING_PHRASES


async def generate_new_client_coaching_opener(first_message: str) -> str:
    """Direct Sonnet coaching response for new clients who opened with something substantive."""
    system = """You are Tanya, a professional life coach. A brand-new client just sent their first iMessage to you. They shared something specific and real.

Respond warmly and directly to what they said. Acknowledge what they shared briefly, then ask one clear coaching question that goes one layer deeper. This is the first thing they'll hear from you after your contact card.

Rules:
- 2-3 sentences max
- No em dashes
- Do not start with "I"
- No mention of terms, privacy, legal, or how you work
- No bullet points or markdown
- Standard punctuation spacing: space after a comma, never before it
- Never mention bots, AI, platforms, apps, or tech mechanics
- Return only the response, nothing else"""

    try:
        response = await _claude_create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": first_message}],
        )
        return response.content[0].text.strip().replace("—", ",").replace("–", ",")
    except Exception as e:
        logger.error("New client coaching opener failed: %s", e)
        return "So glad you reached out. What's on your mind today?"


async def prepare_new_client_opener_parts(user_text: str) -> tuple[str, str]:
    """Bridge + second-line Haiku in parallel (always two outbound messages total from Tanya)."""
    bridge, followup = await asyncio.gather(
        generate_new_client_opener_bridge(user_text),
        generate_new_client_opener_followup_line(user_text),
    )
    return bridge, followup


async def deliver_new_client_opener_messages(
    phone: str,
    user_name: str,
    bridge: str,
    followup: str,
) -> str:
    """Outbound flow: (1) bridge; (2) coaching invite. OPENER_INTRO shown via landing page pop-up."""
    await blooio_send_message(phone, bridge)
    logger.info("New client opener: bridge sent for %s", user_name)

    await blooio_send_message(phone, followup)
    logger.info("New client opener: follow-up line sent for %s", user_name)

    return f"{bridge}\n\n{followup}"


def _parse_stripe_confirmation_label(raw: str) -> str:
    """Normalize Haiku output to affirmative | negative | unclear."""
    lines = (raw or "").strip().lower().splitlines()
    line = lines[0] if lines else ""
    first = line.split()[0].rstrip(".,!?") if line.split() else ""
    if first in ("affirmative", "negative", "unclear"):
        return first
    return "unclear"


async def classify_stripe_confirmation_intent(
    phone: str, username: str, user_text: str,
) -> str:
    """Use Haiku to classify reply after trial Stripe prompt: affirmative | negative | unclear."""
    system = """You classify the client's latest message in iMessage.

Context: They finished the free trial. Tanya offered TanyaTalk for a monthly fee and offered to send a secure Stripe payment link when they are ready. This message is their reply.

Decide intent:
- affirmative: They want the link or to pay or subscribe now, clear yes, agreement to continue with payment in natural language (including phrases like let's do it, I'm in, sounds good if clearly about paying).
- negative: They decline, not ready, maybe later, no thanks, or clearly refuse the paid option for now.
- unclear: Ambiguous, mostly off-topic, only asks how pricing works without accepting or refusing, jokes, hedging without commitment, or you cannot tell if they want the link sent now.

Reply with exactly one word on the first line: affirmative OR negative OR unclear. Nothing else."""

    try:
        response = await _claude_create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=30,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )
        await asyncio.to_thread(record_coaching_usage, phone, username, response)
        label = _parse_stripe_confirmation_label(response.content[0].text)
        logger.info(
            "Stripe confirmation intent phone_key=%s label=%s",
            _phone_key(phone),
            label,
        )
        return label
    except Exception as e:
        logger.error("Stripe confirmation classification failed: %s", e)
        return "unclear"


SESSION_CLOSE_CONFIRMATION = "I've saved our session. When you're here, I'm here. 💛"
SESSION_CLOSE_PS = "ps: send \"END SESSION\" when you're ready to end a session."
LINK_RESPONSE = "I can't open links, but I'm here with you. What's on your mind?"


def _referral_state_load_unlocked() -> dict[str, int]:
    if not REFERRAL_STATE_PATH.exists():
        return {}
    try:
        with REFERRAL_STATE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        out: dict[str, int] = {}
        for k, v in data.items():
            if not isinstance(k, str):
                continue
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
        return out
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return {}


def _referral_state_save_unlocked(state: dict[str, int]) -> None:
    REFERRAL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REFERRAL_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def referral_get_last_nudge_session(client_name: str) -> int | None:
    with _REFERRAL_STATE_LOCK:
        s = _referral_state_load_unlocked()
        v = s.get(client_name)
        return v if v is not None else None


def referral_record_nudge(client_name: str, session_num: int) -> None:
    with _REFERRAL_STATE_LOCK:
        s = _referral_state_load_unlocked()
        s[client_name] = session_num
        _referral_state_save_unlocked(s)


def referral_nudge_prompt_allowed(phone: str, client_name: str) -> bool:
    """True when session spacing allows offering the optional referral line (context still model-gated)."""
    sess = session_numbers.get(phone, 0)
    if sess < REFERRAL_NUDGE_FIRST_ELIGIBLE_SESSION:
        return False
    if referral_nudge_used_this_session.get(phone):
        return False
    last = referral_get_last_nudge_session(client_name)
    if last is None:
        return True
    return sess - last >= REFERRAL_NUDGE_MIN_SESSIONS_BETWEEN


def referral_nudge_system_instruction() -> str:
    return f"""
---

## Optional referral line (this turn only — strict)

The system may show this block only when spacing rules allow. **You** decide if the **client's latest message** genuinely fits one of these: clear **gratitude toward you or the work**, they **mention someone else struggling** who might benefit, or a plain **"this helped"** beat (relief, shift, thank you).

**If there is no clear fit, ignore this entire section** and coach normally with no referral language.

**If there is a clear fit:** weave **one short sentence** about sharing your number into the **same reply** as your normal coaching—same flow, same voice, not a separate message, not a labeled postscript, not its own paragraph that feels bolted on. Land what they said first; slip the invite in where it follows naturally (often mid-reply or toward the close of the same thought), then continue or finish with your one question if the turn still needs it. No referral codes, links, or tracking. Not salesy. Do not combine with any other CTA.

**At most once** in this session (the system enforces spacing across sessions).

**If and only if** you included that sentence, end your reply with a **final line** containing exactly `{REFERRAL_NUDGE_MARKER}` and nothing else on that line (the client does not see this line). If you did not include the sentence, do **not** output the marker.
""".strip()


def strip_referral_nudge_marker(text: str) -> tuple[str, bool]:
    """Remove trailing marker from model output; return (client_visible_text, marker_was_present)."""
    stripped = text.rstrip()
    if stripped.endswith(REFERRAL_NUDGE_MARKER):
        core = stripped[: -len(REFERRAL_NUDGE_MARKER)].rstrip()
        return core, True
    return text, False


def cancel_session_timeout(phone: str) -> None:
    if phone in timeout_tasks and not timeout_tasks[phone].done():
        timeout_tasks[phone].cancel()


async def _finalize_session_writes(
    phone: str,
    ph: str,
    client_name: str,
    history: list[dict],
    session_num: int,
    session_path: Path | None,
    *,
    session_label: str | None = None,
    is_dabble: bool = False,
) -> bool:
    """Profile update, vault indexes, follow-up — runs after in-memory session state is cleared."""
    if not history:
        return True
    display = session_label or str(session_num)
    try:
        logger.info(
            "Ending session for hash %s session %s (%d messages)%s",
            ph[:12], display, len(history), " [dabble]" if is_dabble else "",
        )
        today = datetime.date.today().isoformat()

        profile = await asyncio.to_thread(load_client_profile, ph) or ""
        if is_dabble:
            logger.info(
                "Skipping profile update for dabble session %s phone_key=%s",
                display, ph[:12],
            )
        elif _profile_includes_session(profile, session_num, session_label):
            logger.info(
                "Profile already includes session %s for phone_key=%s; skipping profile update",
                display, ph[:12],
            )
        else:
            # For the first real session, fold in any prior Session 0.x dabble transcripts
            prior_dabbles: list[tuple[str, str]] | None = None
            if session_num == 1:
                prior_dabbles = await asyncio.to_thread(load_dabble_transcripts, ph)
                if prior_dabbles:
                    logger.info(
                        "Including %d prior dabble(s) in Session 1 profile for phone_key=%s",
                        len(prior_dabbles), ph[:12],
                    )
            try:
                await update_client_profile(
                    ph, client_name, session_num, history, session_label=session_label,
                    prior_dabble_texts=prior_dabbles,
                )
            except Exception as e:
                logger.error(
                    "Profile update failed for phone_key=%s session=%s: %s",
                    ph[:12], display, e,
                )
                return False
            profile = await asyncio.to_thread(load_file, profile_path_for(ph))
            if not _profile_includes_session(profile or "", session_num, session_label):
                logger.error(
                    "Profile missing session %s after update for phone_key=%s",
                    display, ph[:12],
                )
                return False

        transcript_lines = []
        for msg in history:
            role = "Tanya" if msg["role"] == "assistant" else client_name
            transcript_lines.append(f"{role}: {msg['content']}")
        transcript = "\n".join(transcript_lines)

        user_turns = sum(1 for m in history if m["role"] == "user")
        paid = await has_tanyatalk_access(phone)
        if (
            not is_dabble
            and session_path
            and session_num >= tanya_followup.MIN_SESSION_NUM_FOR_FOLLOWUP
            and user_turns >= MIN_EXCHANGES_FOR_FOLLOWUP
            and paid
        ):
            ended_at = tanya_followup._session_ended_at_from_extraction(session_path)
            if not _session_has_follow_up_extraction(session_path):
                ended_at = datetime.datetime.now(timezone.utc)
                await append_follow_up_extraction(
                    session_path, client_name, session_num, history, ended_at
                )
            elif ended_at is None:
                ended_at = datetime.datetime.now(timezone.utc)
            tanya_followup.ensure_follow_up_scheduled(
                phone, client_name, session_path, session_num, ended_at,
            )
        elif session_path:
            if is_dabble:
                reason = "dabble archive"
            elif session_num < tanya_followup.MIN_SESSION_NUM_FOR_FOLLOWUP:
                reason = "session 1"
            elif user_turns < MIN_EXCHANGES_FOR_FOLLOWUP:
                reason = f"{user_turns} user turns < {MIN_EXCHANGES_FOR_FOLLOWUP}"
            elif not paid:
                reason = "no paid access"
            else:
                reason = "unknown"
            logger.info(
                "Skipping follow-up for hash %s session %s (%s)",
                ph[:12], display, reason,
            )

        try:
            await update_vault_index_files(
                ph, client_name, session_num, today, profile, transcript,
                session_label=session_label, is_dabble=is_dabble,
            )
            mark_vault_dirty()
            asyncio.create_task(_immediate_vault_push())
        except Exception as e:
            logger.error(
                "Vault index update failed for phone_key=%s session=%s: %s",
                ph[:12], display, e,
            )
            return False

        if session_path and session_path.exists() and not _session_is_finalized(session_path):
            try:
                with session_path.open("a", encoding="utf-8") as f:
                    f.write("\n<!-- session:finalized -->\n")
            except Exception as e:
                logger.warning("Could not write session finalized marker: %s", e)

        _dequeue_pending_finalize(ph, session_num, session_label)
        return True
    except Exception as e:
        logger.exception("Session finalize failed for phone_key=%s: %s", ph[:12], e)
        return False


async def _run_background_finalize(
    phone: str,
    ph: str,
    client_name: str,
    history: list[dict],
    session_num: int,
    session_path: Path | None,
    *,
    session_label: str | None = None,
    is_dabble: bool = False,
) -> None:
    await _finalize_session_writes(
        phone, ph, client_name, history, session_num, session_path,
        session_label=session_label, is_dabble=is_dabble,
    )


async def _process_pending_finalizes() -> None:
    """Retry profile/vault writes interrupted by redeploy."""
    jobs = _load_pending_finalizes()
    if not jobs:
        return
    logger.info("Retrying %d pending session finalize job(s)", len(jobs))
    for job_id, job in list(jobs.items()):
        session_path_str = job.get("session_path", "")
        session_path = Path(session_path_str) if session_path_str else None
        session_label = job.get("session_label")
        is_dabble = bool(job.get("is_dabble"))
        if not session_path or not session_path.exists():
            logger.warning("Dropping stale finalize job %s — session file missing", job_id)
            _dequeue_pending_finalize(job["ph"], job["session_num"], session_label)
            continue
        if _session_is_finalized(session_path):
            logger.info("Finalize job %s already complete — removing from queue", job_id)
            _dequeue_pending_finalize(job["ph"], job["session_num"], session_label)
            continue
        history = history_from_session_file(session_path, job.get("client_name", "Client"))
        if not history:
            logger.warning("Finalize job %s has no parseable history — dropping", job_id)
            _dequeue_pending_finalize(job["ph"], job["session_num"], session_label)
            continue
        ok = await _finalize_session_writes(
            job["phone"],
            job["ph"],
            job.get("client_name", "Client"),
            history,
            job["session_num"],
            session_path,
            session_label=session_label,
            is_dabble=is_dabble,
        )
        if ok:
            logger.info("Pending finalize completed for job %s", job_id)
        else:
            logger.warning("Pending finalize failed for job %s — will retry on next start", job_id)


def _clear_session_memory(phone: str) -> None:
    cancel_cache_warming(phone)
    _cached_static_prompts.pop(phone, None)
    referral_nudge_used_this_session.pop(phone, None)
    conversations.pop(phone, None)
    session_files.pop(phone, None)
    session_numbers.pop(phone, None)
    session_outlines.pop(phone, None)
    session_profiles.pop(phone, None)
    voice_note_redirects.pop(phone, None)
    pending_first_message_opener.pop(phone, None)
    awaiting_contact_save.pop(phone, None)
    awaiting_topup_confirmation.pop(phone, None)
    free_trial_user_msg_count.pop(phone, None)
    free_trial_90_warned.pop(phone, None)
    last_activity.pop(phone, None)
    new_client_voice_followup_snippet.pop(phone, None)
    timeout_tasks.pop(phone, None)


async def end_session(
    phone: str,
    *,
    background_writes: bool = False,
    quiet_dabble: bool = False,
):
    """Transcript already written in real time — update profile + vault indexes, then clear state."""
    ph = phone_to_hash(phone)
    client_name = client_names.get(phone, "Client")
    history = list(conversations.get(phone, []))
    session_num = session_numbers.get(phone, 1)
    session_path = session_files.get(phone)
    session_label: str | None = None
    is_dabble = False

    if quiet_dabble and session_path and session_path.exists() and session_num == 1:
        is_dabble = True
        session_path, session_label = await asyncio.to_thread(
            archive_active_session_to_dabble, ph, session_path,
        )
        save_onboarding_checkpoint(
            ph,
            pending_first_message_opener=pending_first_message_opener.get(phone, False),
            awaiting_contact_save=awaiting_contact_save.get(phone, False),
        )

    # Write close marker so crash detection can distinguish a clean close from a crash
    if session_path and session_path.exists():
        try:
            with session_path.open("a", encoding="utf-8") as f:
                marker = "<!-- session:closed:dabble -->\n" if is_dabble else "<!-- session:closed -->\n"
                f.write(f"\n{marker}")
        except Exception as e:
            logger.warning("Could not write session close marker: %s", e)

    _clear_session_memory(phone)

    if not history:
        return

    _enqueue_pending_finalize(
        phone, ph, client_name, session_num, session_path,
        session_label=session_label, is_dabble=is_dabble,
    )

    if background_writes:
        task = asyncio.create_task(
            _run_background_finalize(
                phone, ph, client_name, history, session_num, session_path,
                session_label=session_label, is_dabble=is_dabble,
            )
        )
        _finalize_tasks.add(task)
        task.add_done_callback(_finalize_tasks.discard)
        return

    await _finalize_session_writes(
        phone, ph, client_name, history, session_num, session_path,
        session_label=session_label, is_dabble=is_dabble,
    )


async def ai_detects_cancel_intent(text: str) -> bool:
    """Return True if Claude thinks the message is a subscription cancellation request."""
    try:
        response = await _claude_create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=5,
            system="Reply with only 'yes' or 'no'. No other text.",
            messages=[{"role": "user", "content": (
                f"Does this message express a desire to cancel their paid subscription or stop being billed? Answer yes only if it is clearly about billing or payment, not about ending a conversation or coaching session.\n\nMessage: {text}"
            )}],
        )
        return response.content[0].text.strip().lower().startswith("yes")
    except Exception:
        return False


async def ai_detects_delete_intent(text: str) -> bool:
    """Return True if Claude thinks the message is a data-deletion request."""
    try:
        response = await _claude_create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=5,
            system="Reply with only 'yes' or 'no'. No other text.",
            messages=[{"role": "user", "content": (
                f"Does this message express a desire to delete, erase, or remove the person's data, "
                f"account, or information from the system?\n\nMessage: {text}"
            )}],
        )
        return response.content[0].text.strip().lower().startswith("yes")
    except Exception:
        return False


async def classify_delete_confirmation_intent(user_text: str) -> str:
    """Haiku sentiment check for delete confirmation: affirmative | negative | unclear."""
    _clean = user_text.strip().lower().rstrip(".,!?")
    _YES = {"yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay", "confirmed",
            "confirm", "go ahead", "do it", "delete it", "delete", "affirmative"}
    _NO  = {"no", "n", "nope", "nah", "cancel", "stop", "never mind", "nevermind",
            "i changed my mind", "keep it", "don't", "dont", "negative"}
    if _clean in _YES:
        return "affirmative"
    if _clean in _NO:
        return "negative"

    system = (
        "A client was asked to confirm they want all their data permanently deleted. "
        "Classify their reply.\n\n"
        "- affirmative: any form of yes, go ahead, do it, sure, ok, yep, confirmed, delete it, etc.\n"
        "- negative: any form of no, cancel, keep it, never mind, I changed my mind, etc.\n"
        "- unclear: ambiguous or unrelated\n\n"
        "Reply with exactly one word: affirmative OR negative OR unclear. Nothing else."
    )
    try:
        response = await _claude_create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=10,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )
        word = response.content[0].text.strip().lower().split()[0].rstrip(".,!?")
        return word if word in ("affirmative", "negative", "unclear") else "unclear"
    except Exception as e:
        logger.warning("Delete confirmation classify error: %s", e)
        return "unclear"


async def classify_topup_confirmation_intent(user_text: str, *, allow_haiku: bool = True) -> str:
    """Classify reply to monthly-cap top-up offer: affirmative | negative | unclear."""
    clean = user_text.strip().lower().rstrip(".,!?")
    yes_exact = {
        "yes", "yeah", "yep", "yup", "sure", "ok", "okay",
        "send", "send it", "send the link", "affirmative",
    }
    no_exact = {"no", "nope", "nah", "wait", "later", "negative"}
    if clean in yes_exact:
        return "affirmative"
    if clean in no_exact:
        return "negative"
    if any(p in clean for p in ("not sure", "not yet", "not now")):
        return "unclear"
    if re.search(r"\b(no|nope|nah|not|wait|don't|dont)\b", clean):
        return "negative"
    if re.search(r"\blater\b", clean) and "maybe" not in clean:
        return "negative"
    if re.search(r"\b(yes|yeah|yep|yup|sure|ok|okay|send)\b", clean):
        return "affirmative"
    if not allow_haiku:
        return "unclear"

    system = (
        "A client hit their monthly message cap. Tanya asked if they want a $5 top-up link "
        "(60 messages each). Classify their reply.\n\n"
        "- affirmative: wants the link sent now (yes, sure, send it, ok, etc.)\n"
        "- negative: declines or wants to wait until next billing period\n"
        "- unclear: ambiguous, off-topic, or hedging without commitment (e.g. not sure)\n\n"
        "Reply with exactly one word: affirmative OR negative OR unclear. Nothing else."
    )
    try:
        response = await _claude_create(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=10,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )
        word = response.content[0].text.strip().lower().split()[0].rstrip(".,!?")
        return word if word in ("affirmative", "negative", "unclear") else "unclear"
    except Exception as e:
        logger.warning("Top-up confirmation classify error: %s", e)
        return "unclear"


async def delete_client_data(phone: str) -> None:
    """Anonymize all data for a client who requested deletion.

    The session folder is renamed to an unguessable UUID-based name so the bot
    can never find it again (treating the user as new on return), while the raw
    files remain on disk for any audit/legal need. All other state tied to
    phone is also cleared.
    """
    import uuid

    ph = phone_to_hash(phone)

    # End any live session cleanly first (writes transcript, clears state).
    if phone in conversations:
        await end_session(phone)

    # Rename session folder and profile file to unguessable names so the bot
    # can never find them again (treats the user as brand-new on return).
    # Anonymized items are moved into a _Deleted/ subfolder to keep the vault tidy.
    sessions_deleted_bin = Path(VAULT_PATH) / "02-Client-Sessions" / "_Deleted"
    profiles_deleted_bin = Path(VAULT_PATH) / "02-Client-Sessions" / "Client Profiles" / "_Deleted"

    client_dir = Path(VAULT_PATH) / "02-Client-Sessions" / ph
    if client_dir.exists():
        deleted_name = f"_deleted_{uuid.uuid4().hex}"
        await asyncio.to_thread(sessions_deleted_bin.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(client_dir.rename, sessions_deleted_bin / deleted_name)
        logger.info("Anonymized session folder for phone_key=%s → _Deleted/%s", ph[:12], deleted_name)

    profile_file = profile_path_for(ph)
    if profile_file.exists():
        deleted_profile_name = f"_deleted_{uuid.uuid4().hex}.md"
        await asyncio.to_thread(profiles_deleted_bin.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(profile_file.rename, profiles_deleted_bin / deleted_profile_name)
        logger.info("Anonymized profile file for phone_key=%s → _Deleted/%s", ph[:12], deleted_profile_name)

    # Wipe monthly usage only. Trial flag and access record are kept intentionally —
    # a returning deleted client routes to the subscription offer, not a new free trial.
    await billing_db.delete_monthly_usage(ph)

    # Cancel any pending follow-up jobs for this client.
    try:
        tanya_followup.cancel_all_followup_jobs_for_chat(phone)
    except Exception:
        pass

    # Clear all remaining in-memory state.
    for state_dict in (
        client_names,
        paid_tanyatalk_access,
        mesh_tanyatalk_included,
        awaiting_stripe_confirmation,
        awaiting_delete_confirmation,
        awaiting_topup_confirmation,
        topup_link_sent,
        awaiting_contact_save,
        pending_first_message_opener,
        referral_nudge_used_this_session,
        cache_warm_tasks,
        _cached_static_prompts,
        _pending_messages,
        _pending_phones,
        _debounce_tasks,
        _typing_tasks,
        new_client_voice_followup_snippet,
    ):
        state_dict.pop(phone, None)

    logger.info("Deletion complete for phone_key=%s", ph[:12])


def load_coach_teaching_ratio() -> tuple[int, int]:
    """Persisted coaching/teaching blend; default 50/50."""
    default = (50, 50)
    try:
        if not COACHING_BLEND_CONFIG_PATH.exists():
            return default
        data = json.loads(COACHING_BLEND_CONFIG_PATH.read_text(encoding="utf-8"))
        c = int(data.get("coaching_pct", 50))
        t = int(data.get("teaching_pct", 50))
        if c < 1 or t < 1 or c + t != 100:
            return default
        return c, t
    except Exception:
        return default


def save_coach_teaching_ratio(coaching_pct: int, teaching_pct: int) -> None:
    COACHING_BLEND_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    COACHING_BLEND_CONFIG_PATH.write_text(
        json.dumps(
            {"coaching_pct": coaching_pct, "teaching_pct": teaching_pct},
            indent=2,
        ),
        encoding="utf-8",
    )


def coaching_teaching_blend_instruction() -> str:
    c, t = load_coach_teaching_ratio()
    return f"""## Coaching / teaching blend (continuous voice)

Your default blend is **{c}% coaching / {t}% teaching**. This is not a mode switch — it is a texture. **{c}%** of the weight in every response is ICF-style coaching: questions, reflection, holding space, client-led discovery. **{t}%** is teaching: actual pauses in the coaching flow where Tanya stops, names a principle that directly applies to what the client is experiencing right now, gives them something to understand or sit with, then returns to coaching questions. Teaching does not feel like a lesson being delivered — it feels like Tanya handing the client a flashlight. One principle at a time. Never name the framework. When a client is using the wrong tool for their phase (for example trying to rewrite when they need regulation), name what they actually need — this is the tool-timing principle. Regardless of blend ratio, no response should exceed two short paragraphs unless the client has explicitly requested content delivery."""


# Distilled from ICF 2025 Core Competencies (session agreements, listening, evokes awareness,
# facilitates growth). Full official text lives in the vault PDF; this block only guards autonomy vs teaching.
ICF_AUTONOMY_DISTILLED_SECTION = """## ICF-aligned autonomy (distilled)

When the urge to teach or solve conflicts with coaching texture, prioritize these:

- Session direction stays client-led: honor what they want from this conversation before you deepen or weave framework language.
- Reflect or summarize what you heard before you advance the layer or add teaching.
- Invite more about their experience in the moment before you name patterns or frameworks.
- Invite them to generate their own ideas about what is true, what might help, or what they are willing to try next before you deliver a heavy reframe. Their wording lands first when possible.
- Offer reframes or concise observations lightly and without attachment, to spark their insight, not to replace their ownership of the answer.
- Next steps and accountability stay theirs to design; support and sharpen only after they claim a direction.
- Stay in coaching scope: reflection, meaning, and choice. Not psychotherapy, medical, legal, or financial advice. Encourage appropriate professional help when needs are clearly outside coaching."""


def becoming_you_phase_for_prompt(profile_md: str) -> str | None:
    """Inject phase only after at least one completed session is reflected in the profile."""
    if not profile_md or not profile_indicates_prior_session(profile_md):
        return None
    sec = _extract_profile_section(profile_md, "BecomingYou Phase")
    if not sec.strip():
        return None
    m = re.search(r"\*\*Current phase:\*\*\s*(.+)", sec, re.IGNORECASE)
    if not m:
        return None
    line = m.group(1).strip().split("\n")[0].strip()
    if re.match(r"^N/A\b", line, re.IGNORECASE) or "not yet identified" in line.lower():
        return None
    return line


# ---------------------------------------------------------------------------
# Timeout task
# ---------------------------------------------------------------------------

async def session_timeout_task(phone: str, *, delay_seconds: float | None = None) -> None:
    """Wait SESSION_TIMEOUT_MINUTES then end session if no activity."""
    await asyncio.sleep(
        delay_seconds if delay_seconds is not None else SESSION_TIMEOUT_MINUTES * 60
    )
    lock = await _get_chat_message_lock(phone)
    ended = False
    ended_quietly = False
    is_ft = False
    async with lock:
        if phone in conversations and conversations[phone]:
            is_ft = await in_first_free_trial_session(phone)
            ft_count = free_trial_user_msg_count.get(phone, 0)
            if is_ft and ft_count < FREE_TRIAL_MIN_MSGS_FOR_CLOSE:
                logger.info(
                    "Free trial quiet timeout for phone_key=%s — %d user turn(s), need %d for paywall close",
                    _phone_key(phone), ft_count, FREE_TRIAL_MIN_MSGS_FOR_CLOSE,
                )
                await end_session(phone, quiet_dabble=True)
                ended_quietly = True
            else:
                logger.info("Session timeout for phone_key=%s", _phone_key(phone))
                if is_ft:
                    ph = phone_to_hash(phone)
                    await mark_free_trial_completed(phone)
                    await billing_db.delete_trial_msg_count(ph)
                    awaiting_stripe_confirmation[phone] = True
                await end_session(phone)
                ended = True
    if ended_quietly:
        return
    if ended:
        close_text = FREE_TRIAL_CLOSE_TEXT if is_ft else SESSION_CLOSE_CONFIRMATION
        try:
            await blooio_send_message(phone, close_text)
        except Exception:
            pass


def reset_timeout(phone: str, *, delay_seconds: float | None = None) -> None:
    """Cancel any existing timeout and start a fresh one."""
    if phone in timeout_tasks and not timeout_tasks[phone].done():
        timeout_tasks[phone].cancel()
    timeout_tasks[phone] = asyncio.create_task(
        session_timeout_task(phone, delay_seconds=delay_seconds)
    )


# ---------------------------------------------------------------------------
# Cache warming — keeps Anthropic prompt cache alive between messages
# ---------------------------------------------------------------------------

async def _cache_warm_loop(phone: str) -> None:
    """Send a minimal API call every CACHE_WARM_INTERVAL_SEC to keep the prompt cache alive."""
    while True:
        await asyncio.sleep(CACHE_WARM_INTERVAL_SEC)
        static_prompt = _cached_static_prompts.get(phone)
        if not static_prompt or phone not in session_files:
            logger.info(
                "Cache warm loop exiting phone_key=%s reason=no_prompt_or_session",
                _phone_key(phone),
            )
            break
        try:
            response = await _claude_create(
                model=CLAUDE_MODEL,
                max_tokens=1,
                system=[
                    {"type": "text", "text": static_prompt, "cache_control": {"type": "ephemeral"}},
                ],
                messages=[{"role": "user", "content": "."}],
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )
            _u = getattr(response, "usage", None)
            cr = getattr(_u, "cache_read_input_tokens", 0) if _u else 0
            cc = getattr(_u, "cache_creation_input_tokens", 0) if _u else 0
            logger.info(
                "Cache warm ping phone_key=%s cache_read=%s cache_creation=%s",
                _phone_key(phone),
                cr,
                cc,
            )
        except Exception as e:
            logger.warning("Cache warm ping failed for phone_key=%s: %s", _phone_key(phone), e)


def start_cache_warming(phone: str) -> None:
    """Start or restart the cache warming loop for a session."""
    cancel_cache_warming(phone)
    cache_warm_tasks[phone] = asyncio.ensure_future(_cache_warm_loop(phone))


def cancel_cache_warming(phone: str) -> None:
    task = cache_warm_tasks.pop(phone, None)
    if task and not task.done():
        task.cancel()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def perform_session_close(phone: str, user_text: str) -> bool:
    """End active session if one exists; log close to transcript + history, then send confirmation."""
    await asyncio.sleep(TYPING_BUBBLE_DELAY_SEC)
    await blooio_typing_on(phone)
    lock = await _get_chat_message_lock(phone)
    async with lock:
        if phone not in session_files:
            await blooio_send_message(
                phone,
                "There isn't an active session to close. Send Tanya a message whenever you want to start.",
            )
            return False

        is_ft = await in_first_free_trial_session(phone)
        ft_count = free_trial_user_msg_count.get(phone, 0)
        is_quiet_dabble = is_ft and ft_count < FREE_TRIAL_MIN_MSGS_FOR_CLOSE
        close_text = FREE_TRIAL_CLOSE_TEXT if (is_ft and not is_quiet_dabble) else SESSION_CLOSE_CONFIRMATION

        client_name = client_names.get(phone, "Client")
        user_close = user_text.strip() or "end session"

        session_path = session_files[phone]
        await asyncio.to_thread(
            append_exchange, session_path, client_name, user_close, close_text
        )

        if phone not in conversations:
            conversations[phone] = []
        conversations[phone].append({"role": "user", "content": user_close})
        conversations[phone].append({"role": "assistant", "content": close_text})
        if len(conversations[phone]) > MAX_HISTORY * 2:
            conversations[phone] = conversations[phone][-(MAX_HISTORY * 2):]

        if is_ft and not is_quiet_dabble:
            ph = phone_to_hash(phone)
            await mark_free_trial_completed(phone)
            await billing_db.delete_trial_msg_count(ph)
            awaiting_stripe_confirmation[phone] = True

        cancel_session_timeout(phone)
        await end_session(phone, background_writes=True, quiet_dabble=is_quiet_dabble)

    await blooio_send_message(phone, close_text)
    return True


async def begin_session_with_opening(phone: str, client_name: str, phone_hash: str) -> bool:
    """Create session on disk + outline + profile cache + timeout; opener is sent from handle_inbound_message."""
    sess_num = await asyncio.to_thread(get_next_session_number, phone_hash)
    if (
        sess_num == 1
        and BLOCK_AFTER_FREE_TRIAL
        and not _is_bypass_phone(phone)
        and await _has_completed_free_trial(phone)
        and not await has_tanyatalk_access(phone)
    ):
        await blooio_send_message(phone, POST_TRIAL_RESET_DENIED_MESSAGE)
        awaiting_stripe_confirmation[phone] = True
        logger.info(
            "Refused session 1 for completed-trial phone_key=%s",
            phone_hash[:12],
        )
        return False

    tanya_followup.cancel_all_followup_jobs_for_chat(phone)
    voice_note_redirects[phone] = 0
    referral_nudge_used_this_session.pop(phone, None)

    logger.info("Session opening setup: client=%s", client_name)

    session_numbers[phone] = sess_num
    session_files[phone] = await asyncio.to_thread(start_session_file, phone_hash, client_name, sess_num)
    session_outlines[phone] = await asyncio.to_thread(load_session_outline)
    session_profiles[phone] = await asyncio.to_thread(load_client_profile, phone_hash)

    ckpt = await asyncio.to_thread(pop_onboarding_checkpoint, phone_hash)
    if ckpt is not None:
        if ckpt.get("pending_first_message_opener"):
            pending_first_message_opener[phone] = True
        if ckpt.get("awaiting_contact_save"):
            awaiting_contact_save[phone] = True
        logger.info(
            "Restored onboarding checkpoint for phone_key=%s opener=%s contact_save=%s",
            phone_hash[:12],
            ckpt.get("pending_first_message_opener"),
            ckpt.get("awaiting_contact_save"),
        )
    else:
        pending_first_message_opener[phone] = True
    reset_timeout(phone)
    return True


async def open_coaching_session_after_mini(phone: str, client_name: str) -> None:
    """Start a full session after mini-session chooses SESSION_NOW (under per-phone lock)."""
    ph = phone_to_hash(phone)
    lock = await _get_chat_message_lock(phone)
    async with lock:
        conversations[phone] = []
        # Use cached name from earlier in the inbound flow; fetch from Stripe only if missing
        resolved = client_name or client_names.get(phone) or await get_stripe_customer_name(phone)
        client_names[phone] = sanitize_name_for_path(resolved)
        last_activity[phone] = datetime.datetime.now()
        if not await begin_session_with_opening(phone, client_names[phone], ph):
            return


async def _fire_coaching_message(phone: str, user_name: str, user_text: str) -> None:
    """Process one coaching turn. Called by the debounce timer with potentially combined user text."""
    await blooio_typing_on(phone)
    ph = phone_to_hash(phone)

    # Monthly cap (paid users only; free trial has its own 25-message hard stop)
    if await has_tanyatalk_access(phone) and not _is_bypass_phone(phone):
        count = await billing_db.get_monthly_message_count(ph)
        extra = await billing_db.get_extra_messages(ph)
        effective_cap = MONTHLY_MESSAGE_CAP + extra
        if count >= effective_cap:
            if count == effective_cap:
                await billing_db.increment_monthly_message_count(ph)
            if not awaiting_topup_confirmation.get(phone):
                awaiting_topup_confirmation[phone] = True
                await blooio_send_message(phone, MONTHLY_CAP_BLOCK_MESSAGE)
            return
        new_count = await billing_db.increment_monthly_message_count(ph)
        if new_count > MONTHLY_MESSAGE_CAP:
            await billing_db.consume_extra_message(ph)
        if new_count == MONTHLY_CAP_WARNING_AT:
            await blooio_send_message(phone, MONTHLY_CAP_WARNING_MESSAGE)

    lock = await _get_chat_message_lock(phone)
    async with lock:
        if await should_block_unpaid_after_free_trial(phone):
            if awaiting_stripe_confirmation.get(phone):
                intent = await classify_stripe_confirmation_intent(phone, user_name, user_text)
                if intent == "affirmative":
                    awaiting_stripe_confirmation.pop(phone, None)
                    if STRIPE_SECRET_KEY and STRIPE_SUBSCRIPTION_PRICE_ID:
                        checkout_url = None
                        for attempt in range(2):
                            try:
                                checkout_url = await create_subscription_checkout_url(phone)
                                break
                            except Exception as e:
                                if attempt == 0:
                                    await asyncio.sleep(2)
                                else:
                                    logger.error("Checkout URL failed after retry: %s", e)
                        if checkout_url:
                            await blooio_send_message(phone, f"Here you go. Come back whenever you are ready and we will pick up right where we left off.\n\n{checkout_url}")
                        else:
                            awaiting_stripe_confirmation[phone] = True
                            await blooio_send_message(phone, "Having a small tech hiccup. Text me back in a minute and I'll send you the link.")
                    elif STRIPE_PAYMENT_LINK:
                        await blooio_send_message(phone, f"Here you go. Come back whenever you are ready.\n\n{STRIPE_PAYMENT_LINK}")
                elif intent == "negative":
                    awaiting_stripe_confirmation.pop(phone, None)
                    await blooio_send_message(phone, FREE_TRIAL_STRIPE_DECLINED)
                else:
                    await blooio_send_message(phone, STRIPE_CONFIRMATION_UNCLEAR_REPLY)
            else:
                awaiting_stripe_confirmation[phone] = True
                await blooio_send_message(phone, POST_FREE_TRIAL_BLOCK_MESSAGE)
            return

        session_turn_anchor_time = asyncio.get_event_loop().time()

        if phone not in session_files:
            conversations[phone] = []
            if not await begin_session_with_opening(phone, user_name, ph):
                return
        elif phone not in conversations:
            conversations[phone] = []

        conversations[phone].append({"role": "user", "content": user_text})

        if len(conversations[phone]) > MAX_HISTORY * 2:
            conversations[phone] = conversations[phone][-(MAX_HISTORY * 2):]

        reset_timeout(phone)

        # Per-session message cap
        session_user_msgs = sum(1 for m in conversations[phone] if m["role"] == "user")
        if session_user_msgs >= SESSION_MESSAGE_CAP:
            await asyncio.to_thread(
                append_exchange, session_files[phone], user_name, user_text, SESSION_CAP_BLOCK_MESSAGE
            )
            await blooio_send_message(phone, SESSION_CAP_BLOCK_MESSAGE)
            cancel_session_timeout(phone)
            await end_session(phone)
            return
        if session_user_msgs == SESSION_CAP_WARNING_AT:
            await blooio_send_message(phone, SESSION_CAP_WARNING_MESSAGE)

        in_ft = await in_first_free_trial_session(phone)
        if in_ft and phone not in free_trial_user_msg_count:
            disk_count = await billing_db.get_trial_msg_count(ph)
            if disk_count > 0:
                free_trial_user_msg_count[phone] = disk_count
        prev_ft = free_trial_user_msg_count.get(phone, 0)
        n_ft = prev_ft + 1

        if in_ft and n_ft == FREE_TRIAL_90_PCT_USER_MESSAGE and not free_trial_90_warned.get(phone):
            free_trial_90_warned[phone] = True
            await blooio_send_message(phone, FREE_TRIAL_90_WARNING)

        archive_context = ""
        if any(sig in user_text.lower() for sig in ARCHIVE_REFERENCE_SIGNALS):
            client_name_for_archive = client_names.get(phone)
            if client_name_for_archive and phone in session_files:
                full_profile = await asyncio.to_thread(
                    load_file, profile_path_for(ph)
                )
                if full_profile:
                    archived_rows = parse_archived_sessions(full_profile)
                    if archived_rows:
                        matched = find_matching_archived_sessions(user_text, archived_rows)
                        if matched:
                            archive_context = await load_archive_context(matched)
                            logger.info(
                                "Archive retrieval: %d match(es) for %s",
                                len(matched), client_name_for_archive,
                            )

        if archive_context:
            conversations[phone][-1] = {
                "role": "user",
                "content": f"{archive_context}\n\n---\n\nClient message: {user_text}",
            }

        if in_ft and n_ft == FREE_TRIAL_USER_MESSAGE_CAP:
            conversations[phone].append({"role": "assistant", "content": FREE_TRIAL_CLOSE_TEXT})
            if len(conversations[phone]) > MAX_HISTORY * 2:
                conversations[phone] = conversations[phone][-(MAX_HISTORY * 2):]
            await asyncio.to_thread(
                append_exchange,
                session_files[phone],
                user_name,
                user_text,
                FREE_TRIAL_CLOSE_TEXT,
            )
            await blooio_send_message(phone, FREE_TRIAL_CLOSE_TEXT)
            await mark_free_trial_completed(phone)
            free_trial_user_msg_count[phone] = FREE_TRIAL_USER_MESSAGE_CAP
            await billing_db.delete_trial_msg_count(ph)
            awaiting_stripe_confirmation[phone] = True
            cancel_session_timeout(phone)
            await end_session(phone)
            return

        if _is_service_inquiry(user_text):
            await blooio_send_message(phone, SERVICE_INQUIRY_RESPONSE)
            if not pending_first_message_opener.get(phone):
                # Mid-session: answered, done. Don't generate a coaching response.
                return
            # New user: fall through so the vCard + contact prompt still fires normally.

        if pending_first_message_opener.get(phone):
            is_ret = await asyncio.to_thread(is_returning_client, ph)
            _profile_path = profile_path_for(ph)
            if _profile_path.exists() and not is_ret:
                logger.warning(
                    "Profile file exists but client not classified returning; vault markers may need review (phone_key=%s)",
                    ph[:12],
                )

            if not is_ret:
                if not awaiting_contact_save.get(phone):
                    # Claim slot, send vCard, then respond immediately.
                    if not await billing_db.try_claim_new_user_slot(ph, DAILY_NEW_USER_CAP):
                        cap_msg = build_daily_cap_message()
                        pending_first_message_opener.pop(phone, None)
                        cancel_session_timeout(phone)
                        await end_session(phone)
                        await blooio_send_message(phone, cap_msg)
                        logger.info("Daily new-user cap reached (%d). Blocked: %s", DAILY_NEW_USER_CAP, ph[:12])
                        return
                    await blooio_send_vcard(phone)
                    logger.info("vCard sent; generating opener for first message from %s", ph[:12])

                awaiting_contact_save.pop(phone, None)

                if _is_minimal_opener(user_text):
                    opener_script = NEW_CLIENT_MINIMAL_OPENER
                else:
                    opener_script = await generate_new_client_coaching_opener(user_text)

                # Commit state BEFORE sending — if SIGTERM fires mid-send the snapshot
                # already shows the opener as done, preventing a re-send on restore.
                pending_first_message_opener.pop(phone, None)
                conversations[phone].append({"role": "assistant", "content": opener_script})
                if len(conversations[phone]) > MAX_HISTORY * 2:
                    conversations[phone] = conversations[phone][-(MAX_HISTORY * 2):]
                await asyncio.to_thread(
                    append_exchange,
                    session_files[phone],
                    user_name,
                    user_text,
                    opener_script,
                )
                if in_ft:
                    free_trial_user_msg_count[phone] = n_ft
                    await billing_db.save_trial_msg_count(ph, n_ft)

                elapsed_since_anchor = asyncio.get_event_loop().time() - session_turn_anchor_time
                remaining_open = RESPONSE_DELAY_SECONDS - elapsed_since_anchor
                if remaining_open > 0:
                    await asyncio.sleep(remaining_open)
                await blooio_send_message(phone, opener_script)
                logger.info("New client opener sent for %s", user_name)
                return

            # Resend vCard on session 2 only — once is a reminder, more is annoying.
            if session_numbers.get(phone, 1) == 2:
                await blooio_send_vcard(phone)
                await blooio_send_message(phone, CONTACT_SAVE_PROMPT_RETURNING)

            if _is_minimal_opener(user_text):
                opener_script = RETURNING_CLIENT_MINIMAL_OPENER
            else:
                opener_script = await generate_new_client_coaching_opener(user_text)

            elapsed_open = asyncio.get_event_loop().time() - session_turn_anchor_time
            remaining_open = RESPONSE_DELAY_SECONDS - elapsed_open
            if remaining_open > 0:
                await asyncio.sleep(remaining_open)

            pending_first_message_opener.pop(phone, None)
            conversations[phone].append({"role": "assistant", "content": opener_script})
            if len(conversations[phone]) > MAX_HISTORY * 2:
                conversations[phone] = conversations[phone][-(MAX_HISTORY * 2):]
            await asyncio.to_thread(
                append_exchange,
                session_files[phone],
                user_name,
                user_text,
                opener_script,
            )
            if in_ft:
                free_trial_user_msg_count[phone] = n_ft
                await billing_db.save_trial_msg_count(ph, n_ft)
            await blooio_send_message(phone, opener_script)
            logger.info("Returning client opener sent for %s", user_name)
            return

        message_received_at = asyncio.get_event_loop().time()

        client_profile = session_profiles.get(phone, "")
        phase_line = becoming_you_phase_for_prompt(client_profile)
        static_prompt = build_static_prompt(
            client_profile, session_outlines.get(phone, ""), phase_line
        )
        _cached_static_prompts[phone] = static_prompt
        start_cache_warming(phone)

        relevant_frameworks = await select_frameworks_for_session(
            session_profiles.get(phone, ""),
            user_text,
            conversations[phone],
        )
        framework_context = (
            await asyncio.to_thread(load_frameworks, relevant_frameworks)
            if relevant_frameworks
            else ""
        )

        system_blocks: list[dict] = [
            {"type": "text", "text": static_prompt, "cache_control": {"type": "ephemeral"}},
        ]
        if framework_context:
            system_blocks.append(
                {"type": "text", "text": f"\n\n---\n\n## Relevant Frameworks\n\n{framework_context}"}
            )

        profile_client = client_names.get(phone, user_name)
        if referral_nudge_prompt_allowed(phone, profile_client):
            system_blocks.append({"type": "text", "text": referral_nudge_system_instruction()})

        logger.info(
            "Message from %s | Session %d | Profile: %s | Frameworks: %s | Static: %d chars",
            user_name,
            session_numbers.get(phone, 0),
            "loaded" if client_profile else "none",
            relevant_frameworks or "none",
            len(static_prompt),
        )

        try:
            response = await _claude_create(
                model=CLAUDE_MODEL,
                max_tokens=1024,
                system=system_blocks,
                messages=conversations[phone][-20:],
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )
            raw_reply = response.content[0].text
            reply, referral_marked = strip_referral_nudge_marker(raw_reply)
            reply = reply.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ").replace("–", ", ").replace(" - ", ", ").replace(" ,", ",")
            await asyncio.to_thread(record_coaching_usage, phone, user_name, response)
            if referral_marked:
                referral_nudge_used_this_session[phone] = True
                referral_record_nudge(profile_client, session_numbers.get(phone, 0))
                logger.info(
                    "Referral nudge recorded for %s session %d",
                    profile_client,
                    session_numbers.get(phone, 0),
                )
        except Exception as e:
            logger.error("Anthropic API error: %s", e)
            reply = "I'm having a little trouble right now. Give me a moment and try again."

        conversations[phone].append({"role": "assistant", "content": reply})

        await asyncio.to_thread(
            append_exchange, session_files[phone], user_name, user_text, reply
        )

        elapsed = asyncio.get_event_loop().time() - message_received_at
        remaining = RESPONSE_DELAY_SECONDS - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

        await blooio_send_message(phone, reply)

        if in_ft:
            free_trial_user_msg_count[phone] = n_ft
            await billing_db.save_trial_msg_count(ph, n_ft)


async def _handle_at_monthly_cap(phone: str, user_name: str, user_text: str) -> bool:
    """Top-up gate when out of messages. Returns True if handled (caller should return)."""
    if not await _is_at_monthly_message_cap(phone):
        awaiting_topup_confirmation.pop(phone, None)
        topup_link_sent.pop(phone, None)
        return False

    tanya_followup.on_user_message_cancel_followup(phone)
    if tanya_followup.in_mini_session(phone):
        tanya_followup.clear_mini(phone)

    if is_session_end_message(user_text):
        existing = _debounce_tasks.pop(phone, None)
        if existing and not existing.done():
            existing.cancel()
        existing_typing = _typing_tasks.pop(phone, None)
        if existing_typing and not existing_typing.done():
            existing_typing.cancel()
        _pending_messages.pop(phone, None)
        _pending_phones.pop(phone, None)
        await perform_session_close(phone, user_text)
        return True

    user_text_lower = user_text.strip().lower()
    if any(t in user_text_lower for t in CANCEL_TRIGGERS):
        portal_url = await create_stripe_portal_url(phone) or STRIPE_PORTAL_LINK
        if portal_url:
            await blooio_send_message(
                phone,
                CANCEL_MESSAGE_WITH_LINK.format(portal_link=portal_url),
            )
        else:
            await blooio_send_message(
                phone,
                "To cancel, just reply here and I'll help you sort it out.",
            )
        return True

    if not awaiting_topup_confirmation.get(phone):
        awaiting_topup_confirmation[phone] = True
        await blooio_typing_on(phone)
        await blooio_send_message(phone, MONTHLY_CAP_BLOCK_MESSAGE)
        return True

    await blooio_typing_on(phone)
    intent = await classify_topup_confirmation_intent(
        user_text,
        allow_haiku=not topup_link_sent.get(phone),
    )
    if intent == "affirmative":
        checkout_url = None
        for attempt in range(2):
            try:
                checkout_url = await create_topup_checkout_url(phone)
                break
            except Exception as e:
                if attempt == 0:
                    await asyncio.sleep(2)
                else:
                    logger.error("Top-up checkout URL failed after retry: %s", e)
        if checkout_url:
            topup_link_sent[phone] = True
            await blooio_send_message(
                phone,
                f"Here you go. Each $5 adds 60 messages and you can add as many as you'd like.\n\n{checkout_url}",
            )
        else:
            await blooio_send_message(
                phone,
                "Having a small tech hiccup. Text me back in a minute and I'll send you the link.",
            )
    elif intent == "negative":
        await blooio_send_message(phone, TOPUP_LINK_DECLINED)
    else:
        reply = TOPUP_UNCLEAR_REPLY
        if topup_link_sent.get(phone):
            reply = (
                "Your top-up link is above. Complete checkout there to add messages, "
                "or say no if you'd like to wait until your subscription refills."
            )
        await blooio_send_message(phone, reply)
    return True


async def handle_inbound_message(phone: str, user_text: str) -> None:
    """Route one inbound iMessage from phone through the full Tanya logic."""
    phone = normalize_phone(phone)
    if re.search(r'https?://\S+|www\.\S+', user_text, re.IGNORECASE):
        await blooio_send_message(phone, LINK_RESPONSE)
        return

    # Resolve client name from Stripe (cached after first lookup)
    if phone not in client_names:
        resolved = await get_stripe_customer_name(phone)
        client_names[phone] = resolved
    user_name = client_names[phone]
    last_activity[phone] = datetime.datetime.now()

    # Hard stop: no coaching, mini-session, or billing AI while out of monthly messages.
    if await _handle_at_monthly_cap(phone, user_name, user_text):
        return

    # Cancel any pending follow-up jobs; enter mini-session if in post-FU window
    in_post_fu = tanya_followup.on_user_message_cancel_followup(phone)
    if in_post_fu:
        tanya_followup.enter_mini_session_after_fu1(phone, None)
    if tanya_followup.in_mini_session(phone):
        lock = await _get_chat_message_lock(phone)
        async with lock:
            await tanya_followup.handle_mini_session_turn(phone, user_text)
        return

    if awaiting_stripe_confirmation.get(phone):
        await blooio_typing_on(phone)
        lock = await _get_chat_message_lock(phone)
        async with lock:
            intent = await classify_stripe_confirmation_intent(phone, user_name, user_text)
            if intent == "affirmative":
                awaiting_stripe_confirmation.pop(phone, None)
                if STRIPE_SECRET_KEY and STRIPE_SUBSCRIPTION_PRICE_ID:
                    checkout_url = None
                    for attempt in range(2):
                        try:
                            checkout_url = await create_subscription_checkout_url(phone)
                            break
                        except Exception as e:
                            if attempt == 0:
                                await asyncio.sleep(2)
                            else:
                                logger.error("Subscription checkout URL failed after retry: %s", e)
                    if checkout_url:
                        await blooio_send_message(
                            phone,
                            f"Here you go. Come back whenever you are ready and we will pick up right where we left off.\n\n{checkout_url}",
                        )
                    else:
                        awaiting_stripe_confirmation[phone] = True
                        await blooio_send_message(phone, "Having a small tech hiccup. Text me back in a minute and I'll send you the link.")
                elif STRIPE_PAYMENT_LINK:
                    await blooio_send_message(
                        phone,
                        f"Here you go. Come back whenever you are ready.\n\n{STRIPE_PAYMENT_LINK}",
                    )
            elif intent == "negative":
                awaiting_stripe_confirmation.pop(phone, None)
                await blooio_send_message(phone, FREE_TRIAL_STRIPE_DECLINED)
            else:
                await blooio_send_message(phone, STRIPE_CONFIRMATION_UNCLEAR_REPLY)
        return

    # Delete confirmation: client already triggered the delete flow, waiting on yes/no.
    if awaiting_delete_confirmation.get(phone):
        await blooio_typing_on(phone)
        intent = await classify_delete_confirmation_intent(user_text)
        if intent == "affirmative":
            awaiting_delete_confirmation.pop(phone, None)
            portal_url = await create_stripe_portal_url(phone)
            await delete_client_data(phone)
            await blooio_send_message(phone, DELETE_CONFIRMED_MESSAGE)
            if portal_url:
                await blooio_send_message(
                    phone,
                    "One more thing. Your billing is still active. Use the link below to cancel "
                    "your subscription. You'll keep access through the end of your current billing "
                    "period and won't be charged again after that.\n\n"
                    f"{portal_url}",
                )
        elif intent == "negative":
            awaiting_delete_confirmation.pop(phone, None)
            await blooio_send_message(phone, DELETE_CANCELLED_MESSAGE)
        else:
            await blooio_send_message(phone, "Just want to make sure — do you want me to delete everything?")
        return

    # Session end check runs first — before cancel/delete AI — so "done session" etc.
    # never reaches the cancellation classifier.
    if is_session_end_message(user_text):
        existing = _debounce_tasks.pop(phone, None)
        if existing and not existing.done():
            existing.cancel()
        existing_typing = _typing_tasks.pop(phone, None)
        if existing_typing and not existing_typing.done():
            existing_typing.cancel()
        _pending_messages.pop(phone, None)
        _pending_phones.pop(phone, None)
        await perform_session_close(phone, user_text)
        return

    # Cancel / delete triggers — phrase match first (free), then parallel AI fallback.
    await blooio_typing_on(phone)
    user_text_lower = user_text.strip().lower()
    cancel_phrase = any(t in user_text_lower for t in CANCEL_TRIGGERS)
    delete_phrase = any(t in user_text_lower for t in DELETE_TRIGGERS)

    if not cancel_phrase and not delete_phrase:
        cancel_intent, delete_intent = await asyncio.gather(
            ai_detects_cancel_intent(user_text),
            ai_detects_delete_intent(user_text),
        )
    else:
        cancel_intent, delete_intent = cancel_phrase, delete_phrase

    if cancel_intent:
        if await has_tanyatalk_access(phone):
            portal_url = await create_stripe_portal_url(phone) or STRIPE_PORTAL_LINK
            if portal_url:
                msg = CANCEL_MESSAGE_WITH_LINK.format(portal_link=portal_url)
                await blooio_send_message(phone, msg)
            else:
                await blooio_send_message(phone, "To cancel, just reply here and I'll help you sort it out.")
        else:
            await blooio_send_message(
                phone,
                "You're still in your free trial, so there's nothing to cancel yet. "
                "If you ever subscribe and want to stop, just let me know.",
            )
        return

    if delete_intent:
        existing = _debounce_tasks.pop(phone, None)
        if existing and not existing.done():
            existing.cancel()
        _pending_messages.pop(phone, None)
        _pending_phones.pop(phone, None)
        awaiting_delete_confirmation[phone] = True
        prompt = (
            DELETE_CONFIRMATION_PROMPT
            if await has_tanyatalk_access(phone)
            else DELETE_CONFIRMATION_PROMPT_TRIAL
        )
        await blooio_send_message(phone, prompt)
        return

    # Debounce: buffer this message and wait for more before firing to Claude.
    _pending_messages.setdefault(phone, []).append(user_text)
    _pending_phones[phone] = phone

    existing = _debounce_tasks.pop(phone, None)
    if existing and not existing.done():
        existing.cancel()

    existing_typing = _typing_tasks.pop(phone, None)
    if existing_typing and not existing_typing.done():
        existing_typing.cancel()

    async def _show_typing_soon() -> None:
        try:
            await asyncio.sleep(TYPING_BUBBLE_DELAY_SEC)
            await blooio_typing_on(phone)
        except asyncio.CancelledError:
            pass

    _typing_tasks[phone] = asyncio.ensure_future(_show_typing_soon())

    async def _fire() -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        _debounce_tasks.pop(phone, None)
        msgs = _pending_messages.pop(phone, [])
        _pending_phones.pop(phone, None)
        if msgs:
            combined = "\n\n".join(msgs)
            await _fire_coaching_message(phone, user_name, combined)

    _debounce_tasks[phone] = asyncio.ensure_future(_fire())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def create_topup_checkout_url(phone: str) -> str:
    import stripe as stripe_lib
    stripe_lib.api_key = STRIPE_SECRET_KEY
    session = await asyncio.to_thread(
        stripe_lib.checkout.Session.create,
        line_items=[{
            "price": STRIPE_TOPUP_PRICE_ID,
            "quantity": 1,
            "adjustable_quantity": {"enabled": True, "minimum": 1, "maximum": 99},
        }],
        mode="payment",
        phone_number_collection={"enabled": True},
        name_collection={"individual": {"enabled": True}},
        metadata={"phone": phone},
        success_url="https://stripe.com",
        cancel_url="https://stripe.com",
    )
    return session.url


async def create_subscription_checkout_url(phone: str) -> str:
    import stripe as stripe_lib
    stripe_lib.api_key = STRIPE_SECRET_KEY
    session = await asyncio.to_thread(
        stripe_lib.checkout.Session.create,
        line_items=[{"price": STRIPE_SUBSCRIPTION_PRICE_ID, "quantity": 1}],
        mode="subscription",
        allow_promotion_codes=True,
        phone_number_collection={"enabled": True},
        name_collection={"individual": {"enabled": True}},
        metadata={"phone": phone, "product_type": "subscription"},
        success_url="https://stripe.com",
        cancel_url="https://stripe.com",
    )
    return session.url


async def _resolve_phone_from_stripe_customer(customer_id: str) -> str:
    """Load canonical phone from Stripe customer metadata."""
    if not customer_id:
        return ""
    import stripe as stripe_lib
    try:
        stripe_lib.api_key = STRIPE_SECRET_KEY
        customer = await asyncio.to_thread(stripe_lib.Customer.retrieve, customer_id)
        phone = ((customer.get("metadata") or {}).get("phone") or "").strip()
        if phone:
            return normalize_phone(phone)
    except Exception as e:
        logger.error("Could not retrieve Stripe customer %s for phone lookup: %s", customer_id, e)
    return ""


async def _resolve_stripe_checkout_phone(session_obj: dict) -> str:
    """Resolve canonical E.164 phone from checkout session metadata, customer, or collected details."""
    metadata = session_obj.get("metadata") or {}
    phone = (metadata.get("phone") or "").strip()
    if phone:
        return normalize_phone(phone)
    phone = await _resolve_phone_from_stripe_customer(session_obj.get("customer") or "")
    if phone:
        return phone
    customer_details = session_obj.get("customer_details") or {}
    details_phone = (customer_details.get("phone") or "").strip()
    if details_phone:
        return normalize_phone(details_phone)
    return ""


async def handle_stripe_webhook(request: Request) -> Response:
    import stripe as stripe_lib
    import json as _json
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        stripe_lib.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        logger.warning("Stripe webhook verification failed: %s", e)
        return Response(status_code=400)
    event = _json.loads(payload)  # plain dict — StripeObject.get() is unreliable across SDK versions

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        metadata = session_obj.get("metadata") or {}
        product_type = metadata.get("product_type", "topup")
        phone = await _resolve_stripe_checkout_phone(session_obj)
        if not phone:
            logger.error(
                "checkout.session.completed: could not resolve phone (session=%s) — returning 500 for Stripe retry",
                session_obj.get("id", "?"),
            )
            return Response(status_code=500)
        try:
            if product_type == "subscription":
                # Access control first — if this fails, return 500 so Stripe retries
                ph = phone_to_hash(phone)
                await billing_db.grant_access(ph)
                paid_tanyatalk_access[phone] = True  # update memory cache immediately
                awaiting_stripe_confirmation.pop(phone, None)  # clear gate so next message routes normally
                await billing_db.record_subscription_start(ph)
                customer_id = session_obj.get("customer", "")
                if customer_id:
                    await billing_db.store_stripe_customer_id(ph, customer_id)
                    try:
                        stripe_lib.api_key = STRIPE_SECRET_KEY
                        await asyncio.to_thread(
                            stripe_lib.Customer.modify,
                            customer_id,
                            metadata={"phone": phone},
                        )
                    except Exception as e:
                        logger.warning("Could not set customer metadata.phone: %s", e)
                logger.info("Subscription granted for phone_key=%s", _phone_key(phone))
                try:
                    await blooio_send_message(
                        phone,
                        "You're in. Your subscription is active and your 250 messages are ready. "
                        "Come back whenever and we will pick up right where we left off.",
                    )
                except Exception as e:
                    logger.error("Subscription welcome message failed (access already granted): %s", e)
            else:
                stripe_lib.api_key = STRIPE_SECRET_KEY
                session_expanded = await asyncio.to_thread(
                    stripe_lib.checkout.Session.retrieve,
                    session_obj["id"],
                    expand=["line_items"],
                )
                qty = session_expanded.line_items.data[0].quantity if session_expanded.line_items.data else 1
                # Credits first — if this fails, return 500 so Stripe retries
                total_extra = await billing_db.add_extra_messages(phone_to_hash(phone), qty * 60)
                awaiting_topup_confirmation.pop(phone, None)
                topup_link_sent.pop(phone, None)
                logger.info(
                    "Top-up: added %d bonus messages to phone_key=%s (total bonus now %d)",
                    qty * 60,
                    _phone_key(phone),
                    total_extra,
                )
                try:
                    await blooio_send_message(phone, TOPUP_CREDITED_MESSAGE)
                except Exception as e:
                    logger.error("Top-up confirmation message failed (credits already added): %s", e)
        except Exception as e:
            logger.error("Stripe checkout.session.completed processing failed: %s", e)
            return Response(status_code=500)

    elif event["type"] == "customer.subscription.deleted":
        sub_obj = event["data"]["object"]
        metadata = sub_obj.get("metadata") or {}
        phone = (metadata.get("phone") or "").strip()
        if phone:
            phone = normalize_phone(phone)
        else:
            phone = await _resolve_phone_from_stripe_customer(sub_obj.get("customer") or "")
        if not phone:
            logger.warning("subscription.deleted ignored — could not resolve phone from metadata or customer")
            return Response(status_code=200)
        try:
            await billing_db.revoke_access(phone_to_hash(phone))
            paid_tanyatalk_access.pop(phone, None)  # clear memory cache immediately
            tanya_followup.cancel_all_followup_jobs_for_chat(phone)
            logger.info("Access revoked for phone_key=%s via subscription.deleted", _phone_key(phone))
        except Exception as e:
            logger.error("Access revocation failed: %s", e)
            return Response(status_code=500)
        try:
            await blooio_send_message(
                phone,
                "Your subscription has ended. Your session history is saved and I'll be here if you decide to come back.",
            )
        except Exception as e:
            logger.error("Subscription ended message failed (access already revoked): %s", e)

    elif event["type"] == "invoice.payment_failed":
        invoice_obj = event["data"]["object"]
        metadata = invoice_obj.get("metadata") or {}
        phone = (metadata.get("phone") or "").strip()
        if phone:
            phone = normalize_phone(phone)
        else:
            phone = await _resolve_phone_from_stripe_customer(invoice_obj.get("customer") or "")
        if phone:
            try:
                await blooio_send_message(
                    phone,
                    "Hey, just a heads up — your payment didn't go through this month. "
                    "Update your card through your billing portal to keep your access.",
                )
            except Exception as e:
                logger.error("Payment failed notification error: %s", e)
                # Intentionally return 200 — notification failure is non-critical; don't trigger Stripe retry storms.

    return Response(status_code=200)  # invoice.payment_failed also exits here — 200 is intentional (see above)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    await _startup()
    yield
    await _shutdown()


_fastapi_app = FastAPI(lifespan=_lifespan)
_fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.tanya-talk.com", "https://tanya-talk.com"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@_fastapi_app.get("/health")
async def health() -> Response:
    return Response(content="ok", status_code=200)


@_fastapi_app.post("/report-error")
async def report_error(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)
    description = (body.get("description") or "").strip()
    phone = (body.get("phone") or "").strip()
    if not description:
        return Response(status_code=400)
    if not GITHUB_ISSUES_PAT:
        logger.warning("GITHUB_ISSUES_PAT not set — error report dropped")
        return Response(status_code=500)
    title = description[:72] + ("…" if len(description) > 72 else "")
    body_md = f"**Phone:** {phone}\n\n**Description:**\n{description}" if phone else f"**Description:**\n{description}"
    try:
        resp = await _http.post(
            "https://api.github.com/repos/cole-projects/tanya-landing/issues",
            headers={
                "Authorization": f"Bearer {GITHUB_ISSUES_PAT}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "body": body_md, "labels": ["user-report"]},
            timeout=10,
        )
        if resp.status_code == 201:
            logger.info("Error report → GitHub issue #%s", resp.json().get("number"))
            return Response(status_code=200)
        logger.warning("GitHub issue creation failed %d: %s", resp.status_code, resp.text[:200])
        return Response(status_code=500)
    except Exception as e:
        logger.warning("Error report request failed: %s", e)
        return Response(status_code=500)


@_fastapi_app.get("/audio/{filename}")
async def serve_audio(filename: str) -> Response:
    if "/" in filename or ".." in filename or not filename.endswith(".mp3"):
        return Response(status_code=404)
    audio_path = AUDIO_DIR / filename
    if not audio_path.exists():
        return Response(status_code=404)
    content = audio_path.read_bytes()
    return Response(content=content, media_type="audio/mpeg")


@_fastapi_app.api_route("/tanya.vcf", methods=["GET", "HEAD"])
async def serve_vcard(request: Request) -> Response:
    return Response(
        content=None if request.method == "HEAD" else TANYA_VCARD,
        media_type="text/vcard",
        headers={"Content-Disposition": 'attachment; filename="TanyaTalk.vcf"'},
    )


@_fastapi_app.get("/admin/usage-csv")
async def admin_usage_csv(request: Request) -> Response:
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not ADMIN_KEY or token != ADMIN_KEY:
        return Response(status_code=401)
    if not USAGE_CSV_PATH.exists():
        return Response(content="no data yet", status_code=404)
    content = await asyncio.to_thread(USAGE_CSV_PATH.read_bytes)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tanya_usage.csv"},
    )


@_fastapi_app.post("/webhook")
async def blooio_webhook_endpoint(request: Request) -> Response:
    raw_body = await request.body()
    logger.info("Blooio webhook received: %d bytes", len(raw_body))
    sig = request.headers.get("X-Blooio-Signature", "")
    if not verify_blooio_signature(raw_body, sig):
        logger.warning("Blooio webhook signature verification failed (sig=%s)", sig[:40] if sig else "none")
        return Response(status_code=401)
    try:
        payload = json.loads(raw_body)
    except Exception:
        return Response(status_code=400)
    event_type = request.headers.get("x-blooio-event", "")
    if event_type and event_type != "message.received":
        if event_type == "message.failed":
            error_code = payload.get("error_code")
            message_id = payload.get("message_id") or payload.get("id")
            logger.error(
                "Blooio message.failed — message_id=%s error_code=%s error_message=%s to=%s payload=%s",
                message_id,
                error_code,
                payload.get("error_message"),
                payload.get("to") or payload.get("recipient"),
                json.dumps(payload),
            )
            if error_code == "device_unreachable":
                retry_phone = payload.get("external_id")
                retry_text = payload.get("text")
                if retry_phone and retry_text and message_id:
                    logger.info("Scheduling retry for device_unreachable message_id=%s in 15s", message_id)
                    asyncio.create_task(_retry_device_unreachable(retry_phone, retry_text, message_id))
        else:
            logger.info("Blooio webhook event=%s — ignored (not message.received)", event_type)
        return Response(status_code=200)
    phone = payload.get("sender") or payload.get("from", "")
    text = payload.get("text", "")
    is_group = payload.get("is_group", False)
    logger.info("Webhook payload: phone_key=%s text_len=%s is_group=%s keys=%s", _phone_key(phone) if phone else "", len(text) if text else 0, is_group, list(payload.keys()))
    if payload.get("error_code") or payload.get("error_message"):
        logger.warning("Blooio delivery error: error_code=%s error_message=%s message_id=%s", payload.get("error_code"), payload.get("error_message"), payload.get("message_id"))
        return Response(status_code=200)
    if phone and not is_group:
        if text:
            asyncio.ensure_future(handle_inbound_message(phone, text))
        else:
            # No text — voice note or media attachment. Route through session logic
            # so the system prompt's voice note redirect fires in the right context.
            asyncio.ensure_future(handle_inbound_message(phone, "[voice note]"))
    return Response(status_code=200)


@_fastapi_app.post("/stripe/webhook")
async def stripe_webhook_endpoint(request: Request) -> Response:
    return await handle_stripe_webhook(request)


def _write_session_snapshot() -> None:
    """Write all live session state to disk before shutdown so it survives a redeploy."""
    try:
        data: dict = {
            "conversations": conversations,
            "session_files": {p: str(v) for p, v in session_files.items()},
            "session_numbers": session_numbers,
            "client_names": client_names,
            "last_activity": {p: v.isoformat() for p, v in last_activity.items()},
            "awaiting_stripe_confirmation": awaiting_stripe_confirmation,
            "awaiting_topup_confirmation": awaiting_topup_confirmation,
            "topup_link_sent": topup_link_sent,
            "awaiting_delete_confirmation": awaiting_delete_confirmation,
            "awaiting_contact_save": awaiting_contact_save,
            "pending_first_message_opener": pending_first_message_opener,
            "free_trial_user_msg_count": free_trial_user_msg_count,
            "free_trial_90_warned": free_trial_90_warned,
            "referral_nudge_used_this_session": referral_nudge_used_this_session,
        }
        SESSION_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SESSION_SNAPSHOT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Snapshot written: %d active sessions", len(session_files))
    except Exception as e:
        logger.error("Failed to write session snapshot: %s", e)


def _restore_session_snapshot() -> None:
    """Restore in-memory state from the snapshot written before last shutdown."""
    if not SESSION_SNAPSHOT_PATH.exists():
        return
    try:
        data = json.loads(SESSION_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        cutoff = datetime.datetime.now() - datetime.timedelta(minutes=SESSION_TIMEOUT_MINUTES)

        # Restore last_activity first so we can filter stale sessions
        raw_activity: dict[str, str] = data.get("last_activity", {})
        restored_activity: dict[str, datetime.datetime] = {}
        for phone, iso in raw_activity.items():
            try:
                restored_activity[phone] = datetime.datetime.fromisoformat(iso)
            except ValueError:
                pass

        # Only restore sessions that haven't timed out while the server was down
        active_phones = {
            p for p, t in restored_activity.items() if t >= cutoff
        }

        raw_files: dict[str, str] = data.get("session_files", {})
        for phone in active_phones:
            if phone in raw_files:
                p = Path(raw_files[phone])
                if p.exists():
                    session_files[phone] = p
                else:
                    # File was renamed while a session was open — scan for any unclosed whole session
                    ph = phone_to_hash(phone)
                    session_dir = Path(VAULT_PATH) / "02-Client-Sessions" / ph
                    candidates = sorted(
                        _whole_session_files(session_dir),
                        key=lambda f: int(_WHOLE_SESSION_FILE_RE.fullmatch(f.name).group(1)),
                    )
                    for f in candidates:
                        try:
                            if "<!-- session:closed -->" not in f.read_text(encoding="utf-8"):
                                session_files[phone] = f
                                m = _WHOLE_SESSION_FILE_RE.fullmatch(f.name)
                                if m:
                                    session_numbers[phone] = int(m.group(1))
                                logger.info(
                                    "Snapshot recovery: stale path %s → found open session %s",
                                    p.name, f.name,
                                )
                                break
                        except OSError:
                            pass

        raw_convs: dict = data.get("conversations", {})
        for phone in active_phones:
            if phone in raw_convs:
                conversations[phone] = raw_convs[phone]

        for phone in active_phones:
            if phone in restored_activity:
                last_activity[phone] = restored_activity[phone]

        for src, dst in (
            ("session_numbers", session_numbers),
            ("client_names", client_names),
            ("free_trial_user_msg_count", free_trial_user_msg_count),
            ("free_trial_90_warned", free_trial_90_warned),
            ("referral_nudge_used_this_session", referral_nudge_used_this_session),
        ):
            for phone, val in data.get(src, {}).items():
                if phone in active_phones:
                    dst[phone] = val  # type: ignore[index]

        for src, dst in (
            ("awaiting_stripe_confirmation", awaiting_stripe_confirmation),
            ("awaiting_topup_confirmation", awaiting_topup_confirmation),
            ("topup_link_sent", topup_link_sent),
            ("awaiting_delete_confirmation", awaiting_delete_confirmation),
            ("awaiting_contact_save", awaiting_contact_save),
        ):
            for phone, val in data.get(src, {}).items():
                if phone in active_phones:
                    dst[phone] = val  # type: ignore[index]

        # Restore pending_first_message_opener only if the opener wasn't already committed
        # to conversation history before the snapshot was taken.
        for phone, val in data.get("pending_first_message_opener", {}).items():
            if phone not in active_phones:
                continue
            if val and any(m.get("role") == "assistant" for m in conversations.get(phone, [])):
                logger.info("Skipping pending_first_message_opener restore for hash %s — already in conversation", phone_to_hash(phone)[:12])
            else:
                pending_first_message_opener[phone] = val

        # Reload session profile and outline from disk for every active session.
        # Profile: returning clients have one on disk; new clients (Session 1) don't yet.
        # Outline: always reloaded — same file for every client, always fresh from disk.
        profiles_loaded = 0
        profiles_skipped = 0
        outlines_loaded = 0
        outline_cache: str | None = None  # load once, reuse for all sessions
        for phone in active_phones:
            if phone in session_files:
                ph = phone_to_hash(phone)
                try:
                    profile = load_client_profile(ph)
                    if profile:
                        session_profiles[phone] = profile
                        profiles_loaded += 1
                    else:
                        profiles_skipped += 1  # new client, no profile yet
                except Exception:
                    profiles_skipped += 1
                try:
                    if outline_cache is None:
                        outline_cache = load_session_outline()
                    if outline_cache:
                        session_outlines[phone] = outline_cache
                        outlines_loaded += 1
                except Exception:
                    pass

        for phone in active_phones:
            if phone not in session_files:
                continue
            la = last_activity.get(phone)
            if la is not None:
                remaining = SESSION_TIMEOUT_MINUTES * 60 - (
                    datetime.datetime.now() - la
                ).total_seconds()
                if remaining > 0:
                    reset_timeout(phone, delay_seconds=remaining)
            else:
                reset_timeout(phone)

        logger.info(
            "Snapshot restore: profiles reloaded=%d (skipped=%d new clients), outlines reloaded=%d",
            profiles_loaded, profiles_skipped, outlines_loaded,
        )

        SESSION_SNAPSHOT_PATH.unlink()
        logger.info(
            "Snapshot restored: %d active sessions (%d timed out while down)",
            len(active_phones),
            len(raw_activity) - len(active_phones),
        )
    except Exception as e:
        logger.error("Failed to restore session snapshot: %s", e)
        try:
            SESSION_SNAPSHOT_PATH.unlink(missing_ok=True)
        except Exception:
            pass


async def _startup() -> None:
    billing_db.configure(_BOT_DIR / "logs" / "billing.db")
    await billing_db.init_db()
    await billing_db.migrate_from_json(_BOT_DIR / "logs")
    vault = Path(VAULT_PATH)
    if (vault / ".git").exists() and await asyncio.to_thread(_vault_has_unpushed_work, vault):
        mark_vault_dirty()
    _restore_session_snapshot()
    init_usage_csv_file()
    for _mp in _MESH_PHONES:
        mesh_tanyatalk_included[normalize_phone(_mp)] = True
    tanya_followup.init_scheduler(_BOT_DIR / "logs" / "apscheduler.sqlite")
    tanya_followup.configure(
        claude=claude,
        claude_model=CLAUDE_MODEL,
        claude_haiku_model=CLAUDE_HAIKU_MODEL,
        send_message=blooio_send_message,
        merge_focus_for_next_session=merge_focus_for_next_session_profile,
        open_coaching_session=open_coaching_session_after_mini,
        check_monthly_cap=_check_monthly_cap_for_followup,
        typing_on=blooio_typing_on,
    )
    await tanya_followup.start_scheduler()
    await _process_pending_finalizes()
    asyncio.ensure_future(_blooio_failure_poll_loop())
    asyncio.ensure_future(_vault_push_loop())
    asyncio.ensure_future(_audio_cleanup_loop())
    await asyncio.to_thread(_cleanup_stale_audio_files)
    logger.info("Tanya Talk iMessage server started")


async def _shutdown() -> None:
    if _finalize_tasks:
        _done, pending = await asyncio.wait(_finalize_tasks, timeout=45)
        if pending:
            logger.warning(
                "%d session finalize task(s) still running at shutdown; queued jobs will retry on next start",
                len(pending),
            )
    _write_session_snapshot()
    if _vault_dirty and GITHUB_PAT:
        vault = Path(VAULT_PATH)
        if (vault / ".git").exists():
            with _VAULT_GIT_LOCK:
                if not _do_vault_push():
                    mark_vault_dirty()
                elif _vault_has_unpushed_work(vault):
                    mark_vault_dirty()
    await tanya_followup.shutdown_scheduler()
    await _http.aclose()


def main() -> None:
    acquire_single_instance_lock()
    try:
        port = int(os.getenv("PORT", "8080"))
        uvicorn.run(_fastapi_app, host="0.0.0.0", port=port, log_level="info")
    finally:
        release_single_instance_lock()


if __name__ == "__main__":
    main()
