"""Verification analysis for the EXSU 620 hypothermia monitor (engineering requirements ER-1 to ER-8, version 2).

Reads YOUR recorded data and produces the figures and the pass/fail table for the verification report.

Inputs
  ../server/monitor.db          dashboard database (every sample line, alarm, mark and phone alert, per case)
  data/calibration_log.csv      reference thermometer readings (ER-1)
  data/manual_checks.csv        observations made by eye (message received on the phone, display shows 0.1 C)
  data/requirements.csv         the engineering requirements and their limits

Dashboard case labels must start with the requirement:
  ER2-...  10 min stable run on the probe
  ER3-w2c-... / ER3-c2w-...  bath step (press Mark at the moment of the move)
  ER4-... / ER5-... / ER6-...  scripted runs (press the matching script button after Start case)
  ER7-...  probe fault (press Mark, then pull the DAT wire)       ER7R-...  scripted out-of-range run

Run:   python3 analyze.py                 (your data)
       python3 analyze.py --demo          (simulated bench session, only to preview the layout; outputs say DEMO)
"""
import argparse, csv, json, math, os, random, sqlite3, subprocess, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_COL = {"NORMAL": "#2e9e5b", "ADVISORY": "#e8a317", "CRITICAL": "#d62f2f", "TECHNICAL": "#1fb5c4", "NO_READING": "#bbbbbb"}
EXPECTED_EXPOSURE_S = {"ER6": 420.0}     # independently calculated from the ER6 script: 240 s at 35.8 C + 180 s at 35.6 C
ADV_C, CRIT_C, FALL_C, FALL_WIN_S = 36.0, 35.5, 0.5, 900
FALL_RULE = True          # must match USE_FALL_RULE in pico/code.py
plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 130})
DEMO = False


# ------------------------------------------------------------------ helpers
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def finish(fig, path):
    if DEMO:
        fig.text(0.5, 0.5, "SIMULATED DEMO\nNOT REAL RESULTS", ha="center", va="center", fontsize=34,
                 color="red", alpha=0.18, rotation=25, weight="bold")
    fig.tight_layout(); fig.savefig(path); plt.close(fig)
    print("  figure:", os.path.relpath(path, HERE))


def load_db(path):
    if not os.path.exists(path):
        print("No database found at", path); return []
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row
    has_raw = c.execute("SELECT count(*) FROM sqlite_master WHERE name='raw'").fetchone()[0]
    cases = []
    for k in c.execute("SELECT * FROM cases ORDER BY id"):
        cid = k["id"]
        raw = []
        if has_raw:
            for r in c.execute("SELECT ts, line FROM raw WHERE case_id=? ORDER BY ts", (cid,)):
                d = json.loads(r["line"]); d["_ts"] = r["ts"]; raw.append(d)
        ev = [dict(x) for x in c.execute("SELECT ts, kind, title, detail FROM events WHERE case_id=? ORDER BY ts", (cid,))]
        no = [dict(x) for x in c.execute("SELECT ts, status FROM notifications WHERE case_id=? ORDER BY ts", (cid,))]
        cases.append({"id": cid, "label": k["label"] or f"case{cid}", "raw": raw, "events": ev, "notes": no})
    return cases


def kinds_of(case):
    """A label like ER45-1 (one real cooling and rewarming cycle) counts for both ER-4 and ER-5."""
    lab = case["label"].upper().replace("-", "")
    if lab.startswith("ER45"):
        return {"ER4", "ER5"}
    for t in ("ER7R", "ER2", "ER3", "ER4", "ER5", "ER6", "ER7"):
        if lab.startswith(t):
            return {t}
    return {"OTHER"}


def kind_of(case):
    return next(iter(kinds_of(case)))


def dwell_of(raw):
    return next((x["dwell"] for x in raw if x.get("dwell")), 300)


def first_mark(case):
    m = [e["ts"] for e in case["events"] if e["kind"] == "mark"]
    return m[0] if m else None


def stat(R, key, vals):
    vals = [v for v in vals if v is not None and not math.isnan(v)]
    R[key + "_n"] = len(vals)
    if vals:
        R[key + "_min"], R[key + "_max"], R[key + "_mean"] = float(min(vals)), float(max(vals)), float(np.mean(vals))


