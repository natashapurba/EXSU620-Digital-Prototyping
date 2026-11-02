# HYPOTHERMIA MONITOR v3.2 (CALIBRATED) - waterproof DS18B20 probe, alarm logic of Figure 3 (EXSU 620 Project 05).
# Same as v3.1, plus the ER-1 calibration correction. Save this file on CIRCUITPY as code.py.
# BENCH TIME SCALE: every dwell is 30 s instead of the clinical 300 s (DWELL_S below). Set DWELL_S = 300 for clinical timing.
#
#   NORMAL   -> ADVISORY  below 36.0 C continuously for DWELL_S                     (ER-4, CR-2)
#   ADVISORY -> NORMAL    above 36.2 C continuously for DWELL_S
#   ADVISORY -> CRITICAL  below 35.5 C continuously for DWELL_S, or a fall of 0.5 C within 15 min (ER-5)
#   CRITICAL -> NORMAL    only when acknowledged AND back at or above 36.0 C (latched)
#   TECHNICAL             probe lost, or reading outside 25-42 C, for 3 s (overrides the display)  (ER-7)
#   No valid reading yet  ring dim white, never green ("no false normal")                         (ER-7)
#
# Laptop dashboard link (needs boot.py): one JSON line per second on the second USB port.
# Commands from the dashboard:  ACK | RESET (new case: zero the time-below counter) |
#                               SCRIPT <name> (scripted temperature profile instead of the probe) | SCRIPT OFF
import json, time, board, digitalio, neopixel, pwmio
import usb_cdc
try:
    from adafruit_onewire.bus import OneWireBus
    import adafruit_ds18x20
    PROBE_LIBS = True
except ImportError:
    PROBE_LIBS = False

# ===================== SETTINGS =====================
ADVISORY_C   = 36.0     # CR-2
ADV_EXIT_C   = 36.2     # Figure 3: back to Normal above this ...
CRITICAL_C   = 35.5     # CR-2
DWELL_S      = 30       # BENCH: 30 s (clinical value 300 s = 5 min). Used by all three dwell rules
USE_FALL_RULE = True    # Figure 3 'falling 0.5 C in 15 min'. False = Critical only after DWELL_S below 35.5 C
FALL_C       = 0.5      # Figure 3: or falling 0.5 C ...
FALL_WIN_S   = 900      # ... within 15 minutes
RANGE_MIN_C, RANGE_MAX_C = 25.0, 42.0   # ER-1 measuring range; outside = Technical (ER-7)
FAULT_S      = 3        # Technical alarm when there is no valid reading for this long (ER-7: <= 5 s)
SMOOTH_N     = 3        # moving average for the alarm rules (the raw value is reported and counted)
SAMPLE_PERIOD_S = 1.0   # ER-2: one sample per second

# ---- ER-1 calibration (2 Nov 2026, 7 baths 33.7-40.8 C, fever thermometer as reference) ----
# Fitted line: raw = CAL_GAIN * reference + CAL_OFFSET  ->  corrected = (raw - CAL_OFFSET) / CAL_GAIN
CAL_ON     = True        # False = use the raw probe value (no correction)
CAL_GAIN   = 0.9956
CAL_OFFSET = -0.279
SEARCH_EVERY_S  = 5
CRITICAL_FLASH_HZ, TECH_FLASH_HZ = 2.5, 1.0

# Scripted profiles for verification (ER-4, ER-5, ER-6, ER-7). Each is a list of (seconds, temperature C).
# The last temperature is held after the list ends. Starting a script restarts the alarm logic and the counter.
SCRIPTS = {
    "ER4":  [(20, 36.3), (60, 35.9)],                           # Advisory expected DWELL_S after the drop below 36.0
    "ER5":  [(20, 36.3), (45, 35.9), (60, 35.4), (40, 36.5)],   # Advisory, then below 35.5 -> Critical, then warm
    "ER6":  [(60, 36.5), (240, 35.8), (120, 36.4), (180, 35.6), (60, 36.5)],   # exactly 420 s below 36.0 C
    "ER7R": [(30, 37.0), (30, 45.0), (30, 37.0)],               # out of range (45 C) -> Technical
}

