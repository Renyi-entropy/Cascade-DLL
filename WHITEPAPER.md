# Cascade PLL: A Variable-Gain Kuramoto Telemetry Substrate

**Author:** Martin O'Flaherty
**Repository:** https://github.com/Renyi-entropy/Cascade-DLL
**Date:** 2026-08-16

---

## Abstract

Cascade PLL is a cascade of RK4-integrated Kuramoto phase oscillators
(`θ̇ = ω + k·sin(target − θ)`), each level forced toward a moving target
derived from the level below, with coupling gain that varies over time
rather than staying fixed. It is driven entirely by real external
telemetry — live crypto price, real CPU load, real network latency —
not synthetic input. On top of that substrate sits a trust/quorum gate
(`truthd`), a job-table self-heal daemon (`mesh_healer`), and a set of
"ghost-reading" tools that report the substrate's state honestly,
including how stale any given reading is. This paper documents what has
been empirically proven about the substrate's control-theoretic
behavior, and separately describes the operational architecture built
on top of it.

The name is deliberately unglamorous. Earlier candidates (`Trust-Gated
Cascade`, `Coherence-Gated Cascade`) implied that the specific
*informed* judgment behind the gain — coherence, anchoring, stability —
is what makes the system work. A controlled ablation (Section 2) showed
that claim is not established. The one property actually proven
necessary is that gain *varies*; not what decides the variation.

---

## 1. Architecture

```
market_layer0 / mint_cpu_layer0 / netlat_layer0   (Layer0: fixed reference,
              │                                    target=0, "the surveyor's
              ▼                                    stone")
           layer1                                 (entrains to Layer0's output)
              │
              ▼
           layer2                                 (entrains to Layer1's output)
```

Layer0 is a fixed reference — never moves, entrains to nothing. Layer1
entrains to Layer0's live output; Layer2 entrains to Layer1's. Trust
propagates as a relay, not a broadcast: Layer2 does not sight Layer0
directly, it trusts Layer1, which trusts Layer0. If Layer1 ever drifted
from Layer0, Layer2 would not notice directly — it would faithfully
follow Layer1's drifted output as its new reference. This is why `S`
(goal-field meta-coherence) and `r1`/`r2` are checked independently at
every layer, rather than trusting that correctness at Layer0 implies
correctness downstream.

On top of the substrate:

- **`truthd`** (`gate/`) — a quorum/trust gate, in C, reading local
  health files and serving ALLOW/DENY over a unix socket. Three tiers
  (`TIER_FULL` > `TIER_LOCAL_TRIAD` > `TIER_PARTITIONED`), each granting
  a strictly smaller set of permitted actions, fail-closed on any
  uncertainty.
- **`mesh_healer`** (`healer/`) — a job-table self-heal daemon. Each job
  carries a name (identity), a verb class (permission tier — metadata,
  not identity, since many jobs share one verb), a check function, and
  an *optional* heal function. A job with no heal function only ever
  escalates to a human; there is no safe automated action for every
  failure mode (a full disk has no automated fix). No job's heal action
  is ever chosen by a model — every heal is a fixed function picked by
  code, proven live: a real `pi2` WireGuard handshake failure was
  detected, healed via the correct fixed action, and verified — with no
  human in the loop and no LLM deciding what to do.
- **Readers** (`readers/`) — `ask.py`, `ask_node.py`, `ask_web.py`. Each
  answers a question grounded in real, timestamped data, using Groq
  only to narrate what the data already shows, never to decide
  anything.
- **Channels** (`channels/`) — `shell_launch` (unprivileged, signed,
  real-time remote exec, replacing SSH for latency-jitter reasons),
  `pi_intent`/`ec2_intent` (signed, scheduled, whitelisted-intent
  channels).

## 2. The Core Principle: Ghosts, Not Truth

No component in this system claims to read live truth. Every reading is
a **ghost** — a real, permanently-true trace of what was measured at
its own last-updated time — and every consumer of that reading is
required to state how fresh it is before treating it as current. A log
line asserting `r1=0.986` at tick 143596 is permanently true about that
instant; it says nothing about now unless its age is checked against a
real clock.

This is enforced in code, not just documented: `mesh_healer`'s
freshness jobs check real file `mtime`, not a log's internal
elapsed-seconds counter (which is relative to process start and
useless for judging current staleness); `ask.py` computes and states an
explicit age for every source it reads, and instructs its own model
never to describe stale data as "currently happening." The distinction
matters operationally, not just philosophically: a process can be
`alive` (a `pgrep` check passes) while being completely wedged and
producing nothing — proven by a real teardown test (Section 4) in which
every "alive" check on four killed processes correctly failed within
one cycle, alongside separately-tracked freshness checks confirming the
same nodes' outputs had gone stale.

Even the clock used to judge freshness is itself checked against an
independent reference (`mint_clock_drift`, validated against Groq's
signed `ec2_intent` channel, ~162ms RTT, offset consistently 0-1s
against real samples) rather than trusted blindly — because a
freshness check is only as honest as the clock it's measured against.

## 3. Empirical Result: Variable-Gain Ablation

**Setup.** Coupling gain at each level is normally set by a
multiplicative trust product: upstream coherence (`r`), the upstream
signal's own anchoring to its reference, and the level's own tracking
stability.