# ------------------------------------------------------------------ figures for every case
def plot_trend(case, out):
    r = case["raw"]
    if len(r) < 2:
        return
    t0 = r[0]["_ts"]; t = np.array([x["_ts"] - t0 for x in r]) / 60
    y = np.array([np.nan if x.get("probe") is None else x["probe"] for x in r])
    fig, ax = plt.subplots(figsize=(8, 3.6))
    for i in range(len(r) - 1):
        s = "TECHNICAL" if r[i].get("technical") else r[i].get("state")
        ax.axvspan(t[i], t[i + 1], color=STATE_COL.get(s, "#ccc"), alpha=0.15, lw=0)
    ax.plot(t, y, color="#222", lw=1.3)
    for v, c in ((ADV_C, "#e8a317"), (CRIT_C, "#d62f2f")):
        ax.axhline(v, color=c, ls="--", lw=0.8)
    for e in case["events"]:
        if e["kind"] == "mark":
            ax.axvline((e["ts"] - t0) / 60, color="#555", ls=":", lw=0.9)
    for n in case["notes"]:
        if n["status"] == "sent" and np.isfinite(y).any():
            ax.plot((n["ts"] - t0) / 60, np.nanmin(y), marker="v", color="#d62f2f", ms=8)
    from matplotlib.patches import Patch
    hand = [plt.Line2D([], [], color="#222", label="Temperature input")] + \
           [Patch(color=c, alpha=0.3, label=s.title().replace("_", " ")) for s, c in STATE_COL.items()] + \
           [plt.Line2D([], [], color="#d62f2f", marker="v", ls="", label="Phone alert sent")]
    ax.legend(handles=hand, fontsize=7, ncol=4, loc="best")
    src = r[-1].get("source", "probe")
    ax.set_xlabel("Time since case start (min)"); ax.set_ylabel("Temperature (°C)")
    ax.set_title(f"Case '{case['label']}' ({'scripted input ' + src[7:] if src.startswith('script') else 'real probe'})")
    finish(fig, os.path.join(out, f"trend_{case['id']:03d}_{case['label']}.png".replace("/", "-")))


# ------------------------------------------------------------------ ER-1 calibration
def er1(rows, out, R):
    pts = [(num(r["reference_C"]), num(r["probe_C"]), r["bath"]) for r in rows if r["test"].upper() in ("ER1", "ER-1", "T1")]
    pts = [p for p in pts if p[0] is not None and p[1] is not None]
    if len(pts) < 3:
        print("  ER-1: not enough calibration rows with both reference_C and probe_C"); return
    ref = np.array([p[0] for p in pts]); pr = np.array([p[1] for p in pts])
    a, b = np.polyfit(ref, pr, 1)
    raw, corr = pr - ref, (pr - b) / a - ref
    R.update(cal_gain=a, cal_offset=b, cal_max_abs_error_raw=float(np.max(np.abs(raw))),
             cal_max_abs_residual=float(np.max(np.abs(corr))), cal_max_abs_residual_n=len(pts),
             cal_points=len(set(p[2] for p in pts)), cal_span_C=float(ref.max() - ref.min()))
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4))
    for bath in dict.fromkeys(p[2] for p in pts):
        m = [i for i, p in enumerate(pts) if p[2] == bath]
        a1.scatter(ref[m], pr[m], s=22, label=f"{bath} (n={len(m)})")
    lim = [min(ref.min(), pr.min()) - 1, max(ref.max(), pr.max()) + 1]
    a1.plot(lim, lim, "k:", lw=1, label="Ideal"); xs = np.linspace(*lim, 50)
    a1.plot(xs, a * xs + b, "r-", lw=1.2, label=f"Fit: probe = {a:.4f}·ref {b:+.3f}")
    a1.set_xlabel("Reference thermometer (°C)"); a1.set_ylabel("Probe (°C)"); a1.set_title("ER-1 calibration curve"); a1.legend(fontsize=8)
    a2.axhline(0, color="k", lw=0.8)
    a2.axhline(0.5, color="r", ls="--", lw=0.8); a2.axhline(-0.5, color="r", ls="--", lw=0.8)
    a2.scatter(ref, raw, s=20, label=f"Before calibration (max |e| {R['cal_max_abs_error_raw']:.2f} °C)")
    a2.scatter(ref, corr, s=20, marker="x", label=f"After calibration (max |e| {R['cal_max_abs_residual']:.2f} °C)")
    a2.set_xlabel("Reference thermometer (°C)"); a2.set_ylabel("Probe minus reference (°C)"); a2.set_title("Residuals, limit ±0.5 °C"); a2.legend(fontsize=8)
    finish(fig, os.path.join(out, "ER1_calibration.png"))


