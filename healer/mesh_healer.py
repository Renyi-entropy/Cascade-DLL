"""mesh_healer.py — job-table self-heal loop for mesh nodes (pi1, pi2).

Started 2026-08-14 as a single hardcoded check (shell_launch responsive)
+ single hardcoded heal (systemctl --user restart). Refactored
2026-08-15 into a small table of independent JOBS so new checks can be
added without touching the working one -- each job carries its own name
(identity), verb class (permission tier, from truth_manifest.h's fixed
4-value set: BENIGN_READ/LOCAL_HEAL/LOCAL_DESTRUCTIVE/EC2_SELF -- shared
across many jobs, doesn't disambiguate them), check function, and an
OPTIONAL heal function. A job with no heal function never attempts a
fix -- it only ever escalates, because for some failure modes (disk
nearly full) there is no safe automated action.

Deliberately NOT the "ask an LLM what to do" orchestrator that was
proposed and rejected in conversation: no job's heal action is chosen by
a model. Every heal is a fixed function picked by code. The LLM's only
job is narrating a job's outcome afterward, same read-only-reporter
boundary as every other consumer in this repo
([[feedback_substrate_not_a_cron_job]]).

Per (node, job), every CHECK_INTERVAL_S:
  1. Run the job's check function.
  2. Healthy -> clear that job's failure/escalation state, done.
  3. Unhealthy, job has a heal function, past cooldown -> run it, wait
     HEAL_VERIFY_DELAY_S, re-check, narrate the outcome.
  4. Unhealthy, job has NO heal function -> narrate once on first
     detection, then stay quiet (no re-narration every cycle) until
     either healthy again or escalated.
  5. After HEAL_ATTEMPTS_BEFORE_ESCALATE consecutive unhealthy cycles
     (heal attempted-and-failed, or no-heal-available), mark that
     (node, job) ESCALATED and stop attempting/re-notifying until a
     human clears it -- no infinite hammering, no infinite token spend
     narrating the same unresolved problem every cycle.
"""
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time

# mesh_healer.py lives one level deeper now (cascade_pll/healer/) after
# the 2026-08-15 reorg -- REPO is the cascade_pll/ root, two dirs up,
# not this file's own directory.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS = os.path.join(REPO, "channels")
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(CHANNELS, "shell_launch"))
sys.path.insert(0, CHANNELS)
sys.path.insert(0, os.path.join(REPO, "readers"))

from shell_launch_common import load_key as load_shell_key, send_run, PROGRESS, DONE_OK, DONE_FAIL
import pi_intent_common as pic
import ec2_intent_common as eic
import groq_client
from groq_client import query_llm, QuotaExceeded

NODES = {
    "pi1": dict(
        shell_host="10.8.0.7", shell_port=58701,
        shell_key=os.path.join(CHANNELS, "shell_launch", "shell_launch_pi1.key"),
        intent_host="10.8.0.7", intent_port=58601,
        intent_key=os.path.join(CHANNELS, "pi1_intent.key"),
        repo_path="/home/martin/shell_launch",
    ),
    "pi2": dict(
        shell_host="10.8.0.5", shell_port=58701,
        shell_key=os.path.join(CHANNELS, "shell_launch", "shell_launch_pi2.key"),
        intent_host="10.8.0.5", intent_port=58601,
        intent_key=os.path.join(CHANNELS, "pi_intent.key"),
        repo_path="/home/martin/shell_launch",
    ),
}

CHECK_INTERVAL_S = 60
HEALTH_TIMEOUT_S = 8
HEAL_VERIFY_DELAY_S = 5
HEAL_ATTEMPTS_BEFORE_ESCALATE = 3
HEAL_COOLDOWN_S = 120   # minimum real time between two heal attempts on the same job

DISK_USAGE_PCT_UNHEALTHY = 90   # unhealthy at or above this -- pi1/pi2 both sat
                                  # at 59% when this was picked, generous margin


