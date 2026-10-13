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


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    nebius_api_key: str = field(default_factory=lambda: _env("NEBIUS_API_KEY"))
    tavily_api_key: str = field(default_factory=lambda: _env("TAVILY_API_KEY"))
    base_url: str = field(default_factory=lambda: _env("NEBIUS_BASE_URL", GLOBAL_BASE_URL))
    # Per-role model overrides.
    reason_model: str = field(default_factory=lambda: _env("LEAF_REASON_MODEL", NEMOTRON_ULTRA))
    fallback_model: str = field(default_factory=lambda: _env("LEAF_FALLBACK_MODEL", NEMOTRON_SUPER))
    fast_model: str = field(default_factory=lambda: _env("LEAF_FAST_MODEL", NEMOTRON_LIGHTNING))
    vision_model: str = field(default_factory=lambda: _env("LEAF_VISION_MODEL"))
    # Some models are only served from a regional base URL (e.g. us-central1).
    # Format: "model_id=base_url;model_id=base_url"
    model_base_urls: str = field(default_factory=lambda: _env("LEAF_MODEL_BASE_URLS"))
    # Credit protection for the public demo.
    per_ip_per_hour: int = field(default_factory=lambda: _env_int("LEAF_PER_IP_PER_HOUR", 20))
    daily_cap: int = field(default_factory=lambda: _env_int("LEAF_DAILY_CAP", 200))
    request_timeout_s: int = field(default_factory=lambda: _env_int("LEAF_TIMEOUT_S", 120))
    # Whole-request budget for one diagnosis or chat, across every model fallback.
    deadline_s: int = field(default_factory=lambda: _env_int("LEAF_DEADLINE_S", 100))
    # Requests allowed to run at once; extra ones get a quick 503 instead of queueing.
    max_concurrent: int = field(default_factory=lambda: _env_int("LEAF_MAX_CONCURRENT", 8))
    # Proxies in front of Leaf that append to X-Forwarded-For (0 = none, trust the socket).
    trusted_proxy_hops: int = field(default_factory=lambda: _env_int("LEAF_TRUSTED_PROXY_HOPS", 1))

    def base_url_for(self, model: str) -> str:
        for pair in filter(None, self.model_base_urls.split(";")):
            key, _, url = pair.partition("=")
            if key.strip() == model and url.strip():
                return url.strip()
        return self.base_url


def get_settings() -> Settings:
    return Settings()
