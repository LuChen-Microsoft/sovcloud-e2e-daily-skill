#!/usr/bin/env python3
"""
Aggregate TRX results from N runs of a SovClouds playlist into a stability report.

Emits two files in --outdir:
  report.md          - plain structured report (raw numbers)
  report_styled.md   - emoji-styled best-of-N stability report (matches the team's report format)

The styled report's deterministic sections (run summary, scenario table, category tally,
stability) are generated here. The 'Known issues / Path to green / Incident filing' narrative
is left as a skeleton for the agent to enrich from the captured per-test StdOut.

Usage:
  python parse_report.py --trx C:\\out\\cs3\\trx --tests C:\\out\\cs3\\tests.txt \
         --label "CS-3 (Basic Messaging)" --cloud Delos --config AppDelos.config \
         --runs 3 --outdir C:\\out\\cs3 --report-out C:\\out --date 20260706

  # --outdir     : per-playlist working folder (holds trx\\ + run_meta.json).
  # --report-out : shared folder where BOTH playlists' reports land (optional; defaults
  #                to --outdir). Colliding files are key-suffixed there (report_cs3.md,
  #                metrics_cs3.json); the styled '<slug>_<date>.md' is already unique.
  # --date       : fix the yyyyMMdd stamp once per daily run so CS-3 and CS-5 share one
  #                dated folder even if they finish on different days.
"""
import argparse, glob, os, re, json, datetime, sys, xml.etree.ElementTree as ET
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

NS = "{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}"


def strip_params(name):
    return name.split("(")[0].strip()


def parse_trx(path):
    root = ET.parse(path).getroot()
    idclass = {}
    for ut in root.iter(f"{NS}UnitTest"):
        tm = ut.find(f"{NS}TestMethod")
        if tm is not None:
            cn = (tm.get("className") or "").split(",")[0].strip()
            idclass[ut.get("id")] = (cn, tm.get("name"))
    out = []
    for r in root.iter(f"{NS}UnitTestResult"):
        tid = r.get("testId"); tname = r.get("testName"); outcome = r.get("outcome")
        cn = idclass.get(tid, ("", tname))
        method = strip_params(cn[1] or tname or "")
        fqn = f"{cn[0]}.{method}" if cn[0] else method
        msg = ""
        oe = r.find(f"{NS}Output")
        if oe is not None:
            ei = oe.find(f"{NS}ErrorInfo")
            if ei is not None:
                m = ei.find(f"{NS}Message"); s = ei.find(f"{NS}StackTrace")
                msg = (m.text or "" if m is not None else "")
                if s is not None and s.text:
                    msg += "\n" + s.text
            so = oe.find(f"{NS}StdOut")
            if so is not None and so.text:
                msg += "\n" + so.text
        out.append((fqn, outcome, msg))
    return out


def classify(msg):
    m = msg or ""; ml = m.lower()
    if "aadsts50020" in ml:
        return "C2-Aad50020"
    if re.search(r"aadsts\d+", ml):
        return "C2-AadOther"
    if "not connected within" in ml or re.search(r"trouter.{0,40}(timeout|not connected)", ml):
        return "C6-Trouter"
    # Scheduling Service (meeting creation) authorization failures. These surface as a 401 /
    # "Unauthorized" from the Scheduling Service (POST .../teams/v1/meetings) and were previously
    # mislabeled as MsgAPI401 -- but the owner is the Scheduling Service, NOT messaging tokens, so
    # classify them explicitly here (before the MsgAPI401 rule below).
    if ("schedulingexception" in ml or "scheduler.communications" in ml
            or "/teams/v1/meetings" in ml):
        return "C7-Scheduling"
    # Genuine MsgAPI token 401: requires the token-metadata signature ("kid"/"metadata"). Do NOT
    # match a bare "unauthorized" -- any 401 (e.g. the Scheduling Service failure above) contains
    # that word, which is what caused the historical MsgAPI401 mislabeling.
    if "<<< 401" in m and ("kid" in ml or "metadata" in ml):
        return "C5-MsgAPI401"
    if "asyncmediaexception" in ml or re.search(r"\bams\b[^\n]{0,60}(deployment|failed|error)", ml):
        return "C3-AMS"
    if "exceeded timeout value" in ml:
        return "Timeout"
    return "Other"


