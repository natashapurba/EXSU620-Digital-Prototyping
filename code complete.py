# Hypothermia monitor - contact sensor, NeoPixel ring, speaker (via PAM8302 amplifier),
# acknowledge button, written alerts and optional spoken alerts.
# Alarm rules follow Figure 3 of the project card (EXSU620 Project 05).
# Version 3: everything in one file; never crashes if the sensor is missing or unplugged.
import json, time, board, busio, digitalio, neopixel, pwmio, audiocore, audiopwmio, adafruit_mcp9808

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
else:  # DEMO: simple temperature bands for bench demonstrations
    # green = 26 C or above, amber = 25 to 26 C, red = below 25 C, rising = green
    ADVISORY_C, ADV_EXIT_C, CRITICAL_C = 26.0, 26.0, 25.0
    DWELL_S, FALL_C, FALL_WIN_S = 5, 0.5, 90      # a band must hold 5 s before the colour changes
    RANGE_MIN_C, RANGE_MAX_C = 14.0, 35.0
    RISE_C, RISE_WIN_S = 0.1, 5   # "increasing" = smoothed temp up by >= 0.1 C over the last 5 s

SMOOTH_N        = 5 if DEMO else 10   # moving average: 5 readings (~5 s) in DEMO, 10 (~10 s) otherwise
SAMPLE_PERIOD_S = 1.0     # ER-3: at least 1 reading per second
FAULT_S         = 3       # Technical alarm if sensor bad for this long (ER-7: within 5 s)
SEARCH_EVERY_S  = 5       # if the sensor is missing, look for it again this often

# ---- pins (see the wiring pictures) ----
RING_PIN, NUM_PIXELS = board.GP16, 16   # ring Data Input; ring power from the SPI0 3V3 + GND pins
SPEAKER_PIN = board.GP20                 # amplifier A+ (A- and GND to GND, Vin to VSYS)
BUTTON_PIN  = board.GP21                 # acknowledge button: one leg to GP21, other leg to GND
MCP_ADDRESS = 0x18
# (SDA, SCL) pairs searched for the contact sensor. Ring, speaker and button pins are left out.
I2C_PAIRS = [(4, 5), (8, 9), (0, 1), (2, 3), (6, 7), (10, 11), (12, 13), (14, 15), (26, 27)]

GREEN, AMBER, RED, CYAN, OFF = (0, 255, 0), (255, 110, 0), (255, 0, 0), (0, 200, 200), (0, 0, 0)

# ===================== HARDWARE =====================
ring = neopixel.NeoPixel(RING_PIN, NUM_PIXELS, brightness=0.15, auto_write=False)
speaker = pwmio.PWMOut(SPEAKER_PIN, frequency=440, duty_cycle=0, variable_frequency=True)
button = digitalio.DigitalInOut(BUTTON_PIN)
button.switch_to_input(pull=digitalio.Pull.UP)   # pressed = False

sensor = None
sensor_bus = None

def find_sensor():
    """Try each pin pair; keep the MCP9808 where it answers. Never crashes."""
    global sensor, sensor_bus
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
            try:
                sensor, sensor_bus = adafruit_mcp9808.MCP9808(bus), bus
                print(">>> Contact sensor found on SDA=GP%d SCL=GP%d" % (sda_n, scl_n))
                return
            except (ValueError, OSError):
                pass
        bus.deinit()
    sensor, sensor_bus = None, None
    print(">>> Contact sensor not found - check Vdd->3V3, Gnd->GND, SDA->GP4, SCL->GP5")

def drop_sensor():
    """Forget a sensor that stopped answering, so the search starts again."""
    global sensor, sensor_bus
    if sensor_bus is not None:
        sensor_bus.deinit()
    sensor, sensor_bus = None, None

# ===================== SOUND: beeps (never pause the program) =====================
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

# ===================== SOUND: spoken alerts (optional WAV files on CIRCUITPY) =====================
# A missing file is simply skipped, so the monitor works with or without them.
VOICE = {"NORMAL": "normal.wav", "ADVISORY": "advisory.wav",
         "CRITICAL": "critical.wav", "TECHNICAL": "fault.wav"}
audio = None
wav_file = None

def say(kind):
    global speaker, audio, wav_file
    if audio is not None or kind not in VOICE:
        return
    try:
        wav_file = open(VOICE[kind], "rb")
        speaker.deinit()                            # lend the pin to the voice player
        audio = audiopwmio.PWMAudioOut(SPEAKER_PIN)
        audio.play(audiocore.WaveFile(wav_file))    # plays in the background
    except (OSError, ValueError):
        if wav_file is not None:
            wav_file.close()
        wav_file = None
        if audio is None:                           # make sure the beeper is back
            try:
                speaker = pwmio.PWMOut(SPEAKER_PIN, frequency=440, duty_cycle=0, variable_frequency=True)
            except ValueError:
                pass

def voice_busy():
    """True while a clip plays; when it ends, give the pin back to the beeper."""
    global speaker, audio, wav_file
    if audio is None:
        return False
    if audio.playing:
        return True
    audio.deinit()
    wav_file.close()
    audio, wav_file = None, None
    speaker = pwmio.PWMOut(SPEAKER_PIN, frequency=440, duty_cycle=0, variable_frequency=True)
    return False

# ===================== RING =====================
def update_ring(colour, now, blink):
    if blink and int(now * 2) % 2:
        colour = OFF
    ring.fill(colour)
    ring.show()