RING_PIN, NUM_PIXELS = board.GP16, 16
SPEAKER_PIN = board.GP20
BUTTON_PIN  = board.GP21    # optional bedside button (GP21 to GND); not fitted
PROBE_PIN   = board.GP17    # adapter DAT -> GP17, VCC -> 3V3, GND -> GND

GREEN, AMBER, RED, CYAN, WHITE, OFF = (0, 255, 0), (255, 110, 0), (255, 0, 0), (0, 200, 200), (40, 40, 40), (0, 0, 0)

# ===================== HARDWARE =====================
ring = neopixel.NeoPixel(RING_PIN, NUM_PIXELS, brightness=0.15, auto_write=False)
speaker = pwmio.PWMOut(SPEAKER_PIN, frequency=440, duty_cycle=0, variable_frequency=True)
button = digitalio.DigitalInOut(BUTTON_PIN)
button.switch_to_input(pull=digitalio.Pull.UP)

# ===================== PROBE =====================
probe_bus = probe = probe_ready_at = None
last_raw = None              # uncorrected probe value, reported as "probe_raw"

def find_probe():
    global probe_bus, probe
    probe = None
    if not PROBE_LIBS:
        return
    try:
        if probe_bus is None:
            probe_bus = OneWireBus(PROBE_PIN)
        for device in probe_bus.scan():
            if device.family_code == 0x28:
                probe = adafruit_ds18x20.DS18X20(probe_bus, device)
                probe.resolution = 12
                print(">>> Probe found on GP17")
                return
    except Exception as err:
        print(">>> Probe not answering (%s)" % err)
    print(">>> Probe not found - check DAT->GP17, VCC->3V3, GND->GND and the screw terminals")

def read_probe(now):
    """(temperature or None, reason). Non-blocking: starts a conversion and collects it on a later call."""
    global probe, probe_ready_at
    if probe is None:
        return None, "probe not found - check its wires"
    try:
        if probe_ready_at is None:
            probe_ready_at = now + probe.start_temperature_read()
            return None, "starting"
        if now < probe_ready_at:
            return None, "starting"
        value = probe.read_temperature()
        probe_ready_at = now + probe.start_temperature_read()
        if value == 85.0:
            return None, "starting"
        global last_raw
        last_raw = value
        if CAL_ON:
            value = (value - CAL_OFFSET) / CAL_GAIN
        return value, ""
    except Exception:
        probe, probe_ready_at = None, None
        return None, "probe stopped answering - check its wires"

# ===================== SCRIPTED INPUT =====================
script_name, script_t0 = None, 0.0

def script_temp(now):
    t = now - script_t0
    for dur, temp in SCRIPTS[script_name]:
        if t < dur:
            return temp
        t -= dur
    return SCRIPTS[script_name][-1][1]

# ===================== SOUND =====================
PATTERNS = {"ADVISORY": (660, 3, 30), "CRITICAL": (1000, 10, 10), "TECHNICAL": (1500, 1, 5)}
PULSE_ON, PULSE_OFF = 0.12, 0.08
burst_start = burst_kind = None

def update_sound(kind, now, silenced):
    global burst_start, burst_kind
    if kind not in PATTERNS or silenced:
        speaker.duty_cycle = 0
        burst_kind = None
        return
    freq, pulses, repeat = PATTERNS[kind]
    if kind != burst_kind or now - burst_start >= repeat:
        burst_start, burst_kind = now, kind
        speaker.frequency = freq
    t = now - burst_start
    on = int(t / (PULSE_ON + PULSE_OFF)) < pulses and (t % (PULSE_ON + PULSE_OFF)) < PULSE_ON
    speaker.duty_cycle = 32768 if on else 0

def update_ring(colour, now, flash_hz=0):
    if flash_hz and int(now * flash_hz * 2) % 2:
        colour = OFF
    ring.fill(colour)
    ring.show()

