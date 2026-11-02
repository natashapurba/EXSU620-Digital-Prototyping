"""Software-in-the-loop check: runs the REAL pico/code.py under CPython (hardware replaced by stubs, fake clock)
and the REAL server/app.py logic, so the whole chain can be exercised without hardware.
It does NOT replace the bench tests: results from this file are a design check, not verification evidence.

    python3 bench_sim.py out.db       -> writes a dashboard database you can feed to analyze.py
"""
import os, sys, types, runpy, json, random, importlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DB_OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "sim.db"))
EPOCH = 1.9e9


class Clock:
    t = 0.0
clock = Clock()


class StopSim(Exception):
    pass


# ---------------------------------------------------------------- the simulated bench (world)
class World:
    water = 37.0            # bath temperature when the real probe is used
    connected = True        # probe wire in place
    rnd = random.Random(7)
world = World()


# ---------------------------------------------------------------- CircuitPython stubs
def make_stubs():
    board = types.ModuleType("board")
    for n in range(30):
        setattr(board, "GP%d" % n, "GP%d" % n)
    dio = types.ModuleType("digitalio")
    class Pull: UP = "up"; DOWN = "down"
    class DigitalInOut:
        def __init__(self, pin): self.value = True
        def switch_to_input(self, pull=None): self.value = True
        def deinit(self): pass
    dio.Pull, dio.DigitalInOut = Pull, DigitalInOut
    neo = types.ModuleType("neopixel")
    class NeoPixel:
        def __init__(self, *a, **k): self.colour = None
        def fill(self, c): self.colour = c
        def show(self): pass
    neo.NeoPixel = NeoPixel
    pwm = types.ModuleType("pwmio")
    class PWMOut:
        def __init__(self, *a, **k): self.duty_cycle = 0; self.frequency = 440
        def deinit(self): pass
    pwm.PWMOut = PWMOut
    cdc = types.ModuleType("usb_cdc")
    class Data:
        def __init__(self): self.rx = b""; self.tx = []
        @property
        def in_waiting(self): return len(self.rx)
        def read(self, n): d, self.rx = self.rx[:n], self.rx[n:]; return d
        def write(self, b): self.tx.append(b.decode()); return len(b)
    cdc.data = Data()
    ow_pkg = types.ModuleType("adafruit_onewire"); ow = types.ModuleType("adafruit_onewire.bus")
    class Dev: family_code = 0x28
    class OneWireBus:
        def __init__(self, pin): pass
        def scan(self): return [Dev()] if world.connected else []
    ow.OneWireBus = OneWireBus
    ds = types.ModuleType("adafruit_ds18x20")
    class DS18X20:
        def __init__(self, bus, dev): self.resolution = 12
        def start_temperature_read(self):
            if not world.connected: raise RuntimeError("no presence pulse")
            return 0.75
        def read_temperature(self):
            if not world.connected: raise RuntimeError("no presence pulse")
            return round(world.water + world.rnd.gauss(0, 0.03), 4)
    ds.DS18X20 = DS18X20
    tm = types.ModuleType("time")
    tm.monotonic = lambda: clock.t
    def sleep(dt):
        clock.t = round(clock.t + dt, 4)
        tick()
    tm.sleep = sleep
    for name, mod in (("board", board), ("digitalio", dio), ("neopixel", neo), ("pwmio", pwm), ("usb_cdc", cdc),
                      ("adafruit_onewire", ow_pkg), ("adafruit_onewire.bus", ow), ("adafruit_ds18x20", ds), ("time", tm)):
        sys.modules[name] = mod
    return cdc.data


# ---------------------------------------------------------------- the dashboard (real app.py)
def load_app():
    import time as real_time
    sys.path.insert(0, os.path.join(ROOT, "server"))
    if os.path.exists(DB_OUT): os.remove(DB_OUT)
    os.environ.setdefault("WA_PROVIDER", "")
    src = open(os.path.join(ROOT, "server", "app.py")).read().replace(
        'DB_PATH = os.path.join(HERE, "monitor.db")', f'DB_PATH = {DB_OUT!r}')
    mod = types.ModuleType("app_sim"); mod.__file__ = os.path.join(ROOT, "server", "app.py")
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    fake_time = types.SimpleNamespace(time=lambda: EPOCH + clock.t, sleep=lambda s: None)
    mod.time = fake_time
    class SyncThread:
        def __init__(self, target, args=(), daemon=None): self.f, self.a = target, args
        def start(self): self.f(*self.a)
    mod.threading = types.SimpleNamespace(Thread=SyncThread, RLock=lambda: mod.lock)
    mod.WA_PROVIDER = "imessage"
    mod.provider_send = lambda text: None          # pretend the Mac accepted the message
    return mod


