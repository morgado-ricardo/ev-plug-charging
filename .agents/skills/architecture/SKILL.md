---
name: architecture
description: Load before changing logic.py, session.py, rate_model.py, the coordinator, or anything that spans layers. Explains the layer boundaries, the reducer contract, why reduce()'s priority ladder is ordered the way it is, and the invariants a diff must not regress.
---

# Architecture

## The four layers, one-way dependency

```
pure core  →  source adapters  →  coordinator  →  entity platforms
(logic.py,     (sources/*.py)      (coordinator.py)  (sensor.py, switch.py, …)
 session.py,
 rate_model.py,
 models.py,
 store.py,
 source.py)
```

Nothing points back up. The pure core doesn't know a coordinator exists;
sources don't know entities exist. See `AGENTS.md`'s repository map for which
files are in which layer and the zero-import property that enforces it.

## The reducer contract

`logic.py:1-17`'s module docstring states it precisely:

    new_state, decision = reduce(prev_state, inputs)

`reduce()` (`logic.py:204`) is **pure and total**: same `prev` and `inp` in,
same `(new_state, Decision)` out, no I/O, no clock of its own — `inp.now` is
an input, not `datetime.now()`. Every behaviour ported from the YAML is
implemented as **arithmetic over timestamps**, never as an edge-triggered
callback. The reason: an edge-triggered callback only fires once, at the
instant of the edge — if the coordinator tick that would have seen it gets
missed (a restart, a slow poll), the edge is gone forever. Timestamp
arithmetic can be re-evaluated on every tick and gets the same answer
regardless of which tick it runs on. This is what R1, R4 and R8 (a restart
losing state) actually test.

`SessionState` (`models.py`) is everything that must survive a restart —
ownership, anchors, one-shot latches. `Inputs` is a snapshot of the world
right now, built fresh by the coordinator each tick.

## Why the priority ladder in `reduce()` is ordered by safety, not readability

`reduce()` checks conditions in this order (`logic.py:390-580` roughly);
each rung exists because a different rung being checked first caused a real
incident:

1. **Overheat, latched** (`logic.py:394`) — always wins, forces the plug off,
   and stays latched until a hysteresis band clears it (`OVERHEAT_HYSTERESIS_C`,
   `const.py`), so it can't chatter on/off right at the threshold.
2. **Not enabled** (`logic.py:422`) — observes and reports but never
   actuates. Disabling the integration must not stop it from telling you
   what it sees.
3. **Emergency low SoC** (`logic.py:437`) — Smart mode only, ignores the
   window entirely. A near-empty battery is worse than charging outside the
   configured hours.
4. **In window** (`logic.py:464` onward) — and *inside* this branch, the
   untrustworthy-SoC check (`logic.py:490`, `reason = "soc_untrustworthy"`)
   is tested **before** the target comparison. This order is load-bearing:
   D8's fail-closed default makes a missing/untrustworthy SoC read as 100 to
   the projection math (see `session.py:55`'s `anchor_soc = ... else 100.0`
   comment). If the target check ran first, a missing reading would look
   like "at or above target" and fire a false completion — that is exactly
   what R6 is a regression test for. Untrustworthy-SoC must be ruled out
   before target is ever compared.
5. **Outside window** (`logic.py:536` onward) — restart-reconcile, then
   window-close, then "we turned it on so we turn it off", then leave
   alone. A restart with an `UNKNOWN` owner and the plug already on must
   resolve to OFF rather than silently continuing to own a charge it can't
   verify started under this integration's control (D8/R1).

**Manual-off detection runs at the very top of the tick** (before the window
decision, in the "session bookkeeping" block starting `logic.py:238`), not
after. A human physically or via the UI turning the plug off must not be
turned back on by the same `reduce()` call that observes the new state — a
same-tick ordering bug here was an actual regression during the port.

## The projection

`projected_soc = max(reading_projection, session_projection)`, with a 1.15×
margin on the session-cap term (`SESSION_CAP_MARGIN`, `const.py:81`, its
comment records the measured 1.0×-vs-1.15× slack that justified the number —
D1/D2). The session cap must never be argued down by a reading projection
that looks more optimistic; only the max of the two is trusted.

## The anchor: matched SoC+timestamp pair

`session.py` is deliberately split out from `logic.py` — its own docstring
(`session.py:1-10`) calls it "the single largest omission a first pass at
porting the YAML tends to make." The anchor is captured **only on the
instant charging current actually starts flowing** (`charging_active_edge`),
never on plug-on (D3) — `session.py:49-68`. If that first reading is stale
(older than the expected reporting gap), the anchor is marked provisional
and corrected **exactly once**, on the first fresh reading that arrives
after it (`session.py:71-90`, D4) — the correction moves the anchor's
*value*, never its *timestamp*, so the session-cap duration still bounds the
same real wall-clock time.

The same-tick trap documented at `session.py:71-83`: on the very first
`reduce()` call ever, `state.prev_soc_changed_at` is `None`, so *any* reading
looks like a `fresh_reading_edge` — including the same stale reading the
capture logic just used on this exact tick. Without the
`not charging_active_edge` guard, that stale reading would "correct" the
anchor against itself, immediately and meaninglessly. The guard is what
makes the correction only fire on a reading that genuinely postdates the
capture.

## The rate model's one-directional guardrail

`rate_model.py`'s `effective_rate()` computes
`max(learned_rate, seed_rate)` — never the learned rate alone. This is
structural, not a clamp on one term: an earlier version of this guardrail
capped only the session-projection term, leaving the reading-projection term
free to use a fast-biased learned rate, and stopped a real session **26
points short of target**. The fix had to apply `max(learned, seed)`
everywhere the rate is used, not just once. Any future change to the rate
model must preserve `effective_rate() >= seed_rate` as an invariant, checked
directly in `tests/test_rate_model.py`.

## The coordinator is a funnel, not a brain

`coordinator.py` gathers `Inputs` from the source and from entity-pushed
settings, calls `reduce()` exactly once per tick, then actuates the plug,
persists `SessionState` via `store.py`, and dispatches `Decision.events`
through `notify.py`. It contains no charging policy of its own.

Entities never let the coordinator guess an `entity_id` by formatting a
string (e.g. `f"switch.{entry_id}_enabled"`) — **Home Assistant derives
entity IDs from the display name, not from `unique_id`**, so a guessed ID is
wrong the moment a user renames anything. Instead, entities **push** their
own value into the coordinator's `RuntimeSettings` via
`request_settings_update()`, and the coordinator reads that struct. See
`home-assistant-conventions` for the general form of this trap.

## Invariant checklist for a diff touching this layer

Before committing a change to `logic.py`, `session.py`, or `rate_model.py`,
check each of these against the diff, and name the scenario it would
regress if broken:

- [ ] Still zero HA/third-party imports (R-independent, but breaks
  `testing`'s purity check).
- [ ] Untrustworthy-SoC is still checked before the target comparison (R6).
- [ ] Manual-off detection still runs before the window decision (no
  scenario number — was a same-tick bug caught in review, not yet codified
  as a regression test; treat it as load-bearing anyway).
- [ ] The anchor is still only captured on `charging_active_edge`, never on
  plug-on (D3).
- [ ] The anchor correction still can't fire on the same tick as its own
  capture (the `not charging_active_edge` guard).
- [ ] `effective_rate() >= seed_rate` still holds for every input (the
  26-point incident).
- [ ] A restart with an unknown owner and the plug on still resolves to OFF,
  not "leave it alone" (R1/R4/R8).
- [ ] `tests/test_scenarios.py` still passes in full — R1 through R14, no
  exceptions, no edits to the scenarios themselves.
