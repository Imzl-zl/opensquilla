#!/usr/bin/env python3
"""Materialize and validate frozen OpenSquilla ensemble profiles.

The lineup specification is data, not Python code. This keeps the monthly
evaluation harness reusable across model families and campaign names.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "opensquilla-monthly-fusion-profiles/v1"
LINEUP_SCHEMA = "opensquilla-monthly-fusion-lineups/v1"
GROUP_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

DEFAULT_ENSEMBLE = {
    "enabled": True,
    "mode": "b5_fusion",
    "selection_mode": "custom_b5",
    "proposer_tools": False,
    "min_successful_proposers": 1,
    "proposer_max_retries": 0,
    "all_failed_policy": "fallback_single",
    "candidate_max_chars": 24000,
    "proposer_timeout_seconds": 120.0,
    "aggregator_timeout_seconds": 180.0,
    "shuffle_candidates": False,
    "record_candidates": True,
    "model_options": [],
}

DRACO_BASE_CONFIG = {
    "workspace_strict": True,
    "agent_max_iterations": 100,
    "search_provider": "brave",
    "search_api_key_env": "BRAVE_API_KEY",
    "search_max_results": 10,
    "search_use_env_proxy": True,
    "search_fallback_policy": "off",
    "skills": {"allow_bundled": False},
    "tools": {
        "profile": "full",
        "deny": [
            "group:messaging",
            "group:sessions",
            "group:memory",
            "group:trusted_host",
            "apply_patch",
            "background_process",
            "execute_code",
            "process",
        ],
        "file_edit_requires_fresh_read": True,
    },
    "compaction": {"enabled": False},
    "naming": {"enabled": False},
    "sandbox": {"sandbox": False, "security_grading": False},
    "permissions": {"default_mode": "full"},
    "memory": {
        "flush_enabled": False,
        "auto_capture_enabled": False,
        "capture_mode": "off",
        "session_source_enabled": False,
        "repair_enabled": False,
    },
}


def read_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _positive_number(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def validate_spec(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized, validated lineup specification."""
    spec = copy.deepcopy(dict(raw))
    if spec.get("schema_version") != LINEUP_SCHEMA:
        raise ValueError(f"lineup schema must be {LINEUP_SCHEMA}")
    provider = spec.get("provider")
    fallback_model = spec.get("fallback_model")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a nonempty string")
    if not isinstance(fallback_model, str) or not fallback_model.strip():
        raise ValueError("fallback_model must be a nonempty string")
    groups = spec.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise ValueError("groups must be a nonempty object")
    normalized_groups: dict[str, dict[str, Any]] = {}
    for group, lineup in groups.items():
        if not isinstance(group, str) or not GROUP_PATTERN.fullmatch(group):
            raise ValueError(f"invalid group name: {group!r}")
        if not isinstance(lineup, dict):
            raise ValueError(f"group {group} must be an object")
        proposers = lineup.get("proposers")
        aggregator = lineup.get("aggregator")
        if (
            not isinstance(proposers, list)
            or not proposers
            or not all(isinstance(v, str) and v for v in proposers)
        ):
            raise ValueError(f"group {group} requires one or more proposer model IDs")
        if len(proposers) != len(set(proposers)):
            raise ValueError(f"group {group} contains duplicate proposer model IDs")
        if not isinstance(aggregator, str) or not aggregator:
            raise ValueError(f"group {group} requires an aggregator model ID")
        normalized_groups[group] = {"proposers": list(proposers), "aggregator": aggregator}
    common = copy.deepcopy(DEFAULT_ENSEMBLE)
    overrides = spec.get("ensemble") or {}
    if not isinstance(overrides, dict):
        raise ValueError("ensemble must be an object")
    unknown = set(overrides) - set(DEFAULT_ENSEMBLE)
    if unknown:
        raise ValueError("unsupported ensemble settings: " + ", ".join(sorted(unknown)))
    common.update(overrides)
    if common["mode"] != "b5_fusion" or common["selection_mode"] != "custom_b5":
        raise ValueError("monthly fusion profiles require custom_b5/b5_fusion")
    if (
        isinstance(common["min_successful_proposers"], bool)
        or int(common["min_successful_proposers"]) < 1
    ):
        raise ValueError("min_successful_proposers must be positive")
    _positive_number(common["proposer_timeout_seconds"], "proposer_timeout_seconds")
    _positive_number(common["aggregator_timeout_seconds"], "aggregator_timeout_seconds")
    thinking_level = spec.get("thinking_level", "high")
    if thinking_level not in {"off", "low", "medium", "high"}:
        raise ValueError("thinking_level must be off, low, medium, or high")
    return {
        "schema_version": LINEUP_SCHEMA,
        "provider": provider,
        "fallback_model": fallback_model,
        "thinking_level": thinking_level,
        "ensemble": common,
        "groups": normalized_groups,
    }


