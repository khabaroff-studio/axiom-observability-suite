from __future__ import annotations

import argparse
import json
import string
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

ALLOWED_PLACEHOLDERS = {"host", "service", "container", "monitor"}
LIST_OPS = {"contains_any", "in", "prefix_in"}


def _load_schema(schema_path: Path) -> dict[str, Any]:
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")
    return json.loads(schema_path.read_text())


def _iter_runbooks(config: dict[str, Any]) -> list[str]:
    runbooks: list[str] = []
    defaults = config.get("defaults", {})
    if isinstance(defaults, dict):
        runbook = defaults.get("runbook", [])
        if isinstance(runbook, list):
            runbooks.extend([str(line) for line in runbook])

    profiles = config.get("profiles", {})
    if isinstance(profiles, dict):
        for profile in profiles.values():
            if isinstance(profile, dict):
                runbook = profile.get("runbook", [])
                if isinstance(runbook, list):
                    runbooks.extend([str(line) for line in runbook])

    services = config.get("services", {})
    if isinstance(services, dict):
        for service in services.values():
            if isinstance(service, dict):
                runbook = service.get("runbook", [])
                if isinstance(runbook, list):
                    runbooks.extend([str(line) for line in runbook])

    return runbooks


def _validate_placeholders(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    formatter = string.Formatter()
    for line in _iter_runbooks(config):
        try:
            for _, field_name, _, _ in formatter.parse(line):
                if field_name is None:
                    continue
                if field_name not in ALLOWED_PLACEHOLDERS:
                    errors.append(
                        f"Unknown placeholder '{{{field_name}}}' in runbook: {line}"
                    )
        except ValueError as exc:
            errors.append(f"Invalid runbook placeholder syntax: {line} ({exc})")
    return errors


def _validate_references(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    groups = config.get("groups", {})
    topics = config.get("topics", {})
    if isinstance(groups, dict):
        default_group = config.get("default_group")
        if default_group and default_group not in groups:
            errors.append(f"default_group not found in groups: {default_group}")
    if isinstance(topics, dict):
        default_topic = config.get("default_topic")
        if default_topic and default_topic not in topics:
            errors.append(f"default_topic not found in topics: {default_topic}")

    if isinstance(groups, dict) and isinstance(topics, dict):
        for route in config.get("routes", []) or []:
            if not isinstance(route, dict):
                continue
            group = route.get("group")
            topic = route.get("topic")
            if group and group not in groups:
                errors.append(f"route group not found in groups: {group}")
            if topic and topic not in topics:
                errors.append(f"route topic not found in topics: {topic}")

    profiles = config.get("profiles", {})
    services = config.get("services", {})
    if isinstance(profiles, dict) and isinstance(services, dict):
        for service_name, service in services.items():
            if not isinstance(service, dict):
                continue
            for profile in service.get("profiles", []) or []:
                if profile not in profiles:
                    errors.append(
                        f"service '{service_name}' references missing profile '{profile}'"
                    )

    return errors


def _validate_list_ops(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for section in ("drop",):
        for rule in config.get(section, []) or []:
            if not isinstance(rule, dict):
                continue
            match = rule.get("match", {})
            op = match.get("op") if isinstance(match, dict) else None
            value = match.get("value") if isinstance(match, dict) else None
            if op in LIST_OPS and not isinstance(value, list):
                errors.append(f"Rule op '{op}' requires list value: {match}")

    profiles = config.get("profiles", {})
    if isinstance(profiles, dict):
        for profile in profiles.values():
            if not isinstance(profile, dict):
                continue
            for rule in profile.get("p1", []) or []:
                if not isinstance(rule, dict):
                    continue
                match = rule.get("match", {})
                op = match.get("op") if isinstance(match, dict) else None
                value = match.get("value") if isinstance(match, dict) else None
                if op in LIST_OPS and not isinstance(value, list):
                    errors.append(f"Rule op '{op}' requires list value: {match}")

    return errors


def validate_routes_config(config: dict[str, Any], schema_path: Path) -> None:
    schema = _load_schema(schema_path)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(config), key=lambda e: e.path)
    if errors:
        messages = []
        for error in errors:
            path = "/".join([str(p) for p in error.path])
            messages.append(f"{path}: {error.message}")
        raise ValueError("Invalid routes.yml schema:\n" + "\n".join(messages))

    extra_errors: list[str] = []
    extra_errors.extend(_validate_placeholders(config))
    extra_errors.extend(_validate_references(config))
    extra_errors.extend(_validate_list_ops(config))
    if extra_errors:
        raise ValueError("Invalid routes.yml config:\n" + "\n".join(extra_errors))


def validate_routes_file(routes_path: Path, schema_path: Path) -> None:
    if not routes_path.exists():
        raise FileNotFoundError(f"routes.yml not found: {routes_path}")
    config = yaml.safe_load(routes_path.read_text()) or {}
    if not isinstance(config, dict):
        raise ValueError("routes.yml must be a mapping at top level")
    validate_routes_config(config, schema_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate routes.yml schema")
    parser.add_argument("--file", default="routes.yml")
    parser.add_argument("--schema", default="routes.schema.json")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    routes_path = Path(args.file)
    schema_path = Path(args.schema)

    if not routes_path.is_absolute():
        routes_path = root / routes_path
    if not schema_path.is_absolute():
        schema_path = root / schema_path

    validate_routes_file(routes_path, schema_path)
    print(f"{routes_path.name} OK")


if __name__ == "__main__":
    main()
