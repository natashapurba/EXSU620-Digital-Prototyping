"""Laptop service for the perioperative temperature monitor.

Pico (code.py) --USB data port--> this service --> SQLite + browser dashboard + WhatsApp.

Run:
    python app.py --sim                 # no hardware, scripted demo case
    python app.py --port COM7           # real Pico (the *data* port, not Thonny's console port)
    python app.py --list-ports          # show candidate ports

Then open http://127.0.0.1:5050   (5050, not 5000: macOS AirPlay Receiver uses 5000)
"""
import argparse, csv, io, json, os, queue, random, sqlite3, threading, time, urllib.parse, urllib.request
from flask import Flask, Response, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "monitor.db")

# ---- settings you may change ------------------------------------------------
DISAGREE_C = 1.0          # CR-4: channels disagreeing by more than this raise a fault (set from your ER)
WA_MIN_GAP_S = 20         # do not send the same kind of WhatsApp message more often than this
NOTIFY_ON = {"ADVISORY", "CRITICAL", "TECHNICAL"}   # states that trigger a WhatsApp message
WA_PROVIDER = os.environ.get("WA_PROVIDER", "none")  # none | callmebot | twilio
# callmebot: WA_PHONE, WA_APIKEY      twilio: TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, WA_TO
# -----------------------------------------------------------------------------

app = Flask(__name__, static_folder=os.path.join(HERE, "static"))
lock = threading.RLock()
subscribers = []                      # one queue per open browser tab
pico_link = {"ser": None}             # set when a real serial port is open


def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

conn = db()
conn.executescript("""
CREATE TABLE IF NOT EXISTS cases(id INTEGER PRIMARY KEY, label TEXT, started REAL, stopped REAL);
CREATE TABLE IF NOT EXISTS readings(id INTEGER PRIMARY KEY, case_id INT, ts REAL, contact REAL, ir REAL,
  smooth REAL, state TEXT, technical INT, min_below REAL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, case_id INT, ts REAL, kind TEXT, title TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, case_id INT, ts REAL, channel TEXT,
  status TEXT, message TEXT, error TEXT);
""")

# ---- live state shown on the dashboard --------------------------------------
S = {
    "case": None,                 # {"id","label","started"} while a case is running
    "link": "waiting",            # waiting | connected | lost
    "last_rx": None,
    "contact": None, "ir": None, "smooth": None,
    "state": "NORMAL", "technical": False, "acknowledged": False,
    "min_below": 0.0, "fall": 0.0, "diff": None, "disagree": False, "ir_fault": False,
    "adv": 36.0, "crit": 35.5, "demo": False,
    "outputs": {
        "light":    {"status": "idle", "value": "OFF",    "since": None},
        "sound":    {"status": "idle", "value": "SILENT", "since": None},
        "whatsapp": {"status": "idle", "value": WA_PROVIDER, "since": None, "detail": "No message sent yet", "count": 0},
    },
    "events": [],                 # newest first, last 50
}
last_wa = {}                      # kind -> time of last attempt
prev_state = {"eff": None, "ack": False}


def push(msg):
    for q in list(subscribers):
        try:
            q.put_nowait(msg)
        except queue.Full:
            pass


def add_event(kind, title, detail=""):
    ts = time.time()
    ev = {"ts": ts, "kind": kind, "title": title, "detail": detail}
    S["events"].insert(0, ev)
    del S["events"][50:]
    cid = S["case"]["id"] if S["case"] else None
    conn.execute("INSERT INTO events(case_id,ts,kind,title,detail) VALUES(?,?,?,?,?)", (cid, ts, kind, title, detail))
    conn.commit()
    push({"type": "event", "event": ev})