def load_spec(path: str | Path) -> dict[str, Any]:
    return validate_spec(read_json(path))


def _reject_literal_secrets(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if (
                normalized in {"api_key", "access_token", "refresh_token", "password", "secret"}
                and item
            ):
                raise ValueError("literal secret field forbidden at " + ".".join((*path, str(key))))
            _reject_literal_secrets(item, (*path, str(key)))
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_literal_secrets(item, path)


def all_models(spec: Mapping[str, Any]) -> tuple[str, ...]:
    models = {str(spec["fallback_model"])}
    for lineup in spec["groups"].values():
        models.update(lineup["proposers"])
        models.add(lineup["aggregator"])
    return tuple(sorted(models))


def _validate_catalog_overrides(overrides: Mapping[str, Any], spec: Mapping[str, Any]) -> None:
    rows = overrides.get(spec["provider"])
    if not isinstance(rows, Mapping):
        raise ValueError(f"catalog snapshot requires a {spec['provider']} model mapping")
    for model in all_models(spec):
        row = rows.get(model)
        if not isinstance(row, Mapping):
            raise ValueError(f"catalog snapshot missing model: {model}")
        for key in ("max_output_tokens", "context_window"):
            number = row.get(key)
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                raise ValueError(f"catalog snapshot missing positive {key} for {model}")


def build_profile(
    group: str,
    spec: Mapping[str, Any],
    base_config: Mapping[str, Any] | None = None,
    model_catalog_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    spec = validate_spec(spec)
    if group not in spec["groups"]:
        raise ValueError(f"unknown profile group: {group!r}")
    payload = copy.deepcopy(DRACO_BASE_CONFIG)
    if base_config:
        payload.update(copy.deepcopy(dict(base_config)))
    _reject_literal_secrets(payload)
    provider = spec["provider"]
    llm = payload.setdefault("llm", {})
    llm.update(provider=provider, model=spec["fallback_model"])
    llm.setdefault(
        "api_key_env", "OPENROUTER_API_KEY" if provider == "openrouter" else "LLM_API_KEY"
    )
    if provider == "openrouter":
        llm.setdefault("base_url", "https://openrouter.ai/api/v1")
    payload["squilla_router"] = {
        "enabled": False,
        "auto_thinking": False,
        "cross_provider_tiers": False,
    }
    payload["agent_stream_idle_timeout_seconds"] = 1200.0
    ensemble = copy.deepcopy(spec["ensemble"])
    lineup = spec["groups"][group]
    ensemble["candidates"] = [
        {
            "provider": provider,
            "model": model,
            "role": role,
            "enabled": True,
            "thinking_level": spec["thinking_level"],
            "source": "custom",
        }
        for role, model in [
            *[("proposer", model) for model in lineup["proposers"]],
            ("aggregator", lineup["aggregator"]),
        ]
    ]
    payload["llm_ensemble"] = ensemble
    if model_catalog_overrides is not None:
        supplied = copy.deepcopy(dict(model_catalog_overrides))
        _validate_catalog_overrides(supplied, spec)
        payload["models"] = supplied
    assert_profile_config(payload, group, spec)
    return payload


def assert_profile_config(
    config: Mapping[str, Any] | Any, group: str, spec: Mapping[str, Any]
) -> None:
    spec = validate_spec(spec)
    if group not in spec["groups"]:
        raise ValueError(f"unknown profile group: {group!r}")
    payload = config if isinstance(config, Mapping) else config.model_dump()
    llm = payload.get("llm", {})
    if llm.get("provider") != spec["provider"] or llm.get("model") != spec["fallback_model"]:
        raise ValueError("fixed fallback provider/model drifted")
    if payload.get("squilla_router", {}).get("enabled") is not False:
        raise ValueError("router unexpectedly enabled")
    if float(payload.get("agent_stream_idle_timeout_seconds", 0)) != 1200.0:
        raise ValueError("gateway stream idle timeout drifted")
    ensemble = payload.get("llm_ensemble", {})
    for field, expected in spec["ensemble"].items():
        if ensemble.get(field) != expected:
            raise ValueError(f"ensemble setting drifted: {field}")
    if ensemble.get("target_successful_proposers") is not None:
        raise ValueError("target_successful_proposers must remain unset")
    lineup = spec["groups"][group]
    expected = [("proposer", model) for model in lineup["proposers"]]
    expected.append(("aggregator", lineup["aggregator"]))
    rows = ensemble.get("candidates", [])
    if [(row.get("role"), row.get("model")) for row in rows] != expected:
        raise ValueError("candidate order/role/model drifted")
    if any(
        row.get("provider") != spec["provider"]
        or row.get("thinking_level") != spec["thinking_level"]
        or row.get("enabled") is not True
        for row in rows
    ):
        raise ValueError("candidate provider/thinking/enabled setting drifted")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return (
            "{ "
            + ", ".join(
                f"{_toml_value(str(key))} = {_toml_value(item)}"
                for key, item in value.items()
                if item is not None
            )
            + " }"
        )
    raise ValueError(f"unsupported TOML value type: {type(value).__name__}")


def dumps_toml(payload: Mapping[str, Any]) -> str:
    lines = ["# Frozen ensemble experiment; credentials are environment references only."]

    def emit(table: Mapping[str, Any], path: tuple[str, ...]) -> None:
        if path:
            lines.extend(["", "[" + ".".join(_toml_value(key) for key in path) + "]"])
        for key, value in table.items():
            if value is not None and not isinstance(value, Mapping):
                lines.append(f"{_toml_value(str(key))} = {_toml_value(value)}")
        for key, value in table.items():
            if isinstance(value, Mapping):
                emit(value, (*path, str(key)))

    emit(payload, ())
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)
    return text


def write_profiles(
    root_dir: str | Path,
    spec: Mapping[str, Any],
    base_config: Mapping[str, Any] | None = None,
    model_catalog_overrides: Mapping[str, Any] | None = None,
    source_revision: str | None = None,
) -> dict[str, Any]:
    spec = validate_spec(spec)
    root = Path(root_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_revision": source_revision,
        "catalog_frozen": model_catalog_overrides is not None,
        "lineup_spec": spec,
        "profiles": {},
    }
    pending = []
    for group in spec["groups"]:
        payload = build_profile(group, spec, base_config, model_catalog_overrides)
        encoded = dumps_toml(payload).encode()
        pending.append((group, root / group / "config.toml", encoded))
    for _, path, encoded in pending:
        if path.exists() and path.read_bytes() != encoded:
            raise FileExistsError(f"frozen profile differs; use a new output directory: {path}")
    for group, path, encoded in pending:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        path.chmod(0o600)
        manifest["profiles"][group] = {
            "config_path": str(path.relative_to(root)),
            "config_sha256": hashlib.sha256(encoded).hexdigest(),
            "lineup": copy.deepcopy(spec["groups"][group]),
            "fallback": spec["fallback_model"],
        }
    manifest_path = root / "profiles-manifest.json"
    encoded_manifest = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    if manifest_path.exists() and manifest_path.read_bytes() != encoded_manifest:
        raise FileExistsError(
            f"frozen profiles manifest differs; use a new output directory: {manifest_path}"
        )
    if not manifest_path.exists():
        manifest_path.write_bytes(encoded_manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lineups", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-config", type=Path)
    parser.add_argument("--catalog-snapshot", type=Path)
    parser.add_argument("--source-revision")
    args = parser.parse_args()
    spec = load_spec(args.lineups)
    base = tomllib.loads(args.base_config.read_text()) if args.base_config else None
    snapshot = read_json(args.catalog_snapshot) if args.catalog_snapshot else None
    overrides = snapshot.get("model_overrides") if snapshot else None
    manifest = write_profiles(
        args.output_dir,
        spec,
        base,
        overrides,
        source_revision=args.source_revision,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
