---
name: testing
description: Load before adding or changing any test, or before claiming a change is verified. Explains the two test tiers, why a green plain pytest run does not prove the HA-facing layer works, the R1-R14 acceptance gate, and the environment recipe for the real-HA suite.
---

# Testing

## Two tiers, and what each proves

**Tier 1 — plain `pytest`, no Home Assistant installed, no `aiohttp`.**
Covers everything in the pure core: `logic.py`, `session.py`,
`rate_model.py`, `models.py`, `store.py`, `source.py`, plus the
source-specific parsing in `sources/psacc.py`. This is most of the test
suite (`tests/test_scenarios.py`, `test_session.py`, `test_rate_model.py`,
`test_logic_*.py`, `test_source_*.py`, `test_dashboard_examples.py`).

**Tier 2 — `tests/test_integration_setup.py`, using
[`pytest-homeassistant-custom-component`](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)**,
a real (if lightweight) Home Assistant instance. Covers the config flow, the
coordinator's HA-facing behaviour, and every entity platform actually being
set up, added, and torn down correctly.

**Tier 2 auto-skips when the dependency isn't installed.** That means `pytest
-q` can report "all green" while tier 2 silently ran zero tests. This is not
hypothetical: three real bugs in this codebase were invisible to tier 1 and
were only caught once tier 2 actually ran —

- `_attr_entity_category = "diagnostic"` (a bare string) — Home Assistant
  raises `ValueError: entity_category must be a valid EntityCategory
  instance` at entity-add time, which only happens under a real HA instance.
- The coordinator guessing an entity's `entity_id` by string-formatting it
  from `unique_id` — wrong the moment a user renames the device, and only
  observable once real entity registration is exercised.
- A teardown/unload bug that only a real config-entry lifecycle triggers.

**So: when reporting verification, say which tier actually ran.** "101
tests passed" is not informative on its own if tier 2 was skipped — say
"101 passed, tier 2 (N tests) ran / tier 2 skipped (dependency not
installed)" explicitly.

## R1–R14 is the acceptance gate, not an ordinary regression suite

`tests/test_scenarios.py` reproduces fourteen scenarios from the upstream
design record, each one a real incident that happened once in production.
Any change to `logic.py`, `session.py`, or `rate_model.py` must leave every
one of R1–R14 passing. **Never edit a scenario to make a change pass** — a
scenario failing after a change means the change regressed real behaviour;
if a scenario is genuinely believed wrong, that is a design conversation to
have explicitly, not a quiet edit buried in an unrelated diff.

## The environment recipe for tier 2

Setting up `pytest-homeassistant-custom-component` from a clean environment
hits a real build failure: `PyRIC` (a transitive HA dependency) fails to
build with `AttributeError: install_layout` under modern `setuptools`. Fix,
in order:

```bash
python -m venv .venv
source .venv/bin/activate
pip install tzdata   # PyRIC's build also needs this present
SETUPTOOLS_USE_DISTUTILS=stdlib pip install -r requirements-test.txt
pip uninstall -y aiodns pycares   # see below
```

Use an isolated venv rather than the system Python — resolving `ruff` or
`pytest` from the wrong environment has already produced a false "clean"
report in this project once (a different `ruff` binary silently missed an
unused import; see also `.github/workflows/test.yml`'s pinned `ruff==` for
why CI doesn't resolve "whatever's latest" either).

**`aiodns`/`pycares` must not be present when tier 2 runs.**
`homeassistant` hard-depends on `aiodns` -> `pycares` (a c-ares DNS
binding). Its mere presence flips `aiohttp`'s `DefaultResolver` to
`AsyncResolver`, which spawns a background reactor thread
(`_run_safe_shutdown_loop`) the first time a `ClientSession` is created --
e.g. via `async_get_clientsession()` in `sources/psacc.py`. That thread
doesn't reliably finish unwinding before
`pytest-homeassistant-custom-component`'s autouse `verify_cleanup` fixture
checks for leaked threads after every test, which fails the test with an
unrelated teardown `AssertionError` even though the test body itself
passed -- this is timing-dependent, so it can pass locally and still flake
in CI (or on one Python version and not another; it's what
`.github/workflows/test.yml`'s CI matrix hit on 3.12 while 3.13 happened to
get lucky). Tests never need real DNS resolution -- everything is mocked --
so `pip uninstall -y aiodns pycares` after installing requirements removes
the thread entirely; `aiohttp` falls back to its plain `ThreadedResolver`,
which leaves nothing behind. `.github/workflows/test.yml` does this as a
step, not requirements-test.txt, because there's no clean way to exclude a
transitive dependency from a plain `pip install -r` line.

## The purity test

The pure core's zero-import property (see `AGENTS.md`) is verified
concretely, not just asserted: uninstall Home Assistant entirely (or use an
environment where it was never installed) and run tier 1. It must still pass
in full. This is what the deferred-import pattern exists to make possible —
see `sources/psacc.py`'s `_get_json()`, which imports `aiohttp` inside the
method body rather than at module scope, and `__init__.py`'s deferred HA
imports with `TYPE_CHECKING` for type annotations. Any new module-scope
`import homeassistant...` or third-party import in a core or
`sources/__init__.py`-registry module breaks this property immediately —
check for it explicitly in review, since it will not fail loudly until
someone actually tries to run tier 1 without HA installed.

## Tests that guard documentation, not code

`tests/test_dashboard_examples.py` is unusual: it doesn't test the
integration's behaviour, it tests that `dashboard.yaml` and
`dashboard-mushroom.yaml` only reference entity IDs the integration actually
creates. It does this by **AST-parsing the platform modules**
(`sensor.py`, `binary_sensor.py`, etc.) to derive the set of valid entity-name
suffixes, rather than maintaining a hand-written list that would drift. It
carries its own self-guard test,
`test_name_extraction_found_the_expected_entities`, which exists so that if
you change the *shape* of the platform modules (how entity names are passed
to the base class), you notice the extractor needs updating rather than
silently extracting nothing and passing vacuously. If you add or rename an
entity, this is the test that catches a dashboard left pointing at the old
name — treat a failure here as a real bug, not a stale fixture to delete.

## Honest verification

When you report that a change is verified: run `pytest -q` and
`ruff check custom_components/` yourself, from the recipe above, and report
what actually happened — pass/fail counts, and explicitly whether tier 2 ran
or was skipped. Don't report "tests pass" from a partial run, and don't
report a lint result from a tool resolved outside the project's own venv.