def _shell_run(node, cmd, timeout_s=HEALTH_TIMEOUT_S):
    """Shared real request/response over shell_launch -- used by every
    check job, not just listener_responsive. Returns the joined output
    string, or None on timeout/no response."""
    cfg = NODES[node]
    key = load_shell_key(cfg["shell_key"])
    output = []

    def on_status(status, sid, detail, elapsed_s):
        if status in (PROGRESS, DONE_OK, DONE_FAIL):
            output.append(detail)

    session_id = send_run(cfg["shell_host"], cfg["shell_port"], cmd, "", key,
                           on_status, initial_timeout_s=timeout_s)
    if session_id is None:
        return None
    return "\n".join(output)


def check_listener(node):
    """Real request/response over shell_launch, not just a ping --
    matches how the outage was actually diagnosed tonight (network-up
    but listener-dead was indistinguishable from ping alone)."""
    out = _shell_run(node, "echo ok")
    return out is not None and "ok" in out


def check_disk_space(node):
    """BENIGN_READ, no heal function -- there is no safe automated fix
    for disk nearly full, so this job only ever escalates.

    The listener runs commands via shlex.split, not a shell (confirmed
    live 2026-08-14 for a different job) -- "|" needs an explicit
    bash -c wrapper or it gets passed as a literal argv token."""
    out = _shell_run(node, "bash -c " + shlex.quote("df -h / | tail -1"))
    if out is None:
        return False   # can't reach the node at all -- let listener_responsive's
                        # own job report the real reason, this just also fails safe
    m = re.search(r"(\d+)%", out)
    if not m:
        return False
    return int(m.group(1)) < DISK_USAGE_PCT_UNHEALTHY


def send_intent(node, intent, delay_s=2.0, wait_s=15.0):
    """Direct port of pi_intent_send.py's send/receive loop, reading the
    key file directly per-call instead of via pi_intent_common's
    load_key() -- that function caches KEY_PATH at first import from a
    fixed env var, which silently signs with the wrong host's key if
    you try to target more than one host from the same process
    (documented footgun in pi_intent_common.py, hit for real 2026-08-10).
    Returns (status_name, detail) or ("TIMEOUT", None)."""
    cfg = NODES[node]
    key = bytes.fromhex(open(cfg["intent_key"]).read().strip())

    trigger_at = time.time() + delay_s
    nonce = secrets.randbits(64)
    pkt = pic.pack(intent, trigger_at, nonce, key)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.sendto(pkt, (cfg["intent_host"], cfg["intent_port"]))

    deadline = time.time() + delay_s + wait_s
    while time.time() < deadline:
        sock.settimeout(max(deadline - time.time(), 0.1))
        try:
            data, _ = sock.recvfrom(512)
        except socket.timeout:
            break
        try:
            status, detail, resp_nonce = pic.unpack_response(data, key)
        except ValueError:
            continue
        if resp_nonce != nonce:
            continue
        name = {pic.ACCEPTED: "ACCEPTED", pic.REJECTED: "REJECTED",
                pic.DONE_OK: "DONE_OK", pic.DONE_FAIL: "DONE_FAIL"}.get(status, str(status))
        if name in ("DONE_OK", "DONE_FAIL", "REJECTED"):
            return name, detail
    return "TIMEOUT", None


# Validated against real samples (2026-08-15), not picked blind: 15 live
# `wg show` reads across pi1 (1 handshake) and pi2 (2, wg0+wg1) ranged
# 6s-1m57s, consistently topping out just under WireGuard's own protocol
# rekey interval (REKEY_AFTER_TIME=120s, a real protocol constant, not a
# guess). 5 min gives ~2.5x margin above that observed natural cycle --
# same spirit as the regime classifier's percentile-derived thresholds,
# though on a thinner sample (15 reads over ~2min, not tens of
# thousands) -- call this "validated with real margin," not "proven
# optimal."
WG_HANDSHAKE_STALE_MIN = 5


def _handshake_is_fresh(desc):
    if "day" in desc or "hour" in desc:
        return False
    if "minute" in desc:
        m = re.search(r"(\d+)\s*minute", desc)
        return bool(m) and int(m.group(1)) < WG_HANDSHAKE_STALE_MIN
    return True   # "N seconds ago" or "now"