# Representative correlation/telemetry identifiers from a failing test's captured StdOut.
_MSCV_RE = re.compile(r"MS-CV:\s*([A-Za-z0-9+/]+(?:\.\d+)*)")
_CORRV_RE = re.compile(r'correlation_vector"?\s*[:=]\s*"?([A-Za-z0-9+/]+(?:\.\d+)*)')
_CTXCV_RE = re.compile(r"\bcv=([A-Za-z0-9+/]+(?:\.\d+)*)")
_RESP_RE = re.compile(r"<<<\s*(\d{3})")
_DATE_RE = re.compile(r"Date:\s*([A-Za-z]{3},\s*\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2})\s*GMT")


def _any_cv(text):
    m = _MSCV_RE.search(text) or _CORRV_RE.search(text) or _CTXCV_RE.search(text)
    return m.group(1) if m else ""


def _utc_from_date(s):
    """'Thu, 25 Jun 2026 19:14:10' -> '2026-06-25 19:14:10Z' (RFC1123 GMT is UTC)."""
    try:
        return datetime.datetime.strptime(s.strip(), "%a, %d %b %Y %H:%M:%S").strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        return ""


def extract_telemetry(msg):
    """Return {cv, utc} for the FAILING request: the MS-CV / correlation vector and the
    UTC time (from the response Date header) nearest the failure. Prefers the last
    non-2xx (4xx/5xx) response; falls back to the last correlation vector / Date seen
    (e.g. for pure NUnit timeouts that never got a service error response)."""
    m = msg or ""
    lines = m.splitlines()
    cv = utc = ""
    fail_idx = -1
    for i, ln in enumerate(lines):
        rm = _RESP_RE.search(ln)
        if rm and rm.group(1)[0] in "45":
            fail_idx = i
    if fail_idx >= 0:
        window = "\n".join(lines[max(0, fail_idx - 1):fail_idx + 4])
        cv = _any_cv(window)
        dm = _DATE_RE.search(window)
        if dm:
            utc = _utc_from_date(dm.group(1))
    if not cv:
        cvs = _MSCV_RE.findall(m) or _CORRV_RE.findall(m) or _CTXCV_RE.findall(m)
        cv = cvs[-1] if cvs else ""
    if not utc:
        dates = _DATE_RE.findall(m)
        if dates:
            utc = _utc_from_date(dates[-1])
    return {"cv": cv, "utc": utc}


def tele_str(t):
    if not t or not t.get("cv"):
        return "(no CV captured)"
    return f"CV {t['cv']}" + (f" @ {t['utc']}" if t.get("utc") else "")


def scenario_group(fqn):
    f = fqn
    is_notif = ".Notifications." in f
    is_tac = ".TeamsAndChannels." in f
    cls = f.rsplit(".", 1)[0].rsplit(".", 1)[-1]
    if "SpaceAdminSettings" in f:
        return "TaC: Admin Settings"
    if is_tac and is_notif:
        return "TaC: Notifications"
    if is_tac:
        return "TaC: Messaging"
    if ".Media." in f:
        return "Media (Picture Sharing)"
    if "PushTest" in f:
        return "Notifications: Push"
    if is_notif:
        return "Notifications (Chat)"
    if "Membership" in cls or "MemberUpdate" in cls:
        return "Chat Membership"
    if "Draft" in cls or "Sync" in cls:
        return "Sync & Drafts"
    return "Chat Messaging"


LABELS = {
    "C2-Aad50020": "AAD account provisioning AADSTS50020 (guest/account not in tenant)",
    "C2-AadOther": "AAD token failure (other AADSTS)",
    "C6-Trouter": "Trouter not connected within timeout",
    "C5-MsgAPI401": "MsgAPI 401 (token kid not in metadata)",
    "C7-Scheduling": "Scheduling Service meeting creation unauthorized (not a messaging-token issue)",
    "C3-AMS": "AMS old deployment",
    "Timeout": "60s/90s NUnit timeout (uncategorized)",
    "Other": "Other / uncategorized signature",
}


def grp_emoji(rate):
    if rate >= 100: return "\u2705"   # green check
    if rate >= 60:  return "\U0001F7E2"  # green circle
    if rate >= 30:  return "\U0001F7E1"  # yellow circle
    return "\U0001F534"  # red circle


def _fmt_dt(iso):
    """Format an ISO-8601 string (with offset) as 'yyyy-MM-dd HH:mm:ss (UTC±hh:mm)'."""
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except Exception:
        return iso
    off = dt.strftime("%z")
    off = (off[:3] + ":" + off[3:]) if off else ""
    return dt.strftime("%Y-%m-%d %H:%M:%S") + (f" (UTC{off})" if off else "")


