---
name: home-assistant-conventions
description: Load before touching any entity platform, the config flow, diagnostics, services, translations, or the manifest. Lists the Home Assistant-specific traps this codebase has actually hit, each with its symptom, so they aren't rediscovered.
---

# Home Assistant conventions

These are traps that were actually hit while building this integration, not
generic HA advice. Each one cost real debugging time; several were only
caught by the real-HA test tier (see `testing`).

## Entity IDs derive from the display name, not `unique_id`

Home Assistant slugifies `<device name> <entity name>` to produce the entity
ID; `unique_id` only identifies the entity in the registry across restarts,
it does **not** determine the ID string. Renaming an entity's friendly name
(or the device's name) silently moves its entity ID. This is *why* the
shipped dashboards (`dashboard.yaml`, `dashboard-mushroom.yaml`) assume the
device has been renamed to `EV`, and why `tests/test_dashboard_examples.py`
exists — a plausible-looking rename can quietly break every dashboard built
against the old ID with no error anywhere. If you rename a display name in
a platform module, run that test and update the dashboards in the same
commit. See `architecture`'s note on the coordinator never guessing an
`entity_id` by string-formatting, for the same underlying trap from a
different angle.

## `entity_category` must be an `EntityCategory` enum member

```python
# Wrong — raises ValueError: entity_category must be a valid EntityCategory
# instance, but only when the entity is actually added under a real HA
# instance. Invisible to tier-1 (no-HA) tests.
_attr_entity_category = "diagnostic"

# Right
from homeassistant.helpers.entity import EntityCategory
_attr_entity_category = EntityCategory.DIAGNOSTIC
```

## Never name a module after a stdlib module

`models.py` was originally `types.py`. That shadowed Python's own `types`
stdlib module for anything importing this package, producing a baffling
`ImportError: cannot import name 'GenericAlias' from partially initialized
module 'types'` deep in an unrelated import chain. Check a new module's name
against the stdlib before creating it.

## Deferred imports are load-bearing — don't "tidy" them

`__init__.py` and `sources/psacc.py` import `homeassistant.*` / `aiohttp`
inside function bodies (using `TYPE_CHECKING` for type annotations at module
scope) rather than at the top of the file. This is what keeps the pure-core
and source-registry modules importable with no Home Assistant or
`aiohttp` installed at all — see `AGENTS.md`'s zero-import rule and
`testing`'s purity test. Moving one of these imports to module scope "to
match normal style" silently breaks that property; it won't fail until
someone runs the no-HA test tier.

## Config-entry evolution needs a migration, not a default

Adding, renaming, or reinterpreting a config key means bumping
`CONFIG_VERSION` and adding a branch to `async_migrate_entry()` in
`__init__.py` — not quietly reading the new key with `.get(KEY, default)`
and hoping old entries happen to work. A `.get()` default papers over the
gap for new installs but leaves existing installs in an unmigrated,
undocumented state. `__init__.py`'s existing migration handling the old
`charge_efficiency`/`soc_step_minutes` numeric constants (ported from
`packages/ev_charging.yaml:1097-1165`) is the template to follow.

## Timezone discipline

Everything in this codebase is timezone-aware. Combining a bare `date` with
a `time` value needs an explicit `tzinfo`:

```python
# Wrong — TypeError: can't compare offset-naive and offset-aware datetimes
datetime.combine(inp.now.date(), inp.window_end)

# Right
datetime.combine(inp.now.date(), inp.window_end, tzinfo=inp.now.tzinfo)
```

## Strings and translations move together

Any new config-flow field, option, or selectable value needs an entry in
`strings.json` **and** both `translations/en.json` and
`translations/pt.json` — the repository ships Portuguese as a first-class
translation, not an afterthought; don't add English-only and leave the
Portuguese file stale.