# ------------------------------------------------------------------ ER-2 sampling
def er2(cases, out, R):
    ivs, silent, valid = [], 0, []
    for c in cases:
        if kind_of(c) != "ER2" or len(c["raw"]) < 10:
            continue
        ts = np.array([x["_ts"] for x in c["raw"]]); iv = np.diff(ts); ivs.extend(iv)
        valid.append(100 * np.mean([bool(x.get("valid", x.get("contact") is not None)) for x in c["raw"]]))
        gaps = [e["ts"] for e in c["events"] if e["kind"] == "gap" and e["title"] == "Data gap"]
        for k in np.where(iv > 5)[0]:
            if not any(ts[k] <= g <= ts[k + 1] for g in gaps):
                silent += 1
        R["er2_duration_min"] = float((ts[-1] - ts[0]) / 60)
    if not ivs:
        print("  ER-2: no ER2 case found"); return
    ivs = np.array(ivs)
    R["intervals_in_0.9_1.1_pct"] = float(100 * np.mean((ivs >= 0.9) & (ivs <= 1.1)))
    R["intervals_in_0.9_1.1_pct_n"] = len(ivs)
    R["silent_gaps_over_5s"] = silent; R["valid_sample_pct"] = float(min(valid))
    fig, ax = plt.subplots(figsize=(6.5, 3.5))
    ax.hist(ivs, bins=np.linspace(0.8, 1.2, 41), color="#4a78b5")
    ax.axvspan(0.9, 1.1, color="g", alpha=0.08)
    ax.set_xlabel("Time between samples (s)"); ax.set_ylabel("Count")
    ax.set_title(f"ER-2 sampling: {R['intervals_in_0.9_1.1_pct']:.1f} % of {len(ivs)} intervals in 0.9-1.1 s, silent gaps >5 s: {silent}")
    finish(fig, os.path.join(out, "ER2_sampling.png"))


# ------------------------------------------------------------------ ER-3 step response
def er3(cases, out, R):
    runs = []
    for c in cases:
        if kind_of(c) != "ER3":
            continue
        m = first_mark(c)
        r = [x for x in c["raw"] if x.get("probe") is not None]
        if m is None or len(r) < 20:
            continue
        t = np.array([x["_ts"] for x in r]) - m; y = np.array([x["probe"] for x in r])
        before, after = y[(t < 0) & (t > -15)], y[t > t.max() - 15]
        if len(before) < 3 or len(after) < 3:
            continue
        y0, y1 = np.median(before), np.median(after); frac = (y - y0) / (y1 - y0)
        k = np.where((t >= 0) & (frac >= 0.9))[0]
        runs.append({"label": c["label"], "t": t, "frac": frac, "t90": float(t[k[0]]) if len(k) else math.nan,
                     "dir": "warm to cold" if y1 < y0 else "cold to warm", "y0": y0, "y1": y1})
    if not runs:
        print("  ER-3: no ER3 case with a Mark found"); return
    stat(R, "t90", [x["t90"] for x in runs])
    R["steps_warm_to_cold"] = sum(x["dir"] == "warm to cold" for x in runs)
    R["steps_cold_to_warm"] = sum(x["dir"] == "cold to warm" for x in runs)
    fig, ax = plt.subplots(figsize=(7.5, 4))
    for x in runs:
        k = x["t"] >= -10
        ax.plot(x["t"][k], x["frac"][k] * 100, lw=1.1, ls="-" if x["dir"] == "warm to cold" else "--",
                label=f"{x['label']}: {x['y0']:.1f}→{x['y1']:.1f} °C, t90 {x['t90']:.0f} s")
    ax.axhline(90, color="grey", ls=":"); ax.axvline(0, color="k", lw=0.8); ax.axvline(60, color="r", ls="--", lw=0.8)
    ax.set_xlabel("Time since the probe was moved (s)"); ax.set_ylabel("Step completed (%)")
    ax.set_title(f"ER-3 step response: t90 max {R['t90_max']:.1f} s (limit 60 s)"); ax.legend(fontsize=7)
    finish(fig, os.path.join(out, "ER3_step_response.png"))


