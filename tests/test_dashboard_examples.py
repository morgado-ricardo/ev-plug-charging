"""The shipped example dashboards reference entity IDs by hand, so the
failure mode is an ID that no longer exists -- and the user finds out via
"Entity not available" on their dashboard, not via anything noisy.

Entity IDs here derive from the entity's DISPLAY NAME (Home Assistant
slugifies `<device name> <entity name>` when `_attr_has_entity_name` is
set), which means renaming a friendly name silently moves the entity ID.
So this test derives the set of valid suffixes from the platform modules
themselves -- the same name strings they pass to EvPlugChargingEntity --
rather than from a hand-maintained list that would drift.

Plain pytest; no Home Assistant needed (the platform modules are parsed,
not imported, precisely so this runs without HA).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT = REPO_ROOT / "custom_components" / "ev_plug_charging"
DASHBOARDS = [REPO_ROOT / "dashboard.yaml", REPO_ROOT / "dashboard-mushroom.yaml"]

PLATFORM_MODULES = [
    "select.py",
    "switch.py",
    "number.py",
    "time.py",
    "sensor.py",
    "binary_sensor.py",
    "button.py",
]

# The one documented find-and-replace: entities owned by the user's own
# plug integration, which this integration reads but does not create.
PLACEHOLDER = "YOUR_PLUG"

# Entity-ID prefix the dashboards assume, i.e. the device renamed to "EV".
PREFIX = "ev_"


def _slugify(name: str) -> str:
    """Home Assistant's entity-ID slugify, closely enough for names made
    of ASCII words, digits and punctuation (which all of ours are)."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower())
    return re.sub(r"_+", "_", slug).strip("_")


def _display_names() -> set[str]:
    """Every entity display name the integration creates, pulled out of
    the platform modules by parsing their source.

    Matches two shapes, both of which pass (key, name) as adjacent string
    literals: a direct `super().__init__(coordinator, entry, key, name)` /
    `Entity(coordinator, entry, key, name)` call, and number.py's
    `_NumberSpec(key, name, ...)` table.
    """
    names: set[str] = set()
    for module in PLATFORM_MODULES:
        source = (COMPONENT / module).read_text()
        tree = ast.parse(source, filename=module)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            strings = [
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            # (key, name) are always the FIRST two positional string
            # literals -- not the last two: _NumberSpec also passes icon
            # and unit after them. Shape check keeps unrelated calls out:
            # key is lower_snake_case, name is human text starting with a
            # capital, so ("switch", "turn_on") and ("_", " ") don't match.
            if len(strings) >= 2:
                key, name = strings[0], strings[1]
                if re.fullmatch(r"[a-z0-9_]+", key) and name[:1].isupper():
                    names.add(name)
    return names


def _valid_suffixes() -> set[str]:
    return {_slugify(name) for name in _display_names()}


def _walk_entity_refs(node) -> list[str]:
    """Collect every entity ID a Lovelace config refers to, wherever it
    appears: `entity:`, `entities:` (bare strings or row mappings),
    `conditions[].entity`, and `target.entity_id`."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("entity", "entity_id") and isinstance(value, str):
                found.append(value)
            elif key == "entities" and isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        found.append(item)
                    else:
                        found.extend(_walk_entity_refs(item))
            else:
                found.extend(_walk_entity_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_entity_refs(item))
    return found


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_dashboard_parses(path: Path):
    config = yaml.safe_load(path.read_text())
    assert "views" in config, f"{path.name} must define views"
    assert config["views"], f"{path.name} has no views"
    for view in config["views"]:
        assert view.get("cards"), f"a view in {path.name} has no cards"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_every_entity_reference_exists_or_is_a_placeholder(path: Path):
    config = yaml.safe_load(path.read_text())
    valid = _valid_suffixes()
    unknown: list[str] = []

    for ref in _walk_entity_refs(config):
        if PLACEHOLDER in ref:
            continue  # the documented find-and-replace
        domain, _, object_id = ref.partition(".")
        if not object_id:
            continue
        if not object_id.startswith(PREFIX):
            unknown.append(f"{ref} (not prefixed {PREFIX!r} and not a placeholder)")
            continue
        if object_id[len(PREFIX) :] not in valid:
            unknown.append(f"{ref} (no entity with that name is created)")

    assert not unknown, (
        f"{path.name} references entity IDs the integration does not create:\n  "
        + "\n  ".join(sorted(set(unknown)))
        + "\n\nValid suffixes are derived from the platform modules' display "
        "names -- if you renamed an entity, its entity ID moved, and the "
        "dashboards need updating to match."
    )


def test_placeholder_is_documented_in_both_dashboards():
    """The placeholder is only safe if the header explains it."""
    for path in DASHBOARDS:
        text = path.read_text()
        if PLACEHOLDER in text:
            header = text[: text.index("views:")]
            assert PLACEHOLDER in header, (
                f"{path.name} uses {PLACEHOLDER} but its header never tells the "
                "user to replace it"
            )


def test_name_extraction_found_the_expected_entities():
    """Guards the extraction itself: if the (key, name) parsing silently
    stopped matching, every other assertion here would pass vacuously."""
    suffixes = _valid_suffixes()
    # A representative spread across all seven platforms.
    for expected in (
        "charge_mode",  # select
        "enabled",  # switch
        "target_soc",  # number
        "window_start",  # time
        "projected_soc",  # sensor
        "soc_stale",  # binary_sensor
        "refresh_source",  # button
    ):
        assert expected in suffixes, f"extraction missed {expected!r}"
    assert len(suffixes) >= 25, f"only found {len(suffixes)} entity names"