# ---- WhatsApp ---------------------------------------------------------------
def wa_send(kind, text):
    """Runs in its own thread so a slow network never delays the dashboard."""
    o = S["outputs"]["whatsapp"]
    cid = S["case"]["id"] if S["case"] else None
    status, err = "sent", ""
    try:
        if WA_PROVIDER == "callmebot":
            url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
                {"phone": os.environ["WA_PHONE"], "text": text, "apikey": os.environ["WA_APIKEY"]})
            urllib.request.urlopen(url, timeout=10).read()
        elif WA_PROVIDER == "twilio":
            sid, tok = os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"]
            data = urllib.parse.urlencode({"From": "whatsapp:" + os.environ["TWILIO_FROM"],
                                           "To": "whatsapp:" + os.environ["WA_TO"], "Body": text}).encode()
            req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data)
            import base64
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode())
            urllib.request.urlopen(req, timeout=10).read()
        else:
            status, err = "not_configured", "WA_PROVIDER is 'none' (dry run: message was NOT sent)"
    except Exception as e:                        # network down, bad key, ...
        status, err = "failed", str(e)
    with lock:
        o.update(status=status, since=time.time(), detail=(text if status == "sent" else err))
        if status == "sent":
            o["count"] += 1
        conn.execute("INSERT INTO notifications(case_id,ts,channel,status,message,error) VALUES(?,?,?,?,?,?)",
                     (cid, time.time(), "whatsapp", status, text, err))
        conn.commit()
        add_event("notify", f"WhatsApp {status.replace('_', ' ')}", text if status == "sent" else err)
        push({"type": "state", "state": S})


def maybe_notify(eff):
    if eff not in NOTIFY_ON or not S["case"]:
        return
    now = time.time()
    if now - last_wa.get(eff, 0) < WA_MIN_GAP_S:
        return
    last_wa[eff] = now
    t = S["contact"]
    text = {
        "ADVISORY": f"ADVISORY: temperature below {S['adv']:.1f} C for the required time (now {t:.1f} C). Please start active warming.",
        "CRITICAL": f"CRITICAL: temperature below {S['crit']:.1f} C or falling fast (now {t:.1f} C). Escalate warming now.",
        "TECHNICAL": "TECHNICAL FAULT: temperature channel lost, out of range or channels disagree. Check sensors.",
    }[eff] + f" Case {S['case']['label']}."
    S["outputs"]["whatsapp"].update(status="sending", since=now, detail="Sending...")
    threading.Thread(target=wa_send, args=(eff, text), daemon=True).start()


# ---- handle one JSON line from the Pico (or the simulator) -----------------
def handle_sample(d):
    with lock:
        now = time.time()
        S["link"], S["last_rx"] = "connected", now
        S["contact"] = d.get("contact")
        S["ir"] = d.get("ir")                       # None until the MLX90614 is added to code.py
        S["smooth"] = d.get("smooth")
        S["state"] = d.get("state", "NORMAL")
        S["technical"] = bool(d.get("technical"))
        S["acknowledged"] = bool(d.get("ack"))
        S["min_below"] = d.get("min_below_36", 0.0)
        S["fall"] = d.get("fall", 0.0)
        for k in ("adv", "crit", "demo"):
            if k in d:
                S[k] = d[k]

        # CR-4: flag disagreement, never average
        S["ir_fault"] = S["ir"] is None
        if S["contact"] is not None and S["ir"] is not None:
            S["diff"] = round(abs(S["contact"] - S["ir"]), 2)
            S["disagree"] = S["diff"] > DISAGREE_C
        else:
            S["diff"], S["disagree"] = None, False
        eff = "TECHNICAL" if (S["technical"] or S["disagree"]) else S["state"]

        # outputs, as reported by the Pico
        ring = d.get("ring", {"NORMAL": "GREEN", "ADVISORY": "AMBER", "CRITICAL": "RED"}.get(S["state"], "OFF"))
        snd = d.get("sound", "SILENT")
        if S["outputs"]["light"]["value"] != ring:
            S["outputs"]["light"].update(value=ring, since=now)
        if S["outputs"]["sound"]["value"] != snd:
            S["outputs"]["sound"].update(value=snd, since=now)
        S["outputs"]["light"]["status"] = "on"
        S["outputs"]["sound"]["status"] = "silenced" if (snd == "SILENT" and S["state"] == "CRITICAL") else ("playing" if snd != "SILENT" else "idle")

        # events on transitions
        if eff != prev_state["eff"]:
            if prev_state["eff"] is not None or eff != "NORMAL":
                t = S["contact"]
                add_event("state", f"{eff.title()} " + ("started" if eff != "NORMAL" else "cleared"),
                          "" if t is None else f"Probe {t:.1f} C")
            maybe_notify(eff)
            prev_state["eff"] = eff
        if S["acknowledged"] and not prev_state["ack"]:
            add_event("ack", "Alarm acknowledged", "Condition remains latched until back in range")
        prev_state["ack"] = S["acknowledged"]

        if S["case"]:
            conn.execute("INSERT INTO readings(case_id,ts,contact,ir,smooth,state,technical,min_below) VALUES(?,?,?,?,?,?,?,?)",
                         (S["case"]["id"], now, S["contact"], S["ir"], S["smooth"], eff, int(eff == "TECHNICAL"), S["min_below"]))
            conn.commit()
        push({"type": "sample", "ts": now, "contact": S["contact"], "ir": S["ir"], "smooth": S["smooth"], "eff": eff,
              "state": S})


