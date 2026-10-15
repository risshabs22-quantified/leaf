"""The routing table in config.py is the single source of model choice and token budgets."""

from __future__ import annotations

import re
from pathlib import Path

from backend.config import (
    LIGHTNING,
    NANO,
    NEMOTRON_LIGHTNING,
    NEMOTRON_NANO,
    NEMOTRON_SUPER,
    NEMOTRON_ULTRA,
    ROUTES,
    SUPER,
    ULTRA,
    VISION,
    Settings,
)

BACKEND = Path(__file__).resolve().parent.parent / "backend"


def test_every_route_has_a_sane_budget_and_known_roles():
    for task, r in ROUTES.items():
        assert r.chain, task
        assert set(r.chain) <= {ULTRA, SUPER, LIGHTNING, NANO, VISION}, task
        assert 100 <= r.max_tokens <= 16000, task
        assert r.est_in >= 0 and r.est_out >= 0, task


def test_the_nvidia_stack_has_the_roles_the_platform_promises():
    s = Settings(nebius_api_key="x")
    # Live front door: Lightning (fast); background screening: Nano 3 (docs, "Decisions").
    assert s.models("front_door") == [NEMOTRON_LIGHTNING, NEMOTRON_NANO]
    for background in ("checkin_screen", "job_triage", "radar_screen"):
        assert s.models(background)[0] == NEMOTRON_NANO, background
    assert s.models("diagnose") == [NEMOTRON_ULTRA, NEMOTRON_SUPER, NEMOTRON_LIGHTNING]
    assert s.models("season_plan")[0] == NEMOTRON_ULTRA
    for verifier in ("critic", "plan_verify", "fact_verify", "identify_check"):
        assert s.models(verifier)[0] == NEMOTRON_SUPER, verifier
    for volume in ("chat", "plan", "care_wording", "notify_text", "translate", "fact_extract"):
        assert s.models(volume)[0] == NEMOTRON_LIGHTNING, volume
    # Only perception may use the non-NVIDIA vision model.
    for task, r in ROUTES.items():
        assert (VISION in r.chain) == (task in {"vision", "look_closer", "checkin_compare",
                                                "identify"}), task  # fmt: skip


def test_env_overrides_a_role_everywhere_and_chains_drop_duplicates():
    s = Settings(nebius_api_key="x", nano_model=NEMOTRON_LIGHTNING)
    assert s.models("checkin_screen") == [NEMOTRON_LIGHTNING]  # Nano -> Lightning, deduplicated
    assert s.repair_model == NEMOTRON_LIGHTNING
    assert Settings(nebius_api_key="x", vision_model="").models("vision") == []


def test_every_model_step_in_the_code_has_a_route():
    steps = set()
    for path in BACKEND.glob("*.py"):
        # complete_json(step=..., models=...) and task_scope(...) name every model call's task.
        steps |= set(re.findall(r'step="([a-z_]+)",\s+models=', path.read_text()))
        steps |= set(re.findall(r'task_scope\("([a-z_]+)"\)', path.read_text()))
    assert steps, "no steps found"
    assert steps - set(ROUTES) == set(), steps - set(ROUTES)


def test_no_call_site_picks_a_model_outside_the_table():
    """Model choice goes through settings.models(task); role fields are read only in config
    (and named in /api/health)."""
    pattern = re.compile(r"\b(s|settings|self\.settings)\.(reason|fallback|fast|nano)_model\b")
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if path.name in {"config.py", "app.py"}:
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{i}")
    assert offenders == []