# ===================== DASHBOARD LINK =====================
link = usb_cdc.data
rx_buf = b""

def send_to_ui(text):
    print(text)
    if link is not None:
        try:
            link.write((text + "\n").encode())
        except Exception:
            pass

def read_commands():
    global rx_buf
    cmds = []
    if link is None or not link.in_waiting:
        return cmds
    rx_buf += link.read(link.in_waiting)
    while b"\n" in rx_buf:
        line, rx_buf = rx_buf.split(b"\n", 1)
        line = line.strip().decode()
        if line:
            cmds.append(line)
    return cmds

# ===================== ALARM STATE =====================
def reset_logic():
    """Fresh start: used at power-up and when a script starts."""
    global state, acknowledged, below_adv_since, above_exit_since, below_crit_since, recent, history, smooth, fall
    state, acknowledged = "NORMAL", False
    below_adv_since = above_exit_since = below_crit_since = None
    recent, history, smooth, fall = [], [], None, 0.0

reset_logic()
sec_below = 0.0              # CR-3 / ER-6: seconds below 36.0 C since the last RESET (new case)
last_valid_t = None          # time of the last valid reading
cur_valid = False            # was the latest sample valid? Green is only shown when it was (ER-7)
bad_since = None
technical = False
was_pressed = ack_armed = False
last_status = None
reason = ""