link = None
app = None
schedule = []          # (time, function)
gap_state = {"in": False}


def tick():
    # deliver Pico output lines to the dashboard
    while link.tx:
        line = link.tx.pop(0).strip()
        if line.startswith("{"):
            app.handle_sample(json.loads(line))
    # dashboard gap watchdog (same rule as app.gap_watchdog)
    if app.S["last_rx"]:
        age = EPOCH + clock.t - app.S["last_rx"]
        if age > 2.5 and not gap_state["in"]:
            gap_state["in"] = True; app.add_event("gap", "Data gap", "")
        if age <= 2.5 and gap_state["in"]:
            gap_state["in"] = False; app.add_event("gap", "Data resumed", "")
    while schedule and schedule[0][0] <= clock.t:
        schedule.pop(0)[1]()


class FakeSerial:
    def write(self, b): link.rx += b


def api(path, body=None):
    with app.app.test_client() as c:
        c.post(path, json=body or {})


# ---------------------------------------------------------------- the test session (what the group does at the bench)
def build_session():
    t = 5.0
    def at(dt, f):
        nonlocal t
        t += dt; schedule.append((t, f))
    def setw(**k): return lambda: [setattr(world, a, v) for a, v in k.items()]
    case = lambda label: (lambda: api("/api/case", {"action": "start", "label": label}))
    stop = lambda: api("/api/case", {"action": "stop"})
    mark = lambda note: (lambda: api("/api/mark", {"note": note}))
    # ER-2: 10 min on the real probe in a stable 37 C bath
    at(0, setw(water=37.0)); at(5, case("ER2-run1")); at(600, stop)
    # ER-3: 3 warm-to-cold and 3 cold-to-warm steps (bath change modelled as first-order, tau 8 s)
    for i in range(3):
        for d, (a, b) in (("w2c", (38.0, 30.0)), ("c2w", (30.0, 38.0))):
            at(2, setw(water=a)); at(20, case(f"ER3-{d}-{i+1}"))
            at(20, mark(d))
            for k in range(1, 61):
                at(1, (lambda v: setw(water=v))(b + (a - b) * 2.718281828 ** (-k / 8)))
            at(20, stop)
    # ER-4 and ER-5: 5 scripted episodes each (30 s bench dwell)
    for i in range(5):
        at(5, case(f"ER4-{i+1}")); at(1, lambda: api("/api/script", {"name": "ER4"})); at(85, stop)
    for i in range(5):
        at(5, case(f"ER5-{i+1}")); at(1, lambda: api("/api/script", {"name": "ER5"}))
        at(90, lambda: api("/api/ack")); at(80, stop)
    # ER-4 and ER-5 together: 5 real cooling and rewarming cycles in water (probe moved between baths)
    at(2, lambda: api("/api/script", {"name": "OFF"}))
    for i in range(5):
        at(0, setw(water=37.5)); at(15, case(f"ER45-{i+1}"))
        at(15, mark("into cool bath"))
        for k in range(1, 121):
            at(1, (lambda v: setw(water=v))(34.8 + (37.5 - 34.8) * 2.718281828 ** (-k / 8)))
        at(1, lambda: api("/api/ack")); at(1, mark("into warm bath"))
        for k in range(1, 61):
            at(1, (lambda v: setw(water=v))(37.5 + (34.8 - 37.5) * 2.718281828 ** (-k / 8)))
        at(5, stop)
    # ER-6: one scripted profile
    at(5, case("ER6-1")); at(1, lambda: api("/api/script", {"name": "ER6"})); at(680, stop)
    # ER-7: 10 induced probe disconnections (real probe), plus 1 scripted out-of-range run
    at(2, lambda: api("/api/script", {"name": "OFF"})); at(0, setw(water=37.0))
    for i in range(10):
        at(15, case(f"ER7-{i+1}")); at(10, mark("pull wire")); at(0, setw(connected=False))
        at(12, setw(connected=True)); at(15, stop)
    at(5, case("ER7R-1")); at(1, lambda: api("/api/script", {"name": "ER7R"})); at(100, stop)
    at(2, lambda: api("/api/script", {"name": "OFF"}))
    at(5, lambda: (_ for _ in ()).throw(StopSim()))
    return t


if __name__ == "__main__":
    import flask, logging                          # load the dashboard with the real time module first
    app = load_app()
    link = make_stubs()                            # then swap in the CircuitPython stubs for the Pico code
    app.pico_link["ser"] = FakeSerial()
    end = build_session()
    print(f"simulating {end/60:.0f} min of bench time ...")
    try:
        runpy.run_path(os.path.join(ROOT, "pico", "code.py"), run_name="__main__")
    except StopSim:
        pass
    print("done ->", DB_OUT)