def check_wg_handshake(node):
    """`wg show` needs root (CAP_NET_ADMIN) -- confirmed live 2026-08-15
    that a bare, unprivileged call fails with "Operation not permitted".
    Routed through shell_launch (not pi_intent's exec:) since this is a
    read-only check, not a gated heal action -- same sudoers NOPASSWD
    entry either channel would need, since the permission is tied to
    the user account running sudo, not which channel invoked it.
    Checks every "latest handshake" line found (pi2 runs two WG
    interfaces, wg0 and wg1) -- healthy if ANY is fresh."""
    out = _shell_run(node, "sudo -n wg show")
    if out is None or "Operation not permitted" in out or "password is required" in out:
        return False
    handshakes = re.findall(r"latest handshake:\s*(.+)", out)
    if not handshakes:
        return False   # interface up but never completed a handshake at all
    return any(_handshake_is_fresh(h) for h in handshakes)


def heal_wg(node):
    # restart_wg is one of pi_intent_listener.py's original fixed
    # intents (predates last night's work) -- confirmed live 2026-08-15
    # it already has a working sudoers entry, no new one needed for
    # this specific heal action (only the check function above needed
    # a new "wg show" sudoers line).
    return send_intent(node, "restart_wg")


def heal_listener(node):
    # Both Pis' shell-launch-listener.service are systemd-managed now
    # (2026-08-14, Restart=on-failure + linger enabled). Routed through
    # pi_intent's exec:, which always runs as ROOT (sudo -n bash -c,
    # no -u martin) -- confirmed live that root's environment has no
    # DBUS_SESSION_BUS_ADDRESS/XDG_RUNTIME_DIR, so a bare `systemctl
    # --user` as root can't find martin's lingering user-systemd
    # session at all (different failure than the earlier sudoers
    # block, which is now fixed via a NOPASSWD entry on each Pi).
    # `runuser -u martin --` properly switches into martin's session
    # (root can always do this natively, no further sudoers entry
    # needed) with the right runtime dir for the --user bus.
    cmd = ("runuser -u martin -- env XDG_RUNTIME_DIR=/run/user/1000 "
           "systemctl --user restart shell-launch-listener.service")
    return send_intent(node, f"exec:{cmd}")


# Local Mint-side process liveness -- structurally different from the
# node jobs above (plain pgrep, not a remote shell_launch/pi_intent
# request), so it doesn't fit NODES' remote-host fields. Reuses run_job/
# narrate/state as-is by tagging these under a synthetic "mint" node
# label rather than adding a second, parallel loop -- these are exactly
# the writer processes that "check S"/"check regime" have been silently
# assuming are alive all session; this makes that assumption checked
# instead of assumed. BENIGN_READ, no heal function, same as disk_space
# -- blindly restarting a process that died for an unknown real reason
# (bug, port conflict) is not a safe default action, always escalate.
LOCAL_PROCESSES = {
    "market_layer0": "run_test.py",
    "mint_cpu_layer0": "run_local.py",
    "netlat_layer0": "run_netlat.py",
    "layer1": "run_layer1.py",
    "layer2": "run_layer2.py",
    "regime_classifier": "regime_classifier.py",
    "consensus_explainer": "consensus_explainer.py",
    "anomaly_explainer": "anomaly_explainer.py",
    "log_trimmer": "log_trimmer.py",
    "ec2_probe": "ec2_probe.py",
}


def _pgrep_alive(pattern):
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def _make_local_process_check(pattern):
    return lambda node: _pgrep_alive(pattern)


LOCAL_JOBS = [
    dict(name=f"{name}_alive", verb="BENIGN_READ",
         check=_make_local_process_check(pattern), heal=None)
    for name, pattern in LOCAL_PROCESSES.items()
]


