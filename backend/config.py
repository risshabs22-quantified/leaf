"""All runtime settings come from environment variables (see .env.example).

Model IDs live here and nowhere else. Change routing by editing this file or env vars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

GLOBAL_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
US_CENTRAL_BASE_URL = "https://api.tokenfactory.us-central1.nebius.com/v1/"

# NVIDIA models on Nebius Token Factory (IDs checked against Nebius docs, Oct 2026).
NEMOTRON_ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
NEMOTRON_SUPER = "nvidia/nemotron-3-super-120b-a12b"
NEMOTRON_LIGHTNING = "nvidia/Nemotron-3_5-Lightning"
NEMOTRON_NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"

# Fallback prices in dollars per token (input, output), from the Token Factory catalog,
# Oct 2026. At startup the live prices from GET /models replace these.
DEFAULT_PRICES_PER_TOKEN = {
    NEMOTRON_ULTRA: (1.00e-6, 3.00e-6),
    NEMOTRON_SUPER: (0.30e-6, 0.90e-6),
    NEMOTRON_LIGHTNING: (0.06e-6, 0.24e-6),
    NEMOTRON_NANO: (0.06e-6, 0.24e-6),
    "moonshotai/Kimi-K2.6": (0.95e-6, 4.00e-6),
}

# Token Factory has no public NVIDIA vision model right now (Nemotron Nano 2 VL was
# disabled Apr 13, 2026). Leaf auto-detects an image-capable model and prefers NVIDIA
# ones if any appear. Override with LEAF_VISION_MODEL.
VISION_PREFERENCE = [
    "moonshotai/Kimi-K2.6",
    "zai-org/GLM-5.3-Flash",
    "deepseek-ai/DeepSeek-V4.1-Flash",
]


# Chat-template switch for reasoning, per model family. Nemotron reads enable_thinking;
# Kimi reads thinking (measured Oct 2026: with thinking off, Kimi-K2.6 vision answers in
# 3.9 s instead of 11.2 s for a quarter of the tokens, with equally specific observations).
THINKING_TEMPLATE_KEY = {"moonshotai/": "thinking"}
DEFAULT_THINKING_KEY = "enable_thinking"


def thinking_key(model: str) -> str:
    for prefix, key in THINKING_TEMPLATE_KEY.items():
        if model.startswith(prefix):
            return key
    return DEFAULT_THINKING_KEY


# Roles in the NVIDIA stack. Settings maps each to a model ID (env overrides per role).
ULTRA, SUPER, LIGHTNING, NANO, VISION = "ultra", "super", "lightning", "nano", "vision"


@dataclass(frozen=True)
class Route:
    """How one task is served: its model chain (first choice, then fallbacks) and budget.

    `max_tokens` caps one call's output, reasoning included (the client doubles it once when a
    reply is truncated, never past 16,000). `est_in`/`est_out` are typical tokens per call, for
    the cost model in docs/LEAF_PLATFORM.md. `optional` tasks switch to rules or templates when
    the day's spend reaches the brownout level; core tasks run until the hard cap.
    """

    chain: tuple[str, ...]
    max_tokens: int
    thinking: bool
    est_in: int = 0
    est_out: int = 0
    optional: bool = False


# The routing table: every model call names one of these tasks (its trace step). Tasks for
# later milestones are listed so the plan and the cost model have one source of truth.
ROUTES: dict[str, Route] = {
    # Front door for live requests: one Lightning call routes and screens. Lightning, not Nano:
    # same accuracy and cost on evals/front_door.jsonl, 0.4 s vs 1.4-1.6 s median (Oct 2026).
    "front_door": Route((LIGHTNING, NANO), 300, False, 400, 80, optional=True),
    # Background screening, where speed does not matter: Nano 3 first.
    "checkin_screen": Route((NANO, LIGHTNING), 300, False, 400, 80),
    "job_triage": Route((NANO, LIGHTNING), 300, False, 400, 80, optional=True),
    "radar_screen": Route((NANO, LIGHTNING), 300, False, 1500, 80),
    # Doctor (exists).
    "vision": Route((VISION,), 4000, False, 1500, 600),
    "look_closer": Route((VISION,), 4000, False, 1200, 400),
    "diagnose": Route((ULTRA, SUPER, LIGHTNING), 6000, True, 3000, 2000),
    "first_differential": Route((ULTRA, SUPER), 6000, True, 3000, 2000),
    "investigate": Route((ULTRA, SUPER), 4096, True, 6000, 800),
    "critic": Route((SUPER, LIGHTNING), 3000, True, 4000, 1500),
    "plan": Route((LIGHTNING, SUPER), 2500, False, 3000, 1200),
    "claim_check": Route((LIGHTNING,), 1500, False, 2000, 400),
    "checkin_compare": Route((VISION,), 1500, False, 1500, 400),
    "checkin_plan": Route((LIGHTNING, SUPER), 1500, False, 1500, 600),
    "chat": Route((LIGHTNING, SUPER), 800, False, 1500, 400),
    "json_repair": Route((LIGHTNING,), 2048, False, 1500, 800),
    # Later milestones (docs/LEAF_PLATFORM.md).
    "orchestrate": Route((ULTRA, SUPER), 4000, True, 3000, 1500),
    "fact_extract": Route((LIGHTNING, SUPER), 3000, False, 4500, 1200),
    # Super reasons before answering, and reasoning counts toward max_tokens.
    "fact_verify": Route((SUPER, ULTRA), 4000, True, 4500, 1200),
    "identify": Route((VISION,), 4000, False, 1500, 600),
    "identify_check": Route((SUPER, ULTRA), 3000, True, 2500, 800),
    "care_wording": Route((LIGHTNING, NANO), 1200, False, 800, 400, optional=True),
    "notify_text": Route((LIGHTNING, NANO), 300, False, 300, 100, optional=True),
    "translate": Route((LIGHTNING,), 1500, False, 600, 600, optional=True),
    "season_outline": Route((ULTRA, SUPER), 16000, True, 5000, 4000),
    "season_plan": Route((ULTRA, SUPER), 16000, True, 5000, 3000),
    "plan_verify": Route((SUPER, ULTRA), 8000, True, 6000, 3000),
    "replan": Route((ULTRA, SUPER), 8000, True, 6000, 3000, optional=True),
    "radar_extract": Route((LIGHTNING, SUPER), 2000, False, 4000, 1000),
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Secrets never appear in repr(), so a logged or printed Settings cannot leak them.
    nebius_api_key: str = field(default_factory=lambda: _env("NEBIUS_API_KEY"), repr=False)
    tavily_api_key: str = field(default_factory=lambda: _env("TAVILY_API_KEY"), repr=False)
    base_url: str = field(default_factory=lambda: _env("NEBIUS_BASE_URL", GLOBAL_BASE_URL))
    # Per-role model overrides.
    reason_model: str = field(default_factory=lambda: _env("LEAF_REASON_MODEL", NEMOTRON_ULTRA))
    fallback_model: str = field(default_factory=lambda: _env("LEAF_FALLBACK_MODEL", NEMOTRON_SUPER))
    fast_model: str = field(default_factory=lambda: _env("LEAF_FAST_MODEL", NEMOTRON_LIGHTNING))
    nano_model: str = field(default_factory=lambda: _env("LEAF_NANO_MODEL", NEMOTRON_NANO))
    vision_model: str = field(default_factory=lambda: _env("LEAF_VISION_MODEL"))
    # Some models are only served from a regional base URL (e.g. us-central1).
    # Format: "model_id=base_url;model_id=base_url"
    model_base_urls: str = field(default_factory=lambda: _env("LEAF_MODEL_BASE_URLS"))
    # Credit protection for the public demo.
    per_ip_per_hour: int = field(default_factory=lambda: _env_int("LEAF_PER_IP_PER_HOUR", 20))
    daily_cap: int = field(default_factory=lambda: _env_int("LEAF_DAILY_CAP", 60))
    # Tavily credits per day: sized daily from the real monthly balance; this is a ceiling.
    tavily_daily_cap: int = field(default_factory=lambda: _env_int("LEAF_TAVILY_DAILY_CAP", 0))
    request_timeout_s: int = field(default_factory=lambda: _env_int("LEAF_TIMEOUT_S", 120))
    # Whole-request budget for one diagnosis or chat, across every model fallback.
    deadline_s: int = field(default_factory=lambda: _env_int("LEAF_DEADLINE_S", 100))
    # Requests allowed to run at once; extra ones get a quick 503 instead of queueing.
    max_concurrent: int = field(default_factory=lambda: _env_int("LEAF_MAX_CONCURRENT", 8))
    # Deep mode (the investigator agent). Limits are enforced in code.
    deep_daily_cap: int = field(default_factory=lambda: _env_int("LEAF_DEEP_DAILY_CAP", 20))
    deep_max_calls: int = field(default_factory=lambda: _env_int("LEAF_DEEP_MAX_CALLS", 8))
    deep_wall_s: int = field(default_factory=lambda: _env_int("LEAF_DEEP_WALL_S", 90))
    deep_budget_usd: float = field(default_factory=lambda: _env_float("LEAF_DEEP_BUDGET_USD", 0.10))
    deep_max_photos: int = 3
    # Structured symptom checklist in the vision step. Off: on 52 tune photos it fixed 5 Quick
    # cases and broke 7 (top-1 48% vs 52% without), so it did not earn its place (README).
    checklist: bool = field(default_factory=lambda: _env("LEAF_CHECKLIST", "0") == "1")
    # Start the investigation from the structured first differential (the Quick diagnosis
    # step) instead of from nothing; the eval compares both.
    deep_seed: bool = field(default_factory=lambda: _env("LEAF_DEEP_SEED", "1") != "0")
    # Weight of the first differential when blended with the investigator's final one (0 = the
    # investigator alone). Chosen on the tune split; see docs/AGENT_V2.md.
    deep_blend_first: float = field(default_factory=lambda: _env_float("LEAF_DEEP_BLEND", 0.7))
    deep_max_look_closer: int = 3
    # Plant journal: a SQLite file by default; "memory" keeps entries in this process only.
    journal_path: str = field(default_factory=lambda: _env("LEAF_JOURNAL_PATH", "data/leaf.db"))
    journal_days: int = field(default_factory=lambda: _env_int("LEAF_JOURNAL_DAYS", 30))
    # Set in production: entries go to this Object Storage bucket (AWS_* variables configure it).
    journal_bucket: str = field(default_factory=lambda: _env("LEAF_JOURNAL_BUCKET", ""))
    # The site's public address for links in calendar reminders ("" = the request's own host).
    public_url: str = field(default_factory=lambda: _env("LEAF_PUBLIC_URL", ""))
    # Proxies in front of Leaf that append to X-Forwarded-For (0 = none, trust the socket).
    trusted_proxy_hops: int = field(default_factory=lambda: _env_int("LEAF_TRUSTED_PROXY_HOPS", 1))
    # Platform data: in production the Worker serves D1, KV and the job queue to the container
    # at this address (outbound interception, cloudflare/src/index.ts). Empty = local SQLite
    # at platform_db ("memory" keeps it in this process, for tests).
    platform_url: str = field(default_factory=lambda: _env("LEAF_PLATFORM_URL", ""))
    platform_db: str = field(default_factory=lambda: _env("LEAF_PLATFORM_DB", "data/platform.db"))
    # Hard daily model spend (UTC day). At brownout x cap, optional tasks use rules or
    # templates; at the cap every model call is refused and Leaf runs rules-only.
    # Web Push (VAPID). The public key is not secret (browsers need it); the private key is a
    # Worker secret. No private key: notifications are recorded but nothing is pushed.
    vapid_public_key: str = field(default_factory=lambda: _env("VAPID_PUBLIC_KEY"))
    vapid_private_key: str = field(default_factory=lambda: _env("VAPID_PRIVATE_KEY"), repr=False)
    vapid_subject: str = field(
        default_factory=lambda: _env("VAPID_SUBJECT", "https://leafathon.site")
    )
    spend_cap_usd: float = field(default_factory=lambda: _env_float("LEAF_SPEND_CAP_USD", 3.0))
    brownout: float = field(default_factory=lambda: _env_float("LEAF_BROWNOUT", 0.8))

    def role_model(self, role: str) -> str:
        """The model ID behind a role. Vision is discovered at startup, so it is "" here
        unless LEAF_VISION_MODEL pins it."""
        return {
            ULTRA: self.reason_model,
            SUPER: self.fallback_model,
            LIGHTNING: self.fast_model,
            NANO: self.nano_model,
            VISION: self.vision_model,
        }[role]

    def models(self, task: str) -> list[str]:
        """The task's model chain from ROUTES, first choice first, without duplicates."""
        out: list[str] = []
        for role in ROUTES[task].chain:
            model = self.role_model(role)
            if model and model not in out:
                out.append(model)
        return out

    def budget(self, task: str) -> int:
        return ROUTES[task].max_tokens

    @property
    def repair_model(self) -> str:
        return self.models("json_repair")[0]

    def base_url_for(self, model: str) -> str:
        for pair in filter(None, self.model_base_urls.split(";")):
            key, _, url = pair.partition("=")
            if key.strip() == model and url.strip():
                return url.strip()
        return self.base_url


def get_settings() -> Settings:
    return Settings()