# ===================== STATE =====================
state = "NORMAL"            # NORMAL / ADVISORY / CRITICAL
acknowledged = False
below_adv_since = None
above_exit_since = None
below_crit_since = None
bad_since = None
seconds_below_36 = 0.0      # CR-3 counter (in DEMO mode: seconds below 25.0 C)
recent = []
history = []
temp = smooth = None
fall = 0.0
technical = False
was_pressed = False
ack_armed = False           # a NEW press is needed, so a stuck button can't silence an alarm
last_status = None
shown_ack = False
reason = ""
pending = None              # DEMO: colour waiting for its 5 s hold
pending_since = None

MESSAGES = {
    "NORMAL":   "OK - NORMAL: temperature in range (green)",
    "ADVISORY": "*** ALERT - ADVISORY: below %.1f C for %d s - start active warming (amber)" % (ADVISORY_C, DWELL_S),
    "CRITICAL": "!!! ALERT - CRITICAL: below %.1f C or falling fast - escalate warming (red). Press the button to acknowledge." % CRITICAL_C,
}

if DEMO:
    MESSAGES = {
        "NORMAL":   "OK - NORMAL: %.0f C or above, or warming up (green)" % ADVISORY_C,
        "ADVISORY": "*** ALERT - ADVISORY: between %.0f and %.0f C (amber)" % (CRITICAL_C, ADVISORY_C),
        "CRITICAL": "!!! ALERT - CRITICAL: below %.0f C (red). Press the button to acknowledge." % CRITICAL_C,
    }

print("Hypothermia monitor - %s values: advisory %.1f, critical %.1f, dwell %d s"
      % ("DEMO" if DEMO else "GUIDELINE", ADVISORY_C, CRITICAL_C, DWELL_S))
find_sensor()
last_search = time.monotonic()

next_sample = time.monotonic()
while True:
    now = time.monotonic()

    # ---- acknowledge button ----
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

        # ---- 0. look for the sensor if it is missing ----
        if sensor is None and now - last_search >= SEARCH_EVERY_S:
            find_sensor()
            last_search = now

        # ---- 1. read the sensor ----
        if sensor is None:
            temp, valid = None, False
            reason = "contact sensor not found - check its 4 wires"
        else:
            try:
                temp = sensor.temperature
                valid = RANGE_MIN_C <= temp <= RANGE_MAX_C
                if not valid:
                    reason = "reading %.1f C is outside %.0f-%.0f C - is the probe on the patient?" % (temp, RANGE_MIN_C, RANGE_MAX_C)
            except (OSError, RuntimeError):
                temp, valid = None, False
                reason = "contact sensor stopped answering - check its wires"

        # ---- 2. Technical fault: lost or out of range for FAULT_S seconds ----
        if valid:
            bad_since = None
        else:
            bad_since = bad_since or now
        technical = bad_since is not None and now - bad_since >= FAULT_S
        if technical and temp is None and sensor is not None:
            drop_sensor()               # lost mid-run: start searching for it again
            last_search = now - SEARCH_EVERY_S

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

            # ---- 4. state machine ----
            if DEMO:
                # value from about RISE_WIN_S seconds ago, to see whether it is going up
                older = [h[1] for h in history if now - h[0] >= RISE_WIN_S]
                rising = bool(older) and smooth - older[-1] >= RISE_C
                if rising or smooth >= ADVISORY_C:
                    target = "NORMAL"
                elif smooth >= CRITICAL_C:
                    target = "ADVISORY"
                else:
                    target = "CRITICAL"
                if rising:                          # warming up: green straight away
                    state, pending, pending_since = "NORMAL", None, None
                elif target == state:
                    pending, pending_since = None, None
                elif target != pending:
                    pending, pending_since = target, now
                elif now - pending_since >= DWELL_S:
                    state, pending, pending_since = target, None, None
                if state != "CRITICAL":
                    acknowledged = False
            else:   # guideline rules, Figure 3
                if state == "NORMAL":
                    if below_adv_since and now - below_adv_since >= DWELL_S:
                        state = "ADVISORY"
                if state == "ADVISORY":
                    if (below_crit_since and now - below_crit_since >= DWELL_S) or fall >= FALL_C:
                        state, acknowledged = "CRITICAL", False
                    elif above_exit_since and now - above_exit_since >= DWELL_S:
                        state = "NORMAL"
                elif state == "CRITICAL":
                    if acknowledged and smooth >= ADVISORY_C:   # latched until ack AND back in range
                        state, acknowledged = "NORMAL", False

        # ---- written (and spoken) message whenever the status changes ----
        status = "TECHNICAL" if technical else state
        if status != last_status:
            if status == "TECHNICAL":
                print(">>> ALERT - TECHNICAL FAULT: " + reason + " (cyan)")
            else:
                print(">>> " + MESSAGES[state])
            if last_status is not None:
                say(status)
            last_status = status
        if acknowledged and state == "CRITICAL" and not shown_ack:
            print(">>> Critical acknowledged - sound off" + ("" if DEMO else ", stays red until back at %.1f C" % ADVISORY_C))
        shown_ack = acknowledged

        print(json.dumps({
            "t": round(now, 1),
            "contact": None if temp is None else round(temp, 2),
            "smooth": None if smooth is None else round(smooth, 2),
            "state": state,
            "technical": technical,
            "ack": acknowledged,
            "min_below_36": round(seconds_below_36 / 60, 2),
            "fall": round(fall, 2),
        }))

    # ---- 5. ring and speaker: Technical shows on top of the clinical state ----
    talking = voice_busy()
    if technical:
        update_ring(CYAN, now, True)
        if not talking:
            update_sound("TECHNICAL", now, False)
    else:
        colour = {"NORMAL": GREEN, "ADVISORY": AMBER, "CRITICAL": RED}[state]
        update_ring(colour, now, state == "CRITICAL")
        if not talking:
            update_sound(state, now, acknowledged)

    time.sleep(0.01)