# Tier 1 observer-drift check ("the stick hasn't moved, but has the
# ruler warped" -- 2026-08-15 conversation): _alive above only proves
# the PROCESS exists, not that it's still doing anything -- a hung
# process reads as healthy forever. This checks output freshness (real
# wall-clock mtime, not the log's own internal elapsed-seconds label,
# which is relative to process start and useless for "is this recent
# right now") against each writer's own known real tick rate, same
# freshness-not-existence principle as truthd.c's HEALTH_STALE_S/
# EC2_STALE_S.
#
# Only the genuinely periodic writers get this -- regime_classifier/
# consensus_explainer/anomaly_explainer/log_trimmer are event-triggered
# or irregular (a long quiet period is correct behaviour, not staleness)
# and would false-positive under a freshness model; they stay covered
# by _alive only. market_layer0 is journal-only (systemd, no log file),
# a different check shape -- left out of this first pass, not folded in
# just to hit a round number.
#
# stale_after_s margins: ec2_probe reuses truthd.c's own EC2_STALE_S
# (15s = 3x its real PROBE_INTERVAL_S=5.0, already validated there).
# The four 0.5s-tick oscillator/telemetry logs use 30s -- a deliberately
# chosen ~60x margin, not independently sampled the way the WG
# handshake threshold was -- picked generous because checks only run
# every CHECK_INTERVAL_S=60s anyway, so anything tighter risks a false
# stale reading from ordinary scheduling jitter between checks.
FRESHNESS_TARGETS = {
    "layer1": dict(path="layer1/layer1.log", stale_after_s=30),
    "layer2": dict(path="layer2/layer2.log", stale_after_s=30),
    "netlat_layer0": dict(path="netlat_layer0/run_netlat.log", stale_after_s=30),
    "mint_cpu_layer0": dict(path="mint_cpu_layer0/run_local.log", stale_after_s=30),
    "ec2_probe": dict(path="channels/ec2_probe.log", stale_after_s=15),
}


def _fresh(path, stale_after_s):
    full_path = os.path.join(REPO, path)
    try:
        mtime = os.path.getmtime(full_path)
    except FileNotFoundError:
        return False
    return (time.time() - mtime) < stale_after_s


def _make_freshness_check(path, stale_after_s):
    return lambda node: _fresh(path, stale_after_s)


FRESHNESS_JOBS = [
    dict(name=f"{name}_fresh", verb="BENIGN_READ",
         check=_make_freshness_check(cfg["path"], cfg["stale_after_s"]), heal=None)
    for name, cfg in FRESHNESS_TARGETS.items()
]


# Groq quota visibility -- 2026-08-15, tied directly to tonight's real
# recurring friction: consensus_explainer/anomaly_explainer/mesh_healer
# itself have all silently hit QuotaExceeded more than once, discovered
# only after the fact from a skipped narration line. This surfaces it
# proactively instead. BENIGN_READ, no heal -- there's no automated fix
# for "quota's nearly gone," same reasoning as disk_space.
#
# Honest cost: Groq only exposes quota via response headers on a real
# request, there's no free headers-only endpoint -- so checking quota
# costs a tiny sliver of the quota it's watching. Minimized with
# max_completion_tokens=1 (response body is never read, only headers,
# so a truncated/empty completion is fine) to keep that sliver as small
# as possible.
GROQ_QUOTA_LOW_FRAC = 0.10   # unhealthy if remaining tokens OR requests
                              # drop below this fraction of the limit