**Question.** Does the specific, informed judgment behind that gain
(coherence-aware gating) do real work, or does the system's behavior
come from something simpler — the fact that gain varies at all?

**Method.** A three-arm ablation, holding everything else fixed (same
real injected event — a precisely-timed CPU load spike — same depth,
same measurement window):

1. **Fixed** — gain held at a constant, calibrated to the real
   trust-gated version's own mean.
2. **Trust-gated** — the real mechanism (coherence + anchoring +
   stability).
3. **Random** — gain drawn independently each tick from a distribution
   matched to the real trust-gated version's mean *and* standard
   deviation, with zero dependence on any real signal.

Sensitivity to the injected event was measured as a mean-shift z-score
(spike-window mean vs. pre-spike baseline, in units of baseline
standard deviation) on the residual tracking error, at two cascade
depths.

**Result:**

| Depth | Fixed | Trust-gated | Random |
|---|---|---|---|
| 2 | 0.000 | 0.480 | 0.270 |
| 3 | 0.000 | 0.396 | 0.780 |

Fixed gain collapses the tracking residual to an exact fixed point of
the dynamics (`sin(err) ≈ ω/k`, both terms now constant) — zero
measured response to the real input, at both depths, not just weaker
response. Trust-gated and random both show real, non-zero sensitivity,
and random is not reliably weaker than trust-gated — it exceeds it at
depth 3.

**Interpretation.** The one property shown to be necessary is that gain
not be frozen — consistent with the standard sensitivity-function
tradeoff in feedback control (high fixed gain reduces steady-state
error but also reduces differential responsiveness to further input
change). What is *not* shown is that the informed component of the
gating — reacting specifically to coherence rather than varying by any
other means — adds anything beyond mere variability. This was checked,
not assumed: the random arm is a genuine control, matched in first and
second moments to the real signal, not a strawman.

**Open question.** Is "informed gating ≈ matched-variance random
gating" itself an expected result in adaptive/gain-scheduled control,
or is the apparent equivalence here worth investigating further —
possibly under different disturbance classes than the single tested
case (a step-like CPU load spike)?

## 4. Further Validated Results

**The lock is real and holds.** `market_layer0 → layer1 → layer2`
against a live Binance SOL feed: `r → 1.000`, `S → 0.999` (long-horizon
occupancy-weighted coherence) sustained for 15+ hours continuous with a
single real source; `S≈0.85-0.88` with real 3-source consensus
(market/mint/netlat) after `netlat` was added, correctly recalibrated
from the new real distribution rather than left at the old threshold.

**The residual carries real information in its normal operating
regime.** On live market data, Layer 1's `tracking_err1` correlated
`-0.87` with `gain` (itself driven by real volatility) over 88k+
samples.

**It is specifically blind to fast events.** A precisely-timed CPU load
spike: raw telemetry detected it immediately (`z≈7-9`); the residual
showed no reliable response (`z≈0-1.4`). Confirmed independently
against real Deluge traffic: a sustained single-core deviation locked
*more* tightly (smaller residual) the more anomalous it got, since
`gain ∝ |deviation|`.

**Depth alone (no gain variation) produces total insensitivity, not
graceful smoothing.** Holding gain fixed at a realistic constant
collapses `tracking_err` to an exact geometric constant that never
moves regardless of real input — `z = 0.000` exactly, at every depth
≥ 2.

**Oscillator phase-consensus captures cross-signal information a plain
per-signal metric structurally cannot.** For single-signal anomaly
detection, oscillator and plain z-score threshold matched exactly — no
benefit. For genuine 3-signal consensus, real correlation between `r1`
and average `|z|` was ≈ -0.05 over 9 real minutes — not redundant
information.

**A full teardown-and-relocate survives cleanly.** Every process was
stopped, every file moved into this repository's current layout, every
broken path fixed, everything restarted — and the substrate
re-converged to `S≈0.84` within ~20 minutes, unattended. Two real bugs
were found and fixed in the process (a `systemd` unit crash-loop from a
missed `StandardOutput` path, a key-path resolution bug), both caught
by verification, not assumed away.

**Self-healing works unattended, on a real fault.** `pi2`'s WireGuard
handshake genuinely went stale during operation; `mesh_healer` detected
it, executed the correct fixed heal action (`restart_wg`), and verified
recovery — with no human intervention and no model deciding the action.

**It is not fractal, not connected to Riemann zeta, not
"entropy-rejecting" in any free sense.** Each claim tested
independently against real code and real data; each a clean negative
result.

## 5. What Is Explicitly Not Claimed

The substrate never decides for itself. Every consumer built on top of
it reads a signal and reports; the decision to act, where one exists,
is either a fixed deterministic function chosen by code (`mesh_healer`'s
heal actions) or a human. Proposals to wire the substrate's own signal
into self-directed action, or to let a language model choose an
action rather than narrate one, were both raised and declined during
this project's development, for the same reason: an LLM narrating
"what happened" is a reporting function with a known failure mode
(occasional misreading, always visible in the output); an LLM deciding
"what to do next" would be a decision function whose failure mode is
invisible until it acts.

---

## License

MIT — see `LICENSE`.
