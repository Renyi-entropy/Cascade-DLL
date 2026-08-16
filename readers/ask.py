"""ask.py — manual one-off query against the live substrate's current
state, using the same Groq wiring as consensus_explainer.py /
anomaly_explainer.py. Not a trigger-driven consumer -- run it whenever
you want a plain-English read of what's happening right now. Same
boundary as the automated consumers: reads logs, reports, decides
nothing.

Pulls the regime as an explicit CURRENT_STATE label (the string after
the last "->") rather than handing the model the raw transition line --
a first version handed over "TURBULENT -> CALM" verbatim and the model
misread the direction, reporting the machine as turbulent when the log
said it had just returned to calm.
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from groq_client import query_llm as _query_llm, QuotaExceeded

# ask.py lives one level deeper now (cascade_pll/readers/) after the
# 2026-08-15 reorg -- REPO (where all the log files actually live) is
# the cascade_pll/ root, one dir up from this file, NOT this file's own
# directory (that's only correct for finding groq_client.py, right
# above).
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tail_line(path, n=1):
    try:
        with open(path) as f:
            lines = [l.rstrip("\n") for l in f if l.strip()]
        return "\n".join(lines[-n:]) if lines else "(no data)"
    except FileNotFoundError:
        return "(log not found)"


def age_str(path):
    """Real wall-clock age of a source's last write (mtime), not the
    elapsed-seconds label INSIDE the log content -- that label is
    relative to when the writer process started, not to now, and can't
    tell you whether the writer is still alive. Added 2026-08-15: a
    prior version handed the model only the raw log content and relied
    on it to infer recency from the internal label, the same kind of
    inference that already caused one real misread (the regime
    CURRENT_STATE bug this file's own docstring documents). This makes
    staleness an explicit, pre-computed fact instead of something the
    model has to guess at -- "reads the ghost, tells you how fresh it
    is," not "reads the ghost and hopes the reader notices it's old.\""""
    try:
        age = time.time() - os.path.getmtime(path)
    except FileNotFoundError:
        return "no data file found"
    if age < 60:
        return f"{age:.0f}s ago"
    if age < 3600:
        return f"{age / 60:.1f}m ago"
    if age < 86400:
        return f"{age / 3600:.1f}h ago"
    return f"{age / 86400:.1f}d ago"


def current_regime():
    line = tail_line(os.path.join(REPO, "regime_classifier/regime.log"))
    m = re.search(r"->\s*(\w+)\s+r_ema=([\d.]+|n/a)", line)
    if not m:
        return "unknown"
    return f"{m.group(1)} (r_ema={m.group(2)})"


def mesh_health_summary():
    """Real state of mesh_healer's 20 jobs -- including wg_handshake_fresh
    and listener_responsive for pi1/pi2, which is the ACTUAL WireGuard
    mesh state. Added 2026-08-16: a real user question ("is wireguard
    mesh healthy?") got answered using netlat (generic internet ping
    RTT) because that was the closest thing in the old prompt, which
    LOOKED like it addressed the question without actually doing so --
    not a hallucination, just the wrong data for the question asked.
    Silent in the log means healthy, by mesh_healer's own design --
    reconstruct current state by replaying ESCALATED/"healthy again"
    transitions in order, not by reading only the tail."""
    path = os.path.join(REPO, "healer/mesh_healer.log")
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return "(mesh_healer.log not found)", "no data file found"

    escalated = {}
    for line in lines:
        m = re.search(r"\[mesh_healer\] (\S+): ESCALATED", line)
        if m:
            escalated[m.group(1)] = True
            continue
        m = re.search(r"\[mesh_healer\] (\S+): healthy again", line)
        if m:
            escalated[m.group(1)] = False

    still = sorted(tag for tag, esc in escalated.items() if esc)
    summary = (f"ESCALATED (no automated fix ran, needs human attention): "
               f"{', '.join(still)}" if still else
               "no jobs currently escalated (includes pi1/pi2 WireGuard "
               "handshake freshness and listener responsiveness)")
    return summary, age_str(path)


def query_llm(prompt):
    try:
        return _query_llm(prompt, user_agent="deluge-watch-ask/1.0")
    except QuotaExceeded as e:
        # The two automated consumers (consensus_explainer.py,
        # anomaly_explainer.py) share this same free-tier key and can
        # burst close to its quota on triggers -- a manual query landing
        # in that window gets refused up front now instead of 429ing.
        sys.exit(f"[ask] Groq quota exceeded -- retry in {e.retry_after_s:.0f}s.")


def goal_field_history(n=20):
    lines = [l for l in tail_line(os.path.join(REPO, "layer1/layer1.log"), n=2000)
             .splitlines() if "goal_field" in l]
    return "\n".join(lines[-n:]) if lines else "(no S history yet)"


def build_prompt(question):
    z_path = os.path.join(REPO, "simple_consensus.log")
    layer1_path = os.path.join(REPO, "layer1/layer1.log")
    regime_path = os.path.join(REPO, "regime_classifier/regime.log")
    sustained_path = os.path.join(REPO, "deluge_watch/sustained.log")

    z = tail_line(z_path)
    r1 = tail_line(layer1_path)
    s_line = next((l for l in reversed(tail_line(layer1_path, n=200).splitlines())
        if "goal_field" in l), "(no S reading yet)")
    regime = current_regime()
    sustained = tail_line(sustained_path, n=3)
    mesh_summary, mesh_age = mesh_health_summary()

    return f"""You are reading real telemetry from a substrate on a Linux workstation.
Every reading below is a GHOST, not a live truth -- a real trace of what
was measured at its own last-updated time, not necessarily what's true
right now. Its "last updated" age tells you how much to trust it as
current. Three independent real sources feed the substrate: "market"
(live crypto price), "mint" (this machine's real CPU load), "netlat"
(real network latency).

Per-source rolling z-scores (last updated {age_str(z_path)}): {z}
Cross-source oscillator coherence (last updated {age_str(layer1_path)}): {r1}
Goal-field meta-coherence S (last updated {age_str(layer1_path)}): {s_line}
Regime classifier's CURRENT state (last updated {age_str(regime_path)}): {regime}
Per-core sustained-deviation watch (last updated {age_str(sustained_path)}): {sustained}
  -- each entry is tagged "process" (real application CPU work on that
  core) or "network" (kernel softirq/network-interrupt processing). Only
  a "network"-tagged entry has any established link to network latency;
  a "process"-tagged entry does not.
Mesh/WireGuard health -- mesh_healer's 20 real jobs, covering pi1/pi2
WireGuard handshake freshness, remote listener responsiveness, disk
space, and every local substrate process (last updated {mesh_age}):
  {mesh_summary}
  -- THIS is the actual WireGuard mesh state. "netlat" above is
  unrelated (generic internet ping RTT, not WireGuard) -- do not use it
  to answer a question about the mesh or WireGuard specifically. Unlike
  the other sources, an OLD age here does not imply staleness -- this
  log only writes on state transitions, so a large age with "no jobs
  currently escalated" means the mesh has been quietly healthy the
  whole time, not that monitoring stopped.

Question: {question}

Answer in plain English, 3-5 sentences, based only on this data. If any
source's "last updated" age is more than a couple of minutes, say so
explicitly and describe it as the last known reading, not as current
state -- do not describe stale data as "currently happening." State the
regime classifier's CURRENT state exactly as given -- do not infer a
different direction of change than what's labeled. These sources are
independent signals, not necessarily causally linked -- do not suggest a
causal relationship between two sources (e.g. CPU load on a core and
network latency) unless the data itself supports that link (e.g. a
"network"-tagged sustained-deviation entry for a latency question). If
a metric is already near its own calibrated baseline/target, say so
plainly instead of proposing a fix for a problem that isn't present."""


def build_trend_prompt(question):
    # Windowed history instead of a single tail line -- a point-in-time
    # snapshot can't say whether S is drifting or the regime is flapping,
    # only a trend query can. Regime log is small (only transitions get
    # written) so the whole thing is the real transition history.
    layer1_path = os.path.join(REPO, "layer1/layer1.log")
    regime_path = os.path.join(REPO, "regime_classifier/regime.log")
    sustained_path = os.path.join(REPO, "deluge_watch/sustained.log")

    s_hist = goal_field_history(n=20)
    regime_hist = tail_line(regime_path, n=50)
    sustained_hist = tail_line(sustained_path, n=30)
    sustained_events = [l for l in sustained_hist.splitlines()
                         if ">>> SUSTAINED" in l or l.strip().startswith("cleared")]
    mesh_summary, mesh_age = mesh_health_summary()

    return f"""You are reading a windowed HISTORY (not a single snapshot) from a real
telemetry substrate on a Linux workstation. Every source below is a
GHOST -- real traces of what was measured, not a live truth -- and each
one's "last updated" age tells you how current the whole history is,
not just the newest line. Three independent real sources feed the
substrate: "market" (live crypto price), "mint" (this machine's real CPU
load), "netlat" (real network latency).

Goal-field meta-coherence S, last ~20 readings (chronological, oldest
first, each line includes elapsed seconds and S; source last updated
{age_str(layer1_path)}):
{s_hist}

Regime classifier's full transition history (CALM/TURBULENT, each line
is a real state change, not a repeated reading; source last updated
{age_str(regime_path)}):
{regime_hist}

Recent sustained-deviation start/clear events (per-core CPU, "process"
vs "network" tagged; source last updated {age_str(sustained_path)}):
{chr(10).join(sustained_events) if sustained_events else "(no events in this window)"}

Mesh/WireGuard health -- mesh_healer's current job state, covering
pi1/pi2 WireGuard handshake freshness and listener responsiveness
(last updated {mesh_age}):
  {mesh_summary}
  -- THIS is the actual WireGuard mesh state, not "netlat" (generic
  internet ping RTT) above.

Question: {question}

Describe the TRAJECTORY over this window, not a single moment: is S
rising, falling, or flat; is the regime classifier flapping between
states or settled; how frequent are sustained-deviation events. If any
source's "last updated" age is stale (more than a couple of minutes,
or much older than the window this history implies), say so explicitly
-- describe the trajectory as historical up to that point, not as
still ongoing. Answer in plain English, 4-6 sentences, based only on
this data. Do not speculate about causes not supported by the data,
and do not suggest causal links between independent sources unless the
data itself supports it."""


def main():
    args = sys.argv[1:]
    trend = "--trend" in args
    args = [a for a in args if a != "--trend"]
    question = " ".join(args) or (
        "What's the trend over this window?" if trend
        else "What's currently happening on this machine?")
    prompt = build_trend_prompt(question) if trend else build_prompt(question)
    print(query_llm(prompt))


if __name__ == "__main__":
    main()