print("Calibration %s: corrected = (raw %+.3f) / %.4f" % ("ON" if CAL_ON else "OFF", -CAL_OFFSET, CAL_GAIN))
print("MONITOR v3.2 - advisory below %.1f C for %d s, critical below %.1f C for %d s%s"
      % (ADVISORY_C, DWELL_S, CRITICAL_C, DWELL_S,
         (" or falling %.1f C in %d min" % (FALL_C, FALL_WIN_S // 60)) if USE_FALL_RULE else ""))
if not PROBE_LIBS:
    print(">>> Libraries missing: copy adafruit_onewire (folder) and adafruit_ds18x20 into CIRCUITPY/lib")
find_probe()
last_search = time.monotonic()
next_sample = time.monotonic()

while True:
    now = time.monotonic()

    # ---- commands from the dashboard ----
    for cmd in read_commands():
        if cmd == "ACK" and state == "CRITICAL":
            acknowledged = True
        elif cmd == "RESET":
            sec_below = 0.0
            print(">>> New case: time-below counter reset")
        elif cmd.startswith("SCRIPT"):
            name = cmd[6:].strip().upper()
            if name in SCRIPTS:
                script_name, script_t0 = name, now
                reset_logic(); sec_below = 0.0; last_valid_t = None; bad_since = None; cur_valid = False
                print(">>> Script %s started" % name)
            else:
                script_name = None
                reset_logic(); last_valid_t = None; cur_valid = False
                print(">>> Script off - reading the probe")

    # ---- optional bedside button: only a NEW press during Critical counts ----
    pressed = not button.value
    if state == "CRITICAL":
        if not pressed:
            ack_armed = True
        elif ack_armed and not was_pressed:
            acknowledged = True
    else:
        ack_armed = False
    was_pressed = pressed

    if now >= next_sample:
        next_sample += SAMPLE_PERIOD_S
        if next_sample < now:                       # never try to catch up after a pause
            next_sample = now + SAMPLE_PERIOD_S

        # ---- 1. reading: script or probe ----
        if script_name:
            temp, reason = script_temp(now), ""
        else:
            if probe is None and PROBE_LIBS and now - last_search >= SEARCH_EVERY_S:
                find_probe()
                last_search = now
            last_raw = None
            temp, reason = read_probe(now)
        valid = temp is not None and RANGE_MIN_C <= temp <= RANGE_MAX_C
        cur_valid = valid
        if temp is not None and not valid:
            reason = "reading %.1f C is outside %.0f-%.0f C" % (temp, RANGE_MIN_C, RANGE_MAX_C)

        # ---- 2. Technical fault: no valid reading for FAULT_S seconds ----
        if valid:
            bad_since = None
        elif bad_since is None:
            bad_since = now
        technical = bad_since is not None and now - bad_since >= FAULT_S

        if valid:
            # ---- 3. CR-3 counter: raw reading, real elapsed time (gaps are not counted) ----
            if last_valid_t is not None and temp < ADVISORY_C:
                sec_below += min(now - last_valid_t, 2 * SAMPLE_PERIOD_S)
            last_valid_t = now

            # ---- 4. smoothing, dwell timers, fall over 15 min ----
            recent.append(temp)
            if len(recent) > SMOOTH_N:
                recent.pop(0)
            smooth = sum(recent) / len(recent)
            below_adv_since  = (below_adv_since or now)  if smooth < ADVISORY_C else None
            above_exit_since = (above_exit_since or now) if smooth > ADV_EXIT_C else None
            below_crit_since = (below_crit_since or now) if smooth < CRITICAL_C else None
            history.append((now, smooth))
            while now - history[0][0] > FALL_WIN_S:
                history.pop(0)
            fall = max(h[1] for h in history) - smooth

            # ---- 5. Figure 3 ----
            if state == "NORMAL" and below_adv_since is not None and now - below_adv_since >= DWELL_S:
                state = "ADVISORY"
            if state == "ADVISORY":
                if (below_crit_since is not None and now - below_crit_since >= DWELL_S) or (USE_FALL_RULE and fall >= FALL_C - 1e-9):
                    state, acknowledged = "CRITICAL", False
                elif above_exit_since is not None and now - above_exit_since >= DWELL_S:
                    state = "NORMAL"
            elif state == "CRITICAL" and acknowledged and smooth >= ADVISORY_C:
                state, acknowledged = "NORMAL", False
                below_adv_since = below_crit_since = None
                history = [(now, smooth)]

        if technical:
            shown = "TECHNICAL"
        elif state == "NORMAL" and not cur_valid:
            shown = "NO_READING"                    # never show green without a current valid reading
        else:
            shown = state
        if shown != last_status:
            print(">>> " + shown + ((" - " + reason) if shown == "TECHNICAL" else ""))
            last_status = shown

        ring_name = {"TECHNICAL": "CYAN", "NO_READING": "WHITE", "NORMAL": "GREEN", "ADVISORY": "AMBER", "CRITICAL": "RED"}[shown]
        sound_name = "TECHNICAL" if technical else ("SILENT" if (state == "NORMAL" or acknowledged) else state)
        send_to_ui(json.dumps({
            "t": round(now, 2),
            "probe": None if temp is None else round(temp, 2),
            "probe_raw": None if (script_name or last_raw is None) else round(last_raw, 2),
            "cal": CAL_ON,
            "contact": round(temp, 2) if valid else None,
            "valid": valid,
            "smooth": None if smooth is None else round(smooth, 2),
            "state": "NO_READING" if shown == "NO_READING" else state,
            "technical": technical,
            "ack": acknowledged,
            "sec_below": round(sec_below, 1),
            "min_below_36": round(sec_below / 60, 2),
            "fall": round(fall, 2),
            "ring": ring_name,
            "sound": sound_name,
            "adv": ADVISORY_C, "crit": CRITICAL_C, "demo": DWELL_S != 300, "dwell": DWELL_S,
            "source": ("script:" + script_name) if script_name else "probe",
            "script_t": round(now - script_t0, 2) if script_name else None,
        }))

    # ---- outputs ----
    if technical:
        update_ring(CYAN, now, TECH_FLASH_HZ)
        update_sound("TECHNICAL", now, False)
    else:
        if state == "NORMAL" and not cur_valid:
            update_ring(WHITE, now)
        else:
            update_ring({"NORMAL": GREEN, "ADVISORY": AMBER, "CRITICAL": RED}[state], now,
                        CRITICAL_FLASH_HZ if state == "CRITICAL" else 0)
        update_sound(state, now, acknowledged)

    time.sleep(0.01)