def check_groq_quota(node):
    import json
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "model": groq_client.MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 1,
    }).encode()
    req = urllib.request.Request(groq_client.GROQ_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {groq_client.GROQ_API_KEY}",
        "User-Agent": "mesh-healer-quota-check/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            headers = resp.headers
    except urllib.error.HTTPError as e:
        headers = e.headers   # even a 429 response carries the rate-limit headers
    except OSError:
        return False

    try:
        remaining_tokens = int(headers.get("x-ratelimit-remaining-tokens", -1))
        limit_tokens = int(headers.get("x-ratelimit-limit-tokens", -1))
        remaining_requests = int(headers.get("x-ratelimit-remaining-requests", -1))
        limit_requests = int(headers.get("x-ratelimit-limit-requests", -1))
    except (TypeError, ValueError):
        return False
    if limit_tokens <= 0 or limit_requests <= 0:
        return False

    return (remaining_tokens / limit_tokens >= GROQ_QUOTA_LOW_FRAC
            and remaining_requests / limit_requests >= GROQ_QUOTA_LOW_FRAC)


GROQ_QUOTA_JOB = dict(name="groq_quota_healthy", verb="BENIGN_READ",
                       check=check_groq_quota, heal=None)


# Tier 2 observer-drift ("is the ruler bent, not just is the trace
# recent" -- 2026-08-15 conversation): every freshness job above trusts
# Mint's own mtime implicitly. If Mint's system clock silently drifted,
# every one of those checks would agree with each other and all be
# wrong together, since they'd all be measuring against the same bent
# ruler. This checks Mint's clock against an independent reference --
# EC2, reusing its already-established "outside gauge" role (same as
# ec2_probe.py) rather than introducing a new dependency.
#
# Deliberately NOT over SSH -- measured live 2026-08-15: a fresh SSH
# handshake per check has 2.2-2.9s RTT with ~300ms jitter (connection
# setup dominates, not real network latency), too noisy to trust for a
# clock check, and the exact problem shell_launch was built to replace
# SSH for in the first place -- mixing it into the substrate's own
# measurement path was correctly rejected. ec2_intent's existing signed
# UDP "uptime" intent gives ~162ms, low-jitter RTT instead, and its
# response already carries a real wall-clock HH:MM:SS -- no new
# listener code needed.
#
# Validated against 5 real live samples: offset was 0-1s every time
# (the 1s spread is uptime's own HH:MM:SS rounding, the measurement's
# resolution floor, not real drift) against a consistent ~162ms RTT.
# CLOCK_DRIFT_UNHEALTHY_S=10 gives real margin above that observed
# floor while still catching genuine, meaningful drift (minutes), same
# validated-with-real-margin standard as the WG handshake threshold.
CLOCK_DRIFT_UNHEALTHY_S = 10


def check_clock_drift(node):
    key = eic.load_key()
    nonce = secrets.randbits(64)
    pkt = eic.pack("uptime", time.time(), nonce, key)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    try:
        t0 = time.time()
        sock.sendto(pkt, ("10.8.0.1", 58551))
        while True:
            data, _ = sock.recvfrom(512)
            t2 = time.time()
            status, detail, resp_nonce = eic.unpack_response(data, key)
            if resp_nonce != nonce or status == pic.ACCEPTED:
                continue
            break
    except OSError:
        return False
    finally:
        sock.close()

    m = re.match(r"(\d+):(\d+):(\d+)", detail)
    if not m:
        return False
    hh, mm, ss = map(int, m.groups())
    now_utc = time.gmtime((t0 + t2) / 2)
    remote_s = hh * 3600 + mm * 60 + ss
    local_s = now_utc.tm_hour * 3600 + now_utc.tm_min * 60 + now_utc.tm_sec
    return abs(remote_s - local_s) <= CLOCK_DRIFT_UNHEALTHY_S


CLOCK_DRIFT_JOB = dict(name="mint_clock_drift", verb="BENIGN_READ",
                        check=check_clock_drift, heal=None)

JOBS = [
    dict(name="listener_responsive", verb="LOCAL_DESTRUCTIVE",
         check=check_listener, heal=heal_listener),
    dict(name="disk_space", verb="BENIGN_READ",
         check=check_disk_space, heal=None),
    dict(name="wg_handshake_fresh", verb="LOCAL_HEAL",
         check=check_wg_handshake, heal=heal_wg),
]


def narrate(node, job_name, event, state_hash=None):
    prompt = f"""A deterministic self-heal script for a WireGuard mesh node called
"{node}" just observed this sequence of real, already-completed events
for its "{job_name}" check (the script decided and acted on its own
using fixed rules -- you are only being asked to summarize what
happened, not to decide anything):

{event}

Summarize in 1-2 plain-English sentences what happened and whether the
node is healthy now for this check. Do not suggest further actions."""
    try:
        return query_llm(prompt, state_hash=state_hash)
    except QuotaExceeded as e:
        return f"(narration skipped -- Groq quota, retry in {e.retry_after_s:.0f}s)"
    except Exception as e:
        return f"(narration failed: {e})"


def new_state():
    return dict(last_heal_attempt=0.0, consecutive_failures=0, escalated=False, notified=False)


def run_job(node, job, state):
    tag = f"{node}/{job['name']}"

    healthy = job["check"](node)
    if healthy:
        if state["consecutive_failures"] > 0:
            print(f"[mesh_healer] {tag}: healthy again "
                  f"(after {state['consecutive_failures']} failed cycle(s))", flush=True)
        state["consecutive_failures"] = 0
        state["notified"] = False
        return

    if job["heal"] is None:
        state["consecutive_failures"] += 1
        if not state["notified"]:
            state["notified"] = True
            event = f"health check: FAILED\nno automated heal available for this job"
            # Bucketed by (node, job) only -- this event's text is
            # identical every time for a given (node, job), so a repeat
            # notification within the cache TTL is a genuine duplicate,
            # not a distinct situation needing a fresh explanation.
            state_hash = f"nofix:{node}:{job['name']}"
            print(f"[mesh_healer] {tag}: {event.replace(chr(10), ' | ')}", flush=True)
            print(f"[mesh_healer] {tag}: {narrate(node, job['name'], event, state_hash)}",
                  flush=True)
        if state["consecutive_failures"] >= HEAL_ATTEMPTS_BEFORE_ESCALATE:
            state["escalated"] = True
            print(f"[mesh_healer] {tag}: ESCALATED to human after "
                  f"{state['consecutive_failures']} failed checks -- no automated "
                  f"fix exists for this job.", flush=True)
        return

    now = time.time()
    if now - state["last_heal_attempt"] < HEAL_COOLDOWN_S:
        print(f"[mesh_healer] {tag}: unhealthy, within cooldown, skipping heal attempt",
              flush=True)
        return

    state["last_heal_attempt"] = now
    status, detail = job["heal"](node)
    time.sleep(HEAL_VERIFY_DELAY_S)
    verified = job["check"](node)

    event = (f"health check: FAILED\nheal action sent\n"
             f"gate response: {status} ({detail})\n"
             f"post-heal health check: {'PASSED' if verified else 'STILL FAILED'}")
    # Bucketed by outcome CATEGORY (status + pass/fail), not the raw
    # detail text (real command stdout/gate messages vary slightly even
    # for the same underlying situation, which would defeat the cache
    # if included in the hash).
    state_hash = f"heal:{node}:{job['name']}:{status}:{'passed' if verified else 'failed'}"
    print(f"[mesh_healer] {tag}: {event.replace(chr(10), ' | ')}", flush=True)
    print(f"[mesh_healer] {tag}: {narrate(node, job['name'], event, state_hash)}", flush=True)

    if verified:
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] += 1
        if state["consecutive_failures"] >= HEAL_ATTEMPTS_BEFORE_ESCALATE:
            state["escalated"] = True
            print(f"[mesh_healer] {tag}: ESCALATED to human after "
                  f"{state['consecutive_failures']} failed heal attempts -- "
                  f"not retrying automatically.", flush=True)


MINT_JOBS = LOCAL_JOBS + FRESHNESS_JOBS + [GROQ_QUOTA_JOB, CLOCK_DRIFT_JOB]


def main():
    print(f"[mesh_healer] watching {list(NODES)}, jobs={[j['name'] for j in JOBS]}, "
          f"local(mint) jobs={[j['name'] for j in MINT_JOBS]}, "
          f"check every {CHECK_INTERVAL_S}s", flush=True)
    state = {(node, job["name"]): new_state() for node in NODES for job in JOBS}
    state.update({("mint", job["name"]): new_state() for job in MINT_JOBS})

    while True:
        for node in NODES:
            for job in JOBS:
                st = state[(node, job["name"])]
                if st["escalated"]:
                    continue
                run_job(node, job, st)
        for job in MINT_JOBS:
            st = state[("mint", job["name"])]
            if st["escalated"]:
                continue
            run_job("mint", job, st)
        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()