# ------------------------------------------------------------------ ER-4, ER-5 alarm timing on scripted runs
def script_run(case):
    """Samples of the scripted run (after the last 'script' mark), or the real-probe samples when no script ran."""
    smarks = [e for e in case["events"] if e["kind"] == "mark" and str(e["detail"]).startswith("script")]
    if not smarks or str(smarks[-1]["detail"]).upper().endswith("OFF"):
        return [x for x in case["raw"] if not str(x.get("source", "probe")).startswith("script")] or case["raw"]
    t0 = smarks[-1]["ts"]
    return [x for x in case["raw"] if x["_ts"] > t0 and str(x.get("source", "")).startswith("script")
            and x.get("script_t") is not None and x["script_t"] < x["_ts"] - t0 + 2]


def first_ts(raw, cond, after=-1e18):
    for x in raw:
        if x["_ts"] >= after and cond(x):
            return x["_ts"]
    return None


def er4(cases, R, lat):
    errs = []
    for c in cases:
        if "ER4" not in kinds_of(c):
            continue
        raw = script_run(c)
        due = advisory_due(raw, dwell_of(raw))
        t_adv = first_ts(raw, lambda x: x.get("state") in ("ADVISORY", "CRITICAL"))   # Advisory or beyond
        if due and t_adv:
            errs.append(t_adv - due)
    stat(R, "adv_onset_error", errs); lat["ER-4 Advisory onset\n(after the dwell)"] = errs


def advisory_due(raw, dwell):
    """Independent calculation from the input values: first time the reading has been below 36.0 C for the dwell."""
    since = None
    for x in raw:
        v = x.get("probe")
        if v is None:
            continue
        since = (since or x["_ts"]) if v < ADV_C else None
        if since is not None and x["_ts"] - since >= dwell:
            return x["_ts"]
    return None


def qualifying_time(raw, dwell=300, fall_rule=True):
    """Independent calculation of the ER-5 moment: once Advisory is due, the first time the reading is below
    35.5 C for the dwell, or has fallen 0.5 C within 15 min."""
    t_adv = advisory_due(raw, dwell)
    if t_adv is None:
        return None
    hist, below_since = [], None
    for x in raw:
        v, t = x.get("probe"), x["_ts"]
        if v is None:
            continue
        hist = [(a, b) for a, b in hist if t - a <= FALL_WIN_S] + [(t, v)]
        below_since = (below_since or t) if v < CRIT_C else None
        if t >= t_adv and ((fall_rule and max(b for _, b in hist) - v >= FALL_C)
                           or (below_since is not None and t - below_since >= dwell)):
            return t
    return None


def er5(cases, R, lat):
    loc, msg, sent, n = [], [], 0, 0
    for c in cases:
        if "ER5" not in kinds_of(c):
            continue
        raw = script_run(c)
        tq = qualifying_time(raw, dwell_of(raw), FALL_RULE)
        t_crit = first_ts(raw, lambda x: x.get("state") == "CRITICAL" and x.get("ring") == "RED")
        att = [e["ts"] for e in c["events"] if e["title"] == "Phone alert attempt" and tq and e["ts"] >= tq - 2]
        if tq is None or t_crit is None:
            continue
        n += 1
        loc.append(t_crit - tq)
        if att:
            msg.append(att[0] - tq)
        sent += any(x["status"] == "sent" for x in c["notes"])
    stat(R, "crit_local_latency", loc); stat(R, "msg_attempt_latency", msg)
    if n:
        R["msg_logged_sent_pct"] = 100.0 * sent / n; R["msg_logged_sent_pct_n"] = n
    lat["ER-5 Critical, local\n(ring and sound)"] = loc; lat["ER-5 message\nsend attempt"] = msg


