"""Compiled Desktop production presets/serializer cross the real Gateway boundary."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.mutations import LlmProfileActivationError, upsert_llm_provider

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def desktop_router_toml():
    module = ROOT / "desktop/electron/dist/desktop-router-config.js"
    node = shutil.which("node")
    if not node or not module.is_file():
        if os.environ.get("OPENSQUILLA_REQUIRE_DESKTOP_ROUTER_BRIDGE") == "1":
            pytest.fail("Build Desktop TypeScript and provide Node before the Router bridge check")
        pytest.skip("Desktop compiled bridge runs explicitly after TypeScript build in Desktop CI")

    def serialize(binding: str | None = "follow_primary", enabled: bool = True) -> str:
        script = """
import {
  routerConfigTomlLines, resolveDesktopRouterUpdate,
} from './desktop/electron/dist/desktop-router-config.js';
import { defaultRouterTiers } from './desktop/electron/dist/desktop-router-profiles.js';
const router = resolveDesktopRouterUpdate({
  payload: {}, existing: null, routerMode: 'recommended', routerDefaultTier: 'c1',
  defaultTiers: defaultRouterTiers('openrouter', 'recommended'), freshConfig: true,
});
const [binding, enabled] = process.argv.slice(1);
if (binding === 'absent') delete router.routerPresetBinding;
else router.routerPresetBinding = binding;
if (enabled === 'false') router.routerMode = 'disabled';
process.stdout.write(routerConfigTomlLines(router).join('\\n'));
"""
        result = subprocess.run(
            [node, "--input-type=module", "-e", script, binding or "absent", str(enabled).lower()],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        return result.stdout

    return serialize


def config_from_desktop(raw: str) -> GatewayConfig:
    # A pristine Desktop runtime may carry the OpenRouter default without a key.
    return GatewayConfig.model_validate({"llm": {"provider": "openrouter"}, **tomllib.loads(raw)})


def test_new_desktop_recommendations_follow_first_usable_primary(desktop_router_toml) -> None:
    source = config_from_desktop(desktop_router_toml())
    assert source.squilla_router.enabled
    assert source.squilla_router.preset_binding == "follow_primary"
    assert source.squilla_router.tiers["c1"]["provider"] == "openrouter"
    result = upsert_llm_provider(source, provider_id="tokenrhythm", api_key="synthetic-bridge-key")
    assert result.config.llm.provider == "tokenrhythm"
    assert result.config.squilla_router.preset_binding == "follow_primary"
    assert result.config.squilla_router.tiers["c1"]["provider"] == "tokenrhythm"
    assert result.config.squilla_router.tier_profile is None
    assert source.llm.provider == "openrouter"


@pytest.mark.parametrize("binding", [None, "custom"])
def test_identical_historical_or_custom_presets_need_explicit_resolution(
    desktop_router_toml, binding: str | None,
) -> None:
    raw = desktop_router_toml(binding)
    if binding is None:
        assert "preset_binding" not in raw
    source = config_from_desktop(raw)
    before = source.model_dump()
    with pytest.raises(LlmProfileActivationError) as caught:
        upsert_llm_provider(source, provider_id="tokenrhythm", api_key="synthetic-bridge-key")
    assert caught.value.reason == "router_provider_conflict"
    assert caught.value.details["conflictProviders"] == ["openrouter"]
    assert source.model_dump() == before
    disabled = upsert_llm_provider(
        source, provider_id="tokenrhythm", api_key="synthetic-bridge-key", router_action="disable",
    ).config
    assert not disabled.squilla_router.enabled
    assert disabled.squilla_router.tiers == source.squilla_router.tiers
    recommended = upsert_llm_provider(
        source, provider_id="tokenrhythm", api_key="synthetic-bridge-key",
        router_action="use_recommended",
    ).config
    assert recommended.squilla_router.tiers["c1"]["provider"] == "tokenrhythm"
    assert recommended.squilla_router.preset_binding == "follow_primary"


def test_disabled_legacy_desktop_router_does_not_block_provider_save(desktop_router_toml) -> None:
    source = config_from_desktop(desktop_router_toml(None, enabled=False))
    result = upsert_llm_provider(source, provider_id="tokenrhythm", api_key="synthetic-bridge-key")
    assert result.config.llm.provider == "tokenrhythm"
    assert not result.config.squilla_router.enabled
