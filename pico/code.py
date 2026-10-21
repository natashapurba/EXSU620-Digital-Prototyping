# Hypothermia monitor - contact channel, ring, speaker, acknowledge button
# Alarm rules follow Figure 3 of the project card (EXSU620 Project 05).
# Version 2: finds the MCP9808 on its own, button needs a fresh press, DEMO range fits the heater.
import json, time, board, busio, digitalio, neopixel, pwmio, adafruit_mcp9808
import usb_cdc   # UI ADDITION: second USB serial port for the laptop dashboard (needs boot.py)

# ===================== SETTINGS =====================
DEMO = True           # True = shifted values for desk/heater testing, False = real guideline values

if not DEMO:
    ADVISORY_C   = 36.0   # CR-2: below 36.0 C ...
    ADV_EXIT_C   = 36.2   # Fig 3: back to Normal when above 36.2 C ...
    CRITICAL_C   = 35.5   # CR-2: below 35.5 C ...
    DWELL_S      = 300    # ... for 5 minutes
    FALL_C       = 0.5    # Fig 3: or falling 0.5 C ...
    FALL_WIN_S   = 900    # ... within 15 minutes
    RANGE_MIN_C  = 25.0   # ER-1 range: outside this = sensor out of range (Technical)
    RANGE_MAX_C  = 42.0
else:  # same logic, shifted ~11 C down and sped up, so it works at room temperature
    ADVISORY_C, ADV_EXIT_C, CRITICAL_C = 25.0, 25.2, 24.5
    DWELL_S, FALL_C, FALL_WIN_S = 30, 0.5, 90
    RANGE_MIN_C, RANGE_MAX_C = 14.0, 35.0   # max raised from 31 so the 20-34 C heater does not trip Technical

SMOOTH_N        = 10      # moving average over the last 10 readings (~10 s)
SAMPLE_PERIOD_S = 1.0     # ER-3: at least 1 reading per second
FAULT_S         = 3       # Technical alarm if sensor bad for this long (ER-7: within 5 s)
SEARCH_EVERY_S  = 5       # if the sensor is not found, look again this often

RING_PIN, NUM_PIXELS = board.GP16, 16
SPEAKER_PIN = board.GP18
BUTTON_PIN  = board.GP20  # acknowledge button: one leg to GP20, other leg to GND
MCP_ADDRESS = 0x18
# I2C pin pairs (SDA, SCL) to search for the MCP9808. Pins used by ring, speaker and button are left out.
I2C_PAIRS = [(4, 5), (8, 9), (0, 1), (2, 3), (6, 7), (10, 11), (12, 13), (14, 15), (26, 27)]

GREEN, AMBER, RED, CYAN, OFF = (0, 255, 0), (255, 110, 0), (255, 0, 0), (0, 200, 200), (0, 0, 0)

# ===================== HARDWARE =====================
ring = neopixel.NeoPixel(RING_PIN, NUM_PIXELS, brightness=0.15, auto_write=False)
speaker = pwmio.PWMOut(SPEAKER_PIN, frequency=440, duty_cycle=0, variable_frequency=True)
button = digitalio.DigitalInOut(BUTTON_PIN)
button.switch_to_input(pull=digitalio.Pull.UP)   # pressed = False


def find_sensor():
    """Try each pin pair and return the MCP9808 on the pair where it answers, or None."""
    for sda_n, scl_n in I2C_PAIRS:
        try:
            bus = busio.I2C(getattr(board, "GP%d" % scl_n), getattr(board, "GP%d" % sda_n), frequency=100_000)
        except (RuntimeError, ValueError):
            continue                                  # nothing with pull-ups on these pins
        while not bus.try_lock():
            pass
        addresses = bus.scan()
        bus.unlock()
        if MCP_ADDRESS in addresses:
            print("MCP9808 found on SDA=GP%d SCL=GP%d" % (sda_n, scl_n))
            return adafruit_mcp9808.MCP9808(bus)
        bus.deinit()
    print("MCP9808 not found - check Vdd to 3V3, Gnd to GND, SDA and SCL wires")
    return None


sensor = find_sensor()
last_search = time.monotonic()

# ===================== SOUND (does not pause the program) =====================
# Fig 3: Advisory = 3 pulses, Critical = 10 pulses, Technical = chirp
PATTERNS = {               # (frequency Hz, number of pulses, repeat every N seconds)
    "ADVISORY":  (660, 3, 30),
    "CRITICAL":  (1000, 10, 10),
    "TECHNICAL": (1500, 1, 5),
}
PULSE_ON, PULSE_OFF = 0.12, 0.08
burst_start = None
burst_kind = None

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
    pulse_no = int(t / (PULSE_ON + PULSE_OFF))
    on = pulse_no < pulses and (t % (PULSE_ON + PULSE_OFF)) < PULSE_ON
    speaker.duty_cycle = 32768 if on else 0

def update_ring(colour, now, blink):
    if blink and int(now * 2) % 2:
        colour = OFF
    ring.fill(colour)
    ring.show()

# ===================== UI LINK (added) =====================
# Thonny keeps the normal console port, so the dashboard uses the separate usb_cdc.data port.
# If boot.py has not enabled it, usb_cdc.data is None and the code runs exactly as before.
link = usb_cdc.data
def send_to_ui(text):
    print(text)                              # still shows in Thonny
    if link is not None:
        try:
            link.write((text + "\n").encode())
        except Exception:
            pass