def timing_lines(outdir):
    """Return (generated_line, ran_line) describing report-generation and test start/end times."""
    now = datetime.datetime.now().astimezone()
    noff = now.strftime("%z"); noff = (noff[:3] + ":" + noff[3:]) if noff else ""
    tzname = now.tzname() or ""
    generated = f"Report generated: {now.strftime('%Y-%m-%d %H:%M:%S')} {tzname} (UTC{noff})".rstrip()
    ran = None
    meta_path = os.path.join(outdir, "run_meta.json")
    if os.path.exists(meta_path):
        try:
            m = json.load(open(meta_path, encoding="utf-8"))
            tz = m.get("timezone", "")
            dur = m.get("durationMinutes")
            dur_s = f", duration {dur} min" if dur is not None else ""
            ran = (f"Tests ran: {_fmt_dt(m.get('startTime',''))} \u2192 {_fmt_dt(m.get('endTime',''))}"
                   + (f" [{tz}]" if tz else "") + dur_s)
        except Exception:
            ran = None
    return generated, ran


def playlist_key(slug):
    """Folder/key for a playlist, e.g. 'CS-3' -> 'cs3', 'CS-5' -> 'cs5'."""
    return re.sub(r"[^a-z0-9]", "", slug.lower()) or "report"


def write_metrics(path, metrics):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(metrics, open(path, "w", encoding="utf-8"), indent=2)
    except Exception:
        pass