# ---- sources: real serial port or simulator --------------------------------
def serial_reader(port):
    import serial                                     # pip install pyserial
    while True:
        try:
            ser = serial.Serial(port, 115200, timeout=1)
            pico_link["ser"] = ser
            while True:
                line = ser.readline().decode(errors="ignore").strip()
                if line.startswith("{"):
                    try:
                        handle_sample(json.loads(line))
                    except ValueError:
                        pass
                elif S["last_rx"] and time.time() - S["last_rx"] > 5:
                    S["link"] = "lost"
        except Exception:
            pico_link["ser"] = None
            S["link"] = "lost" if S["last_rx"] else "waiting"
            push({"type": "state", "state": S})
            time.sleep(2)


SIM = {"ack_flag": False}

def simulator():
    """Scripted case in DEMO units (advisory 25.0, critical 24.5, dwell 30 s) so you can test the UI with no hardware."""
    t0, below_adv, below_crit, st, ack, mins = time.time(), None, None, "NORMAL", False, 0.0
    while True:
        t = time.time() - t0
        base = 26.5 - 0.04 * t if t < 60 else (24.1 if t < 140 else 24.1 + 0.05 * (t - 140))  # cool, plateau, warm
        base = min(base, 27.0)
        c = base + random.uniform(-0.03, 0.03)
        ir = c + 0.15 + random.uniform(-0.05, 0.05)
        if 200 < t < 215:
            ir += 2.5                                    # draught on the infrared channel only
        if c < 25.0:
            below_adv = below_adv or t; mins += 1 / 60
        else:
            below_adv = None
        below_crit = (below_crit or t) if c < 24.5 else None
        if st == "NORMAL" and below_adv and t - below_adv >= 30: st = "ADVISORY"
        elif st == "ADVISORY" and below_crit and t - below_crit >= 30: st, ack = "CRITICAL", False
        elif st == "ADVISORY" and c > 25.2: st = "NORMAL"
        if SIM["ack_flag"] and st == "CRITICAL": ack = True
        SIM["ack_flag"] = False
        if st == "CRITICAL" and ack and c >= 25.0: st, ack = "NORMAL", False
        ring = {"NORMAL": "GREEN", "ADVISORY": "AMBER", "CRITICAL": "RED"}[st]
        handle_sample({"contact": round(c, 2), "ir": None, "smooth": round(c, 2), "state": st, "technical": False,
                       "ack": ack, "min_below_36": round(mins, 2), "fall": 0.0, "ring": ring,
                       "sound": "SILENT" if (st == "NORMAL" or ack) else st, "adv": 25.0, "crit": 24.5, "demo": True})
        time.sleep(1)


# ---- HTTP API ----------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/state")
def api_state():
    with lock:
        return jsonify(S)


@app.route("/stream")
def stream():
    q = queue.Queue(maxsize=200)
    subscribers.append(q)

    def gen():
        try:
            with lock:
                yield f"data: {json.dumps({'type': 'state', 'state': S})}\n\n"
            while True:
                try:
                    yield f"data: {json.dumps(q.get(timeout=15))}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            subscribers.remove(q)
    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.route("/api/ack", methods=["POST"])