# ------------------------------------------------------------------ ER-6 cumulative minutes
def er6(cases, manual, R):
    errs = []
    for c in cases:
        if kind_of(c) != "ER6":
            continue
        raw = script_run(c) or c["raw"]
        src = next((x["source"][7:] for x in raw if str(x.get("source", "")).startswith("script")), None)
        sec = [x.get("sec_below") for x in raw if x.get("sec_below") is not None]
        dev = sum(max(0.0, b - a) for a, b in zip(sec, sec[1:]))
        exp = EXPECTED_EXPOSURE_S.get(src) if src else None
        if exp is None:
            sw = [num(r["observed"]) for r in manual if r["test"].upper() in ("ER6", "ER-6") and r["item"] == "stopwatch_s"]
            exp = sw[-1] if sw and sw[-1] is not None else None
        if exp is not None:
            errs.append(abs(dev - exp)); R["er6_device_s"], R["er6_expected_s"] = dev, exp
        exports = [x.get("min_below_36") for x in raw]
        R["er6_reported_to_0.01_min"] = all(v is None or abs(v * 100 - round(v * 100)) < 1e-6 for v in exports)
    stat(R, "exposure_abs_error", errs)


# ------------------------------------------------------------------ ER-7 technical fault
def er7(cases, R, lat):
    lats = []
    for c in cases:
        k = kind_of(c)
        if k == "ER7":
            m = first_mark(c)
            t = first_ts(c["raw"], lambda x: x.get("technical"), m) if m else None
            if t: lats.append(t - m)
        elif k == "ER7R":
            raw = script_run(c)
            t0 = first_ts(raw, lambda x: x.get("probe") is not None and not (25 <= x["probe"] <= 42))
            t = first_ts(raw, lambda x: x.get("technical"), t0) if t0 else None
            if t: lats.append(t - t0)
    stat(R, "technical_latency", lats); lat["ER-7 Technical fault"] = lats
    false_normal = 0
    for c in cases:
        for x in c["raw"]:
            if x.get("ring") == "GREEN" and not x.get("valid", x.get("contact") is not None):
                false_normal += 1
    R["false_normal_samples"] = false_normal


def latency_plot(lat, out):
    data = [(k, v) for k, v in lat.items() if v]
    if not data:
        return
    fig, ax = plt.subplots(figsize=(8, 4))
    for i, (k, v) in enumerate(data):
        ax.scatter(np.full(len(v), i) + np.random.uniform(-0.08, 0.08, len(v)), v, s=26, zorder=3)
        ax.hlines(np.mean(v), i - 0.25, i + 0.25, color="k", lw=1.5)
        ax.text(i + 0.27, np.mean(v), f"max {max(v):.1f} s\nn = {len(v)}", fontsize=7, va="center")
    ax.axhline(5, color="r", ls="--", lw=0.8); ax.text(len(data) - 0.5, 5.1, "5 s", color="r", fontsize=7)
    ax.axhline(10, color="r", ls=":", lw=0.8); ax.text(len(data) - 0.5, 10.1, "10 s", color="r", fontsize=7)
    ax.set_xticks(range(len(data)), [k for k, _ in data], fontsize=8); ax.set_xlim(-0.5, len(data) - 0.3)
    ax.set_ylabel("Seconds after the qualifying moment"); ax.set_title("Alarm and message latency (each dot = one run)")
    finish(fig, os.path.join(out, "ER4_ER5_ER7_latency.png"))


# ------------------------------------------------------------------ table
def manual_ok(manual, test):
    rows = [r for r in manual if r["test"].upper().replace("-", "") == test.replace("-", "")]
    if not rows or any(not r["pass"] for r in rows):
        return None
    return all(r["pass"].lower() in ("yes", "y", "pass", "1", "true") for r in rows)