rx_buf = b""
def read_ui_commands():
    """Returns True if the dashboard sent ACK."""
    global rx_buf
    if link is None or link.in_waiting == 0:
        return False
    rx_buf += link.read(link.in_waiting)
    got = False
    while b"\n" in rx_buf:
        line, rx_buf = rx_buf.split(b"\n", 1)
        if line.strip() == b"ACK":
            got = True
    return got

# ===================== STATE =====================
state = "NORMAL"            # NORMAL / ADVISORY / CRITICAL
acknowledged = False
below_adv_since = None      # when smoothed temp went below the advisory threshold
above_exit_since = None     # when smoothed temp went above the exit threshold
below_crit_since = None     # when smoothed temp went below the critical threshold
bad_since = None            # when the sensor started failing
seconds_below_36 = 0.0      # CR-3 counter (in DEMO mode: seconds below 25.0 C)
recent = []                 # last SMOOTH_N raw readings
history = []                # (time, smoothed) for the fall rule
temp = smooth = None
fall = 0.0
technical = False
was_pressed = False         # button state on the previous loop
ack_armed = False           # True once the button has been seen released during this Critical

print("Hypothermia monitor - %s values: advisory %.1f, critical %.1f, dwell %d s"
      % ("DEMO" if DEMO else "GUIDELINE", ADVISORY_C, CRITICAL_C, DWELL_S))

next_sample = time.monotonic()
while True:
    now = time.monotonic()

    # acknowledge button: only a NEW press during Critical counts, so a stuck or shorted
    # button can never silence an alarm by itself
    pressed = not button.value
    if state == "CRITICAL":
        if not pressed:
            ack_armed = True
        elif ack_armed and not was_pressed:
            acknowledged = True
    else:
        ack_armed = False
    was_pressed = pressed
    if state == "CRITICAL" and read_ui_commands():   # UI ADDITION: on-screen Acknowledge = bedside button
        acknowledged = True

    if now >= next_sample:
        next_sample += SAMPLE_PERIOD_S

        # ---- 0. look for the sensor again if it was not found ----
        if sensor is None and now - last_search >= SEARCH_EVERY_S:
            sensor = find_sensor()
            last_search = now

        # ---- 1. read the sensor ----
        try:
            temp = sensor.temperature if sensor else None
            valid = temp is not None and RANGE_MIN_C <= temp <= RANGE_MAX_C
        except OSError:
            temp, valid = None, False

        # ---- 2. Technical fault: lost or out of range for FAULT_S seconds ----
        if valid:
            bad_since = None
        else:
            bad_since = bad_since or now
        technical = bad_since is not None and now - bad_since >= FAULT_S

        if valid:
            # ---- 3. smoothing and counters ----
            recent.append(temp)
            if len(recent) > SMOOTH_N:
                recent.pop(0)
            smooth = sum(recent) / len(recent)

            if smooth < ADVISORY_C:
                seconds_below_36 += SAMPLE_PERIOD_S
                below_adv_since = below_adv_since or now
            else:
                below_adv_since = None
            above_exit_since = (above_exit_since or now) if smooth > ADV_EXIT_C else None
            below_crit_since = (below_crit_since or now) if smooth < CRITICAL_C else None

            history.append((now, smooth))
            while now - history[0][0] > FALL_WIN_S:
                history.pop(0)
            fall = max(h[1] for h in history) - smooth

            # ---- 4. state machine (Figure 3) ----
            if state == "NORMAL":
                if below_adv_since and now - below_adv_since >= DWELL_S:
                    state = "ADVISORY"
            if state == "ADVISORY":
                if (below_crit_since and now - below_crit_since >= DWELL_S) or fall >= FALL_C:
                    state, acknowledged = "CRITICAL", False
                elif above_exit_since and now - above_exit_since >= DWELL_S:
                    state = "NORMAL"
            elif state == "CRITICAL":
                # latched: leaves only when acknowledged AND back in range
                if acknowledged and smooth >= ADVISORY_C:
                    state, acknowledged = "NORMAL", False

        send_to_ui(json.dumps({
            "t": round(now, 1),
            "contact": None if temp is None else round(temp, 2),
            "smooth": None if smooth is None else round(smooth, 2),
            "state": state,
            "technical": technical,
            "ack": acknowledged,
            "min_below_36": round(seconds_below_36 / 60, 2),
            "fall": round(fall, 2),
            # UI ADDITIONS: what the outputs are doing, and the thresholds in force
            "ring": "CYAN" if technical else {"NORMAL": "GREEN", "ADVISORY": "AMBER", "CRITICAL": "RED"}[state],
            "sound": "TECHNICAL" if technical else ("SILENT" if (state == "NORMAL" or acknowledged) else state),
            "adv": ADVISORY_C, "crit": CRITICAL_C, "demo": DEMO,
        }))

    # ---- 5. outputs: Technical shows on top of the clinical state ----
    if technical:
        update_ring(CYAN, now, True)
        update_sound("TECHNICAL", now, False)
    else:
        colour = {"NORMAL": GREEN, "ADVISORY": AMBER, "CRITICAL": RED}[state]
        update_ring(colour, now, state == "CRITICAL")
        update_sound(state, now, acknowledged)

    time.sleep(0.01)