def api_ack():
    ser = pico_link["ser"]
    SIM["ack_flag"] = True
    if ser is not None:
        ser.write(b"ACK\n")            # Pico only honours this while state is CRITICAL
    return jsonify(ok=True, sent_to_pico=ser is not None)


@app.route("/api/case", methods=["POST"])
def api_case():
    action = (request.get_json(silent=True) or {}).get("action")
    with lock:
        if action == "start" and not S["case"]:
            label = (request.get_json() or {}).get("label") or "TEST-001"
            cur = conn.execute("INSERT INTO cases(label,started) VALUES(?,?)", (label, time.time()))
            conn.commit()
            S["case"] = {"id": cur.lastrowid, "label": label, "started": time.time()}
            S["events"].clear(); last_wa.clear(); prev_state["eff"] = None
            add_event("case", "Monitoring started", f"Case {label}")
        elif action == "stop" and S["case"]:
            add_event("case", "Monitoring stopped", f"{S['min_below']:.2f} min below threshold")
            conn.execute("UPDATE cases SET stopped=? WHERE id=?", (time.time(), S["case"]["id"]))
            conn.commit()
            S["case"] = None
        push({"type": "state", "state": S})
    return jsonify(ok=True)


def case_data(cid):
    cur = conn.cursor()
    return ([dict(r) for r in cur.execute("SELECT * FROM readings WHERE case_id=? ORDER BY ts", (cid,))],
            [dict(r) for r in cur.execute("SELECT * FROM events WHERE case_id=? ORDER BY ts", (cid,))],
            [dict(r) for r in cur.execute("SELECT * FROM notifications WHERE case_id=? ORDER BY ts", (cid,))])


@app.route("/api/history")
def api_history():
    cid = S["case"]["id"] if S["case"] else (conn.execute("SELECT max(id) FROM cases").fetchone()[0])
    if not cid:
        return jsonify(readings=[])
    r, _, _ = case_data(cid)
    return jsonify(readings=r[-900:])


@app.route("/api/export")
def api_export():
    """Handover summary: JSON by default, ?fmt=csv for the raw readings."""
    cid = request.args.get("case") or (S["case"]["id"] if S["case"] else conn.execute("SELECT max(id) FROM cases").fetchone()[0])
    if not cid:
        return jsonify(error="no case recorded yet"), 404
    readings, events, notes = case_data(cid)
    if request.args.get("fmt") == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(readings[0].keys()) if readings else ["empty"])
        w.writeheader(); w.writerows(readings)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=case_{cid}_readings.csv"})
    case = dict(conn.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone())
    c = [r["contact"] for r in readings if r["contact"] is not None]
    summary = {"case": case, "minutes_below_threshold": readings[-1]["min_below"] if readings else 0,
               "min_temp": min(c) if c else None, "max_temp": max(c) if c else None,
               "alarms": [e for e in events if e["kind"] in ("state", "ack")],
               "notifications": notes, "events": events}
    return Response(json.dumps(summary, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename=case_{cid}_handover.json"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port of the Pico DATA channel, e.g. COM7 or /dev/ttyACM1")
    ap.add_argument("--sim", action="store_true", help="run a scripted demo case with no hardware")
    ap.add_argument("--list-ports", action="store_true")
    ap.add_argument("--http-port", type=int, default=5050, help="web page port (default 5050)")
    a = ap.parse_args()
    if a.list_ports:
        from serial.tools import list_ports
        for p in list_ports.comports():
            print(p.device, "-", p.description)
        raise SystemExit
    if a.sim:
        threading.Thread(target=simulator, daemon=True).start()
    elif a.port:
        threading.Thread(target=serial_reader, args=(a.port,), daemon=True).start()
    else:
        print("Give --port <data port> or --sim")
        raise SystemExit(1)
    print(f"Dashboard: open http://127.0.0.1:{a.http_port}")
    app.run(host="127.0.0.1", port=a.http_port, threaded=True)