def results_table(reqs, manual, R, out):
    rows = []
    for q in reqs:
        m, op, lim, need = q["metric"], q["operator"], q["limit"], int(num(q.get("min_n")) or 0)
        n = R.get(m + "_n", R.get(m.rsplit("_", 1)[0] + "_n"))
        if m == "not_verified":
            val, verdict = "", "NOT VERIFIED"
        elif m.startswith("manual:"):
            ok = manual_ok(manual, m.split(":")[1].upper())
            val, verdict = "observed", ("NO DATA" if ok is None else "PASS" if ok else "FAIL")
        elif op == "in":
            lo, hi = [float(v) for v in lim.split("..")]
            if m + "_min" in R:
                val = f"{R[m + '_min']:.2f} to {R[m + '_max']:.2f}"
                verdict = "PASS" if lo <= R[m + "_min"] and R[m + "_max"] <= hi else "FAIL"
                n = R.get(m + "_n")
            else:
                val, verdict = "", "NO DATA"
        elif m in R:
            v, L = R[m], num(lim)
            val = f"{v:.2f}" if isinstance(v, float) else str(v)
            verdict = "PASS" if (v <= L if op == "<=" else v >= L if op == ">=" else v == L) else "FAIL"
        else:
            val, verdict = "", "NO DATA"
        if verdict == "PASS" and need and (n or 0) < need:
            verdict = f"INCOMPLETE (n={n or 0} of {need})"
        rows.append([q["ER"], q["CR"], q["requirement"], m, f"{op} {lim} {q['unit']}".strip(), val,
                     "" if n is None else str(n), verdict])
    hdr = ["ER", "CR", "Requirement", "Metric", "Criterion", "Measured", "n", "Result"]
    with open(os.path.join(out, "results_table.csv"), "w", newline="") as f:
        csv.writer(f).writerows([hdr] + rows)
    md = ["# Verification results" + (" (SIMULATED DEMO, NOT REAL)" if DEMO else ""), "",
          "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)] + ["| " + " | ".join(r) + " |" for r in rows]
    md += ["", "## All computed values", ""] + [f"- {k}: {v:.3f}" if isinstance(v, float) else f"- {k}: {v}" for k, v in sorted(R.items())]
    open(os.path.join(out, "results.md"), "w").write("\n".join(md) + "\n")
    print("  table:  ", os.path.relpath(os.path.join(out, "results.md"), HERE))
    for r in rows:
        print(f"    {r[0]:6s} {r[7]:24s} {r[3]} = {r[5]}  (n={r[6]})")


# ------------------------------------------------------------------ demo (simulated bench session)
def make_demo(folder):
    os.makedirs(folder, exist_ok=True)
    db = os.path.join(folder, "monitor.db")
    subprocess.run([sys.executable, os.path.join(HERE, "sim", "bench_sim.py"), db], check=True, stdout=subprocess.DEVNULL)
    rnd = random.Random(3)
    cal = [["test", "bath", "clock_time", "reference_C", "probe_C", "operator", "notes"]]
    for bath, tr in (("warm 27", 27.0), ("warm 34", 34.0), ("warm 37", 37.0), ("hot 41", 41.0)):
        for i in range(5):
            r = tr + rnd.gauss(0, 0.03)
            cal.append(["ER1", bath, f"10:{i:02d}:00", f"{r:.2f}", f"{1.003 * r - 0.05 + rnd.gauss(0, 0.04):.2f}", "sim", ""])
    csv.writer(open(os.path.join(folder, "calibration_log.csv"), "w", newline="")).writerows(cal)
    man = read_csv(os.path.join(HERE, "templates", "manual_checks.csv"))
    for r in man:
        r["observed"], r["pass"], r["operator"] = "as expected (simulated)", "yes", "sim"
    with open(os.path.join(folder, "manual_checks.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(man[0].keys())); w.writeheader(); w.writerows(man)
    import shutil
    shutil.copy(os.path.join(HERE, "templates", "requirements.csv"), os.path.join(folder, "requirements.csv"))
    return db


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--db", default=os.path.join(HERE, "..", "server", "monitor.db"))
    ap.add_argument("--data", default=os.path.join(HERE, "data"))
    ap.add_argument("--no-trends", action="store_true", help="skip the per-case trend figures")
    a = ap.parse_args()
    DEMO = a.demo
    if DEMO:
        a.data = os.path.join(HERE, "demo_data"); a.db = make_demo(a.data)
        print("DEMO MODE: simulated bench session. Not verification evidence.")
    out = os.path.join(HERE, "results_DEMO" if DEMO else "results"); os.makedirs(out, exist_ok=True)
    if not DEMO and not os.path.isdir(a.data):
        print("Create the folder", a.data, "and copy the files from templates/ into it, then fill them in."); sys.exit(1)
    cases = load_db(a.db)
    cal, manual = read_csv(os.path.join(a.data, "calibration_log.csv")), read_csv(os.path.join(a.data, "manual_checks.csv"))
    reqs = read_csv(os.path.join(a.data, "requirements.csv"))
    print(f"{len(cases)} dashboard cases found")
    if not a.no_trends:
        for c in cases:
            plot_trend(c, out)
    R, lat = {}, {}
    er1(cal, out, R); er2(cases, out, R); er3(cases, out, R)
    er4(cases, R, lat); er5(cases, R, lat); er6(cases, manual, R); er7(cases, R, lat)
    latency_plot(lat, out)
    results_table(reqs, manual, R, out)
