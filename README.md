# cascade_pll — Cascade Phase-Locked Loops

A Kuramoto-oscillator telemetry cascade (`market`/`mint`/`netlat` → Layer1 →
Layer2), a trust/quorum gate (`truthd`), a self-heal job-table daemon
(`mesh_healer`), and a pair of "ghost-reading" CLI tools (`ask.py`,
`ask_node.py`) that report on it. Reorganized into this layout 2026-08-15;
see memory `project_variable_gain_cascade.md` and `project_mesh_healer_job_table.md`
for the fuller build history.

## The one rule everything else follows

**Nothing here reads "the truth." Everything reads a ghost — a real trace of
what was measured at its own last-updated time — and reports how fresh that
ghost is.** A log line is permanently true about the instant it was written;
it says nothing about now unless you check its age. Every check in this
repo (`ask.py`'s `age_str()`, `mesh_healer`'s `_fresh` jobs, `truthd.c`'s
`HEALTH_STALE_S`/`EC2_STALE_S`) exists to make that age explicit instead of
assumed. Nothing here ever claims certainty it doesn't have — degrade
honestly (report stale, or fail closed) rather than guess.

**No LLM in this repo ever decides an action.** Groq/`groq_client.py` is
used only to narrate what already happened, in every consumer
(`consensus_explainer.py`, `anomaly_explainer.py`, `mesh_healer.py`,
`ask.py`/`ask_node.py`). Every heal action is a fixed function chosen by
code. This was an explicit design decision, not an oversight — see the
"orchestrator" discussion in project history for why.

## Layout

```
cascade_pll/
├── gate/                  truthd -- the quorum/trust constitution (C)
│   ├── truthd.c            reads quartz_peer_health.json + ec2_reachable.json,
│   │                       serves ALLOW/DENY over a unix socket
│   ├── truth_manifest.h    the fixed verb-class permission table
│   └── truthd_client.py    Python client any intent listener calls before acting
│
├── channels/               signed transport -- how instructions actually reach a node
│   ├── shell_launch/       unprivileged real-time remote-exec (replaces SSH for Pi1/Pi2)
│   ├── pi_intent_*.py      signed, scheduled, whitelisted-intent channel to a Pi
│   ├── ec2_intent_*.py     same shape, separate trust domain, targets EC2
│   ├── ec2_probe.py        Mint-only EC2 reachability sensor; relays its result
│   │                       to pi1/pi2 over shell_launch so their OWN truthd can
│   │                       see it too (each host's truthd only reads local files)
│   └── *.key                per-host HMAC keys (never commit, never git add)
│
├── readers/                 ghost-reading CLI tools -- ask a question, get an
│   ├── ask.py                honest, age-labeled answer, decide nothing
│   ├── ask_node.py           ask.py pointed at a remote Pi via shell_launch
│   └── groq_client.py       shared Groq wiring: model name + quota backoff
│
├── healer/
│   └── mesh_healer.py       job-table daemon: (name, verb, check, heal-or-None)
│                             per job. verb is metadata (permission tier), not
│                             identity -- see project_mesh_healer_job_table.md
│
├── maintenance/
│   └── log_trimmer.py       bounds the high-churn raw logs to ~10min; never
│                             touches layer1.log/layer2.log (the long-run record)
│                             or any event-only log (deletes real incident history)
│
├── market_layer0/  layer1/  layer2/  netlat_layer0/  mint_cpu_layer0/
│   the oscillator substrate itself -- Layer0 is the fixed reference
│   ("the surveyor's stone"), Layer1 entrains to Layer0's output, Layer2
│   entrains to Layer1's -- a relay of trust, not everyone sighting the
│   original stone directly
│
├── regime_classifier/       CALM/TURBULENT hysteresis over Layer1's live r1
├── deluge_watch/             per-core CPU sustained-deviation watch + its own
│                             Groq narrator (anomaly_explainer.py)
├── consensus_explainer.py    dual-trigger narrator: per-source z-spike OR
│                             cross-source phase disagreement
└── consensus_test_simple.py  the plain-rolling-z-score comparison that proved
                                oscillator consensus captures something a
                                per-signal metric structurally can't
```

## Quick status checks

```
cd readers && python3 ask.py "how's system health?"
cd readers && python3 ask.py --trend "what's the trend over this window?"
cd readers && python3 ask_node.py pi1 "how's it doing?"
grep goal_field ../layer1/layer1.log | tail -1     # S
tail -5 ../regime_classifier/regime.log             # CALM/TURBULENT
tail -20 ../healer/mesh_healer.log                  # all 19 jobs, silent = healthy
```

All of `ask.py`/`ask_node.py` require `GROQ_API_KEY` in the environment.
`groq_client.py` shares a daily/per-minute quota across every consumer in
this repo — if several fire near-simultaneously, later ones degrade to a
clean `QuotaExceeded` message, not a crash or a silent wrong answer.

## The trust gate, briefly

Three tiers (`truth_manifest.h`): `TIER_FULL` (EC2 + local triad all
healthy) > `TIER_LOCAL_TRIAD` (EC2 unreachable, Mint+Pi1+Pi2 agree) >
`TIER_PARTITIONED` (fewer than 2 of 3 mutually healthy). Each tier grants
a strictly smaller set of verb classes (`BENIGN_READ` always allowed,
`LOCAL_HEAL`/`LOCAL_DESTRUCTIVE`/`EC2_SELF` progressively gated) --
enforced server-side by each host's own `truthd`, fail-closed on any
uncertainty. `mesh_healer` never routes around a `REJECTED` gate response;
it logs it and waits for the next cycle.

## What's still open

- `ec2_intent_send.py`'s `phase_auth.gate_check()` depends on "AxisPulse,"
  part of the beacon/timing-swarm system decommissioned 2026-07-29 --
  nothing broadcasts it anymore, so that channel currently always fails
  its own precondition. Not fixed yet; needs a real decision (drop the
  check or wire a replacement), not a quick patch.
- Tier 2 observer-drift (validating Mint's own system clock against an
  independent reference, not just trusting its own `mtime`) is parked --
  see `project_mesh_healer_job_table.md`. No real symptom has motivated
  it yet, unlike everything else in `mesh_healer`.