def write_text(path, text):
    """Write a text file, creating parent dirs. Best-effort (never raises)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(text)
    except Exception:
        pass


def find_prev_metrics(reports_root, key, cur_date, explicit=None):
    """Find the most recent previous run's metrics for this playlist.

    If 'explicit' is given it is used directly (a metrics.json path or its folder).
    Otherwise scans <reports_root>\\*\\<key>\\metrics.json and picks the latest whose
    date is strictly earlier than cur_date (yyyymmdd string)."""
    candidates = []
    if explicit:
        p = explicit
        if os.path.isdir(p):
            p = os.path.join(p, "metrics.json")
        if os.path.exists(p):
            candidates.append(p)
    else:
        for p in glob.glob(os.path.join(reports_root, "*", key, "metrics.json")):
            try:
                d = json.load(open(p, encoding="utf-8")).get("date", "")
            except Exception:
                continue
            if d and d < cur_date:
                candidates.append((d, p))
        candidates = [p for _, p in sorted(candidates)]
    for p in reversed(candidates):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
    return None


def _delta(prev, cur, pts=False, lower_better=False):
    d = cur - prev
    if abs(d) < (0.05 if pts else 0.5):
        return "no change"
    arrow = "\U0001F53C" if ((d > 0) != lower_better) else "\U0001F53D"
    sign = "+" if d > 0 else ""
    return f"{arrow} {sign}{d:.1f}{' pts' if pts else ''}"


def _short_fqn(fqn):
    """Class.Method tail of a fully-qualified test name, for scannable lists."""
    parts = fqn.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else fqn


def churn_lines(cur, prev):
    """Per-test fixed/new diff vs the previous run.

    Surfaces tests that were failing last run and now pass ("fixed") and tests that
    were passing last run and now fail ("new"), with their failure categories. This
    makes real progress/regressions visible even when the headline pass rate and the
    per-category counts are flat (e.g. 3 timeouts fixed but 3 different tests newly
    time out -> Timeout 6->6 looks unchanged while the bucket fully churned).
    Requires the previous run's metrics to carry 'fail_tests'; older metrics predate
    it, so we emit a one-line notice and let it populate from the next run onward.
    """
    cf, pf = cur.get("fail_tests"), prev.get("fail_tests")
    if not isinstance(cf, dict) or not isinstance(pf, dict):
        return ["- **\u2705\U0001F195 Fixed / new (per test):** _unavailable \u2014 the previous run predates "
                "per-test failure tracking; this populates automatically from the next run onward._"]
    pdate = prev.get("date", "?")
    cur_fail, prev_fail = set(cf), set(pf)
    cur_stale = set(cur.get("stale_tests", []))
    cur_skip = set(cur.get("skipped_tests", []))
    left = prev_fail - cur_fail
    fixed = sorted(f for f in left if f not in cur_stale and f not in cur_skip)
    dropped = sorted(f for f in left if f in cur_stale or f in cur_skip)
    newly = sorted(cur_fail - prev_fail)
    still = sorted(cur_fail & prev_fail)

    def bullets(fqns, catmap, cap=20):
        out = []
        for f in fqns[:cap]:
            cats = ",".join(catmap.get(f) or []) or "?"
            out.append(f"  - `{_short_fqn(f)}` ({cats})")
        if len(fqns) > cap:
            out.append(f"  - \u2026and {len(fqns) - cap} more")
        return out

    L = []
    if fixed:
        L.append(f"- **\u2705 Fixed since {pdate} ({len(fixed)}):** were failing, now passing")
        L += bullets(fixed, pf)
    else:
        L.append(f"- **\u2705 Fixed since {pdate} (0):** none")
    if newly:
        L.append(f"- **\U0001F195 New failures vs {pdate} ({len(newly)}):** were passing, now failing")
        L += bullets(newly, cf)
    else:
        L.append(f"- **\U0001F195 New failures vs {pdate} (0):** none")
    L.append(f"- **\u267B\ufe0f Still failing ({len(still)}):** unchanged from {pdate}")
    if dropped:
        names = ", ".join(f"`{_short_fqn(f)}`" for f in dropped[:10]) + (f" +{len(dropped) - 10} more" if len(dropped) > 10 else "")
        L.append(f"- **\u26AA No longer executed ({len(dropped)}):** previously-failing, now stale/skipped \u2014 {names}")
    net = len(newly) - len(fixed)
    net_str = f"{'+' if net > 0 else ''}{net}"
    masked = " \u2014 \u26A0\ufe0f churn hidden by a flat pass rate/count" if (fixed and newly and abs(net) <= 1) else ""
    L.append(f"- **Net failing-test change:** {len(fixed)} fixed / {len(newly)} new = **{net_str}**{masked}")
    return L


def comparison_block(cur, prev):
    """Markdown lines comparing the current run to the previous run (or [] if none)."""
    if not prev:
        return []
    pd = prev.get("date", "?")
    L = [f"## \U0001F4C8 Comparison vs previous run ({pd})", "",
         f"- **Pass rate (best-of-N):** {prev.get('pass_rate',0):.1f}% \u2192 "
         f"**{cur['pass_rate']:.1f}%** ({_delta(prev.get('pass_rate',0), cur['pass_rate'], pts=True)})",
         f"- **Deterministic:** {prev.get('deterministic_pct',0):.1f}% \u2192 "
         f"**{cur['deterministic_pct']:.1f}%** ({_delta(prev.get('deterministic_pct',0), cur['deterministic_pct'], pts=True)})",
         f"- **Executed / run:** {prev.get('executed',0)} \u2192 {cur['executed']} "
         f"({_delta(prev.get('executed',0), cur['executed'])})"
         + (f"  \u26A0\ufe0f executed count changed \u2014 check for config/skip differences" if prev.get('executed') != cur['executed'] else "")]
    # Per-category failure deltas (de-duplicated tests affected per bucket).
    pc, cc = prev.get("cat_counts", {}), cur["cat_counts"]
    changes = []
    for cat in sorted(set(pc) | set(cc), key=lambda k: -max(pc.get(k, 0), cc.get(k, 0))):
        p, c = pc.get(cat, 0), cc.get(cat, 0)
        if p == c:
            continue
        tag = " (new)" if p == 0 else (" (cleared)" if c == 0 else "")
        mark = "\U0001F53D" if c > p else "\U0001F53C"  # more failures = regression
        changes.append(f"{LABELS.get(cat, cat)} {p}\u2192{c} {mark}{tag}")
    if changes:
        L.append(f"- **Failure categories:** " + "; ".join(changes))
    elif prev.get("cat_counts") is not None:
        L.append("- **Failure categories:** no net count change "
                 "(see per-test fixed/new below \u2014 buckets may still have churned)")
    # Per-test churn: which specific tests were fixed vs newly broke. This is the part
    # that stays meaningful when the pass rate and category counts are flat.
    L += churn_lines(cur, prev)
    L += ["", "[AGENT: add ONE short sentence summarizing the trend \u2014 net better/worse, the "
          "biggest mover, how many tests were FIXED vs NEWLY failing (call it out explicitly even "
          "when the pass rate is flat, since equal counts can hide a fully churned bucket), and "
          "whether any executed-count change is a config gap vs a real change]", ""]
    return L


def default_reports_root():
    """Persistent reports root for cross-run comparison. Defaults to a per-user
    OneDrive folder when available so the metrics.json history (and the
    'vs previous run' comparison) follows the user across machines; falls back
    to Q:\\delos-test-reports when OneDrive is not present."""
    od = os.environ.get("OneDrive") or os.environ.get("OneDriveCommercial")
    return os.path.join(od, "delos-test-reports") if od else r"Q:\delos-test-reports"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trx", required=True)
    ap.add_argument("--tests", required=True, help="file with the playlist's wanted FQNs")
    ap.add_argument("--label", required=True, help='e.g. "CS-3 (Basic Messaging)"')
    ap.add_argument("--cloud", default="Delos")
    ap.add_argument("--config", default="AppDelos.config")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--reports-root", default=default_reports_root(),
                    help="Persistent reports root used to discover the previous run for comparison. "
                         "Defaults to %%OneDrive%%\\delos-test-reports when OneDrive is available (so the "
                         "history syncs across machines), else Q:\\delos-test-reports.")
    ap.add_argument("--prev", default=None,
                    help="Optional explicit previous metrics.json (or its folder); overrides auto-discovery.")
    ap.add_argument("--report-out", default=None,
                    help="Shared folder for the deliverable reports (styled .md, plain report, metrics). "
                         "When set, CS-3 and CS-5 can share ONE folder: the styled report keeps its unique "
                         "'<slug>_<date>.md' name, while the plain report and metrics get a playlist-key "
                         "suffix (report_cs3.md / metrics_cs3.json) so the two playlists never overwrite each "
                         "other. Run artifacts (trx, logs) still live under --outdir. Defaults to --outdir.")
    ap.add_argument("--date", default=None,
                    help="yyyyMMdd stamp for the report filename and the reports-root folder. Set this ONCE "
                         "at the start of a daily run so CS-3 and CS-5 land in the SAME dated folder even if "
                         "the runs finish on different calendar days. Defaults to the run_meta.json start date.")
    a = ap.parse_args()
    runs = list(range(1, a.runs + 1))

    want = set(l.strip() for l in open(a.tests, encoding="utf-8") if l.strip())
    data = defaultdict(dict)
    tele = {}        # fqn -> telemetry dict from a representative failing run (last run wins)
    all_fqns = set()
    for run in runs:
        agg = {}
        for fp in sorted(glob.glob(os.path.join(a.trx, f"run{run}_chunk*.trx"))):
            for fqn, outcome, msg in parse_trx(fp):
                rank = {"Failed": 3, "Passed": 2, "NotExecuted": 1, None: 0}
                prev = agg.get(fqn)
                if prev is None or rank.get(outcome, 0) > rank.get(prev[0], 0):
                    agg[fqn] = (outcome, msg)
        for fqn, (outcome, msg) in agg.items():
            all_fqns.add(fqn)
            data[fqn][run] = (outcome, classify(msg) if outcome == "Failed" else None)
            if outcome == "Failed":
                t = extract_telemetry(msg)
                if t.get("cv") or t.get("utc") or fqn not in tele:
                    tele[fqn] = t

    run_pass = {r: 0 for r in runs}; run_fail = {r: 0 for r in runs}; run_skip = {r: 0 for r in runs}
    for fqn in all_fqns:
        for r in runs:
            oc = data[fqn].get(r, (None, None))[0]
            if oc == "Passed": run_pass[r] += 1
            elif oc == "Failed": run_fail[r] += 1
            elif oc == "NotExecuted": run_skip[r] += 1

    ap_ = mp = mf = af = 0
    skipped_tests = []
    for fqn in all_fqns:
        execs = [o for o in (data[fqn].get(r, (None, None))[0] for r in runs) if o in ("Passed", "Failed")]
        if not execs:
            skipped_tests.append(fqn); continue
        p = execs.count("Passed"); n = len(execs)
        if p == n: ap_ += 1
        elif p == 0: af += 1
        elif p * 2 >= n: mp += 1
        else: mf += 1
    executed_total = len(all_fqns) - len(skipped_tests)
    best = sum(1 for fqn in all_fqns if any(data[fqn].get(r, (None, None))[0] == "Passed" for r in runs))
    det = ap_ + af
    not_in_build = sorted(want - all_fqns)

    grp_tests = defaultdict(set); grp_pass = defaultdict(int); grp_skip = defaultdict(int)
    grp_cats = defaultdict(lambda: defaultdict(int))
    for fqn in all_fqns:
        g = scenario_group(fqn); grp_tests[g].add(fqn)
        outs = [data[fqn].get(r, (None, None)) for r in runs]
        execs = [o for o in outs if o[0] in ("Passed", "Failed")]
        if not execs: grp_skip[g] += 1; continue
        if any(o[0] == "Passed" for o in execs): grp_pass[g] += 1
        for o in outs:
            if o[0] == "Failed" and o[1]: grp_cats[g][o[1]] += 1

    cat_tests = defaultdict(set)
    for fqn in all_fqns:
        for r in runs:
            o = data[fqn].get(r, (None, None))
            if o[0] == "Failed" and o[1]: cat_tests[o[1]].add(fqn)

    # per-failing-test category set (for the appendix), and per-category representative CV samples
    fqn_cats = defaultdict(set)
    for cat, fqns in cat_tests.items():
        for fqn in fqns:
            fqn_cats[fqn].add(cat)
    failed_fqns = sorted(fqn_cats)

    def cat_samples(cat, n=2):
        out = []
        for fqn in sorted(cat_tests.get(cat, ())):
            t = tele.get(fqn)
            if t and (t.get("cv") or t.get("utc")):
                out.append(tele_str(t))
            if len(out) >= n:
                break
        return out

    pr = (best / executed_total * 100) if executed_total else 0
    dpct = (det / executed_total * 100) if executed_total else 0
    gen_line, ran_line = timing_lines(a.outdir)

    # Slug + run date drive the styled filename, the persistent reports-root location, and
    # previous-run discovery. Prefer an explicit --date (set once per daily run so CS-3 and CS-5
    # share ONE dated folder); otherwise fall back to the run_meta.json start date, then today.
    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", a.label.split("(")[0].strip()).strip("-") or "report"
    key = playlist_key(slug)
    date_str = None
    if a.date and re.fullmatch(r"\d{8}", a.date.strip()):
        date_str = a.date.strip()
    if date_str is None:
        date_str = datetime.datetime.now().strftime("%Y%m%d")
        meta_path = os.path.join(a.outdir, "run_meta.json")
        if os.path.exists(meta_path):
            try:
                st = json.load(open(meta_path, encoding="utf-8")).get("startTime", "")
                if st:
                    date_str = datetime.datetime.fromisoformat(st).strftime("%Y%m%d")
            except Exception:
                pass

    # Deliverable reports go to --report-out when given (shared folder for both playlists),
    # else to --outdir (legacy single-playlist behavior). Colliding files are key-suffixed
    # only in the shared case so existing single-folder callers are unaffected.
    report_out = a.report_out or a.outdir
    shared = bool(a.report_out)
    try:
        os.makedirs(report_out, exist_ok=True)
    except Exception:
        pass

    metrics = {
        "date": date_str, "label": a.label, "slug": slug, "cloud": a.cloud,
        "config": a.config, "runs": a.runs, "executed": executed_total, "best": best,
        "pass_rate": round(pr, 1), "deterministic_pct": round(dpct, 1),
        "always_pass": ap_, "mostly_pass": mp, "mostly_fail": mf, "always_fail": af,
        "skipped": len(skipped_tests), "stale": len(not_in_build),
        "run_pass": {str(r): run_pass[r] for r in runs}, "run_fail": {str(r): run_fail[r] for r in runs},
        "cat_counts": {cat: len(t) for cat, t in cat_tests.items()},
        # Per-test failure identity so the NEXT run can diff which tests were fixed
        # (were failing, now passing) vs newly failing - surfacing churn that flat
        # category counts / pass rates otherwise hide. stale/skipped lists let the
        # diff distinguish a genuine fix from a test that simply stopped executing.
        "fail_tests": {fqn: sorted(cats) for fqn, cats in sorted(fqn_cats.items())},
        "stale_tests": sorted(not_in_build),
        "skipped_tests": sorted(skipped_tests),
        "group_rates": {g: round((grp_pass[g] / (len(grp_tests[g]) - grp_skip[g]) * 100)
                                  if (len(grp_tests[g]) - grp_skip[g]) else 0, 1) for g in grp_tests},
    }
    prev = find_prev_metrics(a.reports_root, key, date_str, explicit=a.prev)
    comp_lines = comparison_block(metrics, prev)
    # Recurring (already failing last run) vs new (started failing this run) per Known-issues
    # bucket, so the category breakdown separates long-standing issues from regressions. Uses the
    # previous run's fail_tests (failed >=1 run, category-agnostic — matches the churn 'still'/'new'
    # definitions). prev_fail_set is None when there is no comparable prior run (baseline) or the
    # prior metrics predate per-test tracking, in which case the tag is omitted.
    _pf = prev.get("fail_tests") if prev else None
    prev_fail_set = set(_pf) if isinstance(_pf, dict) else None

    def churn_tag(tests):
        if prev_fail_set is None:
            return ""
        recurring = sum(1 for t in tests if t in prev_fail_set)
        return f" \u2014 \u267B\ufe0f {recurring} recurring / \U0001F195 {len(tests) - recurring} new"

    # ---------- plain report ----------
    P = [f"# {a.label} in {a.cloud} - {a.runs}-run stability report\n", "## Run summary",
         gen_line]
    if ran_line:
        P.append(ran_line)
    P += [f"{a.runs} sequential runs of {a.label} playlist ({len(want)} entries); config {a.config}.",
         f"{len(not_in_build)} stale (not in build); {len(skipped_tests)} not-applicable/skipped on {a.cloud}; {executed_total} executed per run."]
    for r in runs:
        P.append(f"- Run {r}: {run_pass[r]} pass / {run_fail[r]} fail" + (f" / {run_skip[r]} skipped" if run_skip[r] else ""))
    P.append(f"- Overall best-of-{a.runs} pass rate: {best}/{executed_total} = {pr:.1f}%.")
    P.append(f"- Stability: {ap_} always-pass | {mp} mostly-pass | {mf} mostly-fail | {af} always-fail - {dpct:.1f}% deterministic.\n")
    if comp_lines:
        P += [l for l in comp_lines if not l.startswith("[AGENT")]
    P.append("## Pass rate by scenario group")
    for g in sorted(grp_tests, key=lambda x: (-len(grp_tests[x]), x)):
        ex = len(grp_tests[g]) - grp_skip[g]; rate = (grp_pass[g] / ex * 100) if ex else 0
        cats = "; ".join(f"{k}={v}" for k, v in sorted(grp_cats[g].items(), key=lambda kv: -kv[1]))
        skip = f" (+{grp_skip[g]} skipped)" if grp_skip[g] else ""
        P.append(f"- {g} - {grp_pass[g]}/{ex} ({rate:.0f}%){skip}" + (f" - failures: {cats}" if cats else ""))
    P.append("\n## Failure categories (tests affected)")
    for cat, tests in sorted(cat_tests.items(), key=lambda kv: -len(kv[1])):
        samples = cat_samples(cat)
        s = f" - e.g. {'; '.join(samples)}" if samples else ""
        P.append(f"- {LABELS.get(cat, cat)} - {len(tests)} tests{s}")
    if failed_fqns:
        P.append("\n## Failure telemetry (per test) - failing-request MS-CV @ UTC time")
        P.append("| test | category | MS-CV (failure request) | UTC time |")
        P.append("|------|----------|-------------------------|----------|")
        for fqn in failed_fqns:
            t = tele.get(fqn, {})
            cats = ",".join(sorted(fqn_cats[fqn]))
            P.append(f"| {fqn} | {cats} | {t.get('cv') or '-'} | {t.get('utc') or '-'} |")
    if skipped_tests:
        P.append("\n## Skipped / not-applicable")
        P += [f"- {t}" for t in sorted(skipped_tests)]
    if not_in_build:
        P.append("\n## Not present in build (stale playlist entries)")
        P += [f"- {t}" for t in not_in_build]
    plain_name = f"report_{key}.md" if shared else "report.md"
    plain_text = "\n".join(P)
    open(os.path.join(report_out, plain_name), "w", encoding="utf-8").write(plain_text)

    # ---------- styled report ----------
    # Filename: <playlist-slug>_<yyyyMMdd>.md (e.g. CS-3_20260625.md); slug + date computed above.
    styled_name = f"{slug}_{date_str}.md"
    # Emitted as GitHub-flavored Markdown: '##' headers, '- ' bullets, and blank
    # lines between blocks so it renders correctly in any Markdown viewer.
    S = [f"# \U0001F916 Copilot-generated report \u2014 {a.label} in {a.cloud} \u2014 {a.runs}-run stability", "",
         "## \U0001F9ED Run summary", "",
         f"\U0001F552 *{gen_line}" + (f"; {ran_line}" if ran_line else "") + "*", "",
         f"{a.runs} sequential runs of the SovClouds {a.label} playlist against {a.cloud}; config: `{a.config}`.", "",
         f"Playlist has {len(want)} entries \u2192 {len(not_in_build)} stale (not in build) and {len(skipped_tests)} \u23ED\ufe0f not-applicable on this cloud, leaving **{executed_total} executed** each run.", ""]
    for r in runs:
        S.append(f"- Run {r}: {run_pass[r]} pass / {run_fail[r]} fail")
    S.append(f"- **Overall pass rate (best-of-{a.runs}): {best} / {executed_total} = {pr:.1f}%** \u2014 tests with \u22651 pass; stale + not-applicable excluded from the denominator.")
    S.append(f"- Stability: \u2705 {ap_} always-pass \u00b7 \u26a0\ufe0f {mp} mostly-pass \u00b7 \u26a0\ufe0f {mf} mostly-fail \u00b7 \U0001F534 {af} always-fail \u2014 **{dpct:.1f}% deterministic**.")
    if comp_lines:
        S += [""] + comp_lines
    else:
        S += ["", "_No previous run found under the reports root for this playlist \u2014 this is the baseline; future runs will be compared against it._", ""]
    S += ["", "## \U0001F4CA Pass rate by scenario group", ""]
    for g in sorted(grp_tests, key=lambda x: (-len(grp_tests[x]), x)):
        ex = len(grp_tests[g]) - grp_skip[g]; rate = (grp_pass[g] / ex * 100) if ex else 0
        cats = "; ".join(f"{k}={v}" for k, v in sorted(grp_cats[g].items(), key=lambda kv: -kv[1]))
        skip = f" (+{grp_skip[g]} \u23ED\ufe0f skipped)" if grp_skip[g] else ""
        em = "\u23ED\ufe0f" if ex == 0 else grp_emoji(rate)
        S.append(f"- {em} {g} \u2014 {grp_pass[g]}/{ex} ({rate:.0f}%){skip}" + (f" \u2014 failures: {cats}" if cats else ""))
    S += ["", "## \U0001F6A8 Known issues & status", ""]
    for cat, tests in sorted(cat_tests.items(), key=lambda kv: -len(kv[1])):
        S.append(f"- \U0001F9EF **{LABELS.get(cat, cat)}** \u2014 {len(tests)} tests (best-of-{a.runs}){churn_tag(tests)}. [AGENT: ONE concise sentence \u2014 root cause + owner/status; keep it scannable like a status line]")
    S += ["", "## \U0001F6E3\ufe0f Path to green", ""]
    for cat, tests in sorted(cat_tests.items(), key=lambda kv: -len(kv[1])):
        S.append(f"- Resolving **{LABELS.get(cat, cat)}** would unblock ~{len(tests)} tests.")
    S += ["", "[AGENT: reorder by leverage, merge buckets that share a root cause (e.g. timeouts that are really AADSTS), add owners/links, and call out any config gaps surfaced by the previous-run comparison (e.g. an executed-count change)]"]
    S += ["", "## \U0001F198 Incident filing suggestions", "", "[AGENT: suggest IcM only for genuine product/service failures (the 'Other' bucket); route provisioning issues to the pool owner. Use the CV roots + serving pods below as evidence.]"]
    if failed_fqns:
        S += ["", "## \U0001F50E Failure telemetry (per test) \u2014 failing-request MS-CV @ UTC time", ""]
        S.append("| test | category | MS-CV (failure request) | UTC time |")
        S.append("|------|----------|-------------------------|----------|")
        for fqn in failed_fqns:
            t = tele.get(fqn, {})
            cats = ",".join(sorted(fqn_cats[fqn]))
            short = fqn.split(".")[-2] + "." + fqn.split(".")[-1] if "." in fqn else fqn
            S.append(f"| {short} | {cats} | {t.get('cv') or '-'} | {t.get('utc') or '-'} |")
    if not_in_build:
        S += ["", "## \U0001F9F9 Stale playlist entries (not in build)", ""]
        S += [f"- {t}" for t in not_in_build]
    S.append("")
    styled_text = "\n".join(S)
    open(os.path.join(report_out, styled_name), "w", encoding="utf-8").write(styled_text)

    # Persist machine-readable metrics for next-run comparison: alongside the deliverable
    # reports and into the reports root (<reports_root>\<date>\<key>\metrics.json) so future
    # runs auto-discover it. In the shared-folder case the local copy is key-suffixed.
    metrics_name = f"metrics_{key}.json" if shared else "metrics.json"
    write_metrics(os.path.join(report_out, metrics_name), metrics)
    # metrics.json stays nested under <reports_root>\<date>\<key>\ because find_prev_metrics()
    # globs "<reports_root>\*\<key>\metrics.json" to discover prior runs - this path is not
    # meant to be browsed directly.
    rr_dir = os.path.join(a.reports_root, date_str, key)
    write_metrics(os.path.join(rr_dir, "metrics.json"), metrics)
    # Human-readable reports go directly under <reports_root>\<date>\ (flat, not nested under
    # <key>\) so both playlists' reports are immediately visible side-by-side when browsing the
    # dated folder (e.g. in OneDrive) - no need to open a cs3\/cs5\ subfolder to find them.
    # styled_name is already playlist-unique (<slug>_<date>.md); the plain report is key-suffixed
    # to avoid CS-3/CS-5 collisions, matching the --report-out naming convention.
    date_root = os.path.join(a.reports_root, date_str)
    write_text(os.path.join(date_root, styled_name), styled_text)
    write_text(os.path.join(date_root, f"report_{key}.md"), plain_text)

    print("\n".join(P))
    print(f"\n[written {os.path.join(report_out, plain_name)} and {styled_name}]")


if __name__ == "__main__":
    main()
