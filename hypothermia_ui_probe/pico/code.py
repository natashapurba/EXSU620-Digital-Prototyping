# PROBE TRIAL - the waterproof DS18B20 probe is the ONLY temperature input.
# Bands: above 19.0 C = NORMAL (green), 17.0 to 19.0 C = ADVISORY (amber), below 17.0 C = CRITICAL (red, fast flashing).
# Same ring, speaker, button and optional voice files as the main monitor. The MCP9808 is not used here.
# UI version: also sends each reading to the laptop dashboard (hypothermia_ui/server/app.py) through a second
# USB port, and accepts ACK from the dashboard's Acknowledge button. Needs boot.py on CIRCUITPY.
import json, time, board, digitalio, neopixel, pwmio, audiocore, audiopwmio
import usb_cdc   # UI: second USB serial port for the dashboard (enabled by boot.py)
try:
    from adafruit_onewire.bus import OneWireBus
    import adafruit_ds18x20
    PROBE_LIBS = True
except ImportError:
    PROBE_LIBS = False

# ===================== SETTINGS =====================
NORMAL_ABOVE_C  = 19.0    # above this = NORMAL (green)
CRITICAL_BELOW_C = 17.0   # below this = CRITICAL (red); in between = ADVISORY (amber)
CRITICAL_FLASH_HZ = 2.5   # red flashes this many times per second (kept under 3 for photosensitivity)
TECH_FLASH_HZ     = 1.0   # cyan flashes for a technical fault
HOLD_S          = 3       # a new band must hold this long before the colour changes (0 = instant)
SMOOTH_N        = 3       # average of the last 3 readings (~3 s)
RANGE_MIN_C, RANGE_MAX_C = 0.0, 50.0   # outside this = probe problem (Technical)
SAMPLE_PERIOD_S = 1.0
FAULT_S         = 3       # Technical alarm if the probe is bad for this long
SEARCH_EVERY_S  = 5       # look for a missing probe again this often

# ---- pins ----
RING_PIN, NUM_PIXELS = board.GP16, 16
SPEAKER_PIN = board.GP20
BUTTON_PIN  = board.GP21
PROBE_PIN   = board.GP17   # adapter DAT -> GP17, VCC -> 3V3, GND -> GND

GREEN, AMBER, RED, CYAN, OFF = (0, 255, 0), (255, 110, 0), (255, 0, 0), (0, 200, 200), (0, 0, 0)

# ===================== HARDWARE =====================
ring = neopixel.NeoPixel(RING_PIN, NUM_PIXELS, brightness=0.15, auto_write=False)
speaker = pwmio.PWMOut(SPEAKER_PIN, frequency=440, duty_cycle=0, variable_frequency=True)
button = digitalio.DigitalInOut(BUTTON_PIN)
button.switch_to_input(pull=digitalio.Pull.UP)   # pressed = False

# ===================== PROBE =====================
probe_bus = None
probe = None
probe_ready_at = None

def find_probe():
    global probe_bus, probe
    probe = None
    if not PROBE_LIBS:
        return
    try:
        if probe_bus is None:
            probe_bus = OneWireBus(PROBE_PIN)
        for device in probe_bus.scan():
            if device.family_code == 0x28:              # 0x28 = DS18B20
                probe = adafruit_ds18x20.DS18X20(probe_bus, device)
                probe.resolution = 12
                print(">>> Probe found on GP17")
                return
    except Exception as err:
        print(">>> Probe not answering (%s)" % err)
    print(">>> Probe not found - check DAT->GP17, VCC->3V3, GND->GND and the screw terminals")

def read_probe(now):
    """Returns (temperature or None, reason). Starts a reading and collects it next time, so nothing pauses."""
    global probe, probe_ready_at
    if probe is None:
        return None, "probe not found - check its wires"
    try:
        if probe_ready_at is None:
            probe_ready_at = now + probe.start_temperature_read()
            return None, "first reading in progress"
        if now < probe_ready_at:
            return None, "reading in progress"
        value = probe.read_temperature()
        probe_ready_at = now + probe.start_temperature_read()
        if value == 85.0:                               # chip's start-up value, not a real reading
            return None, "probe starting up"
        return value, ""
    except Exception:
        probe, probe_ready_at = None, None
        return None, "probe stopped answering - check its wires"

# ===================== SOUND: beeps =====================
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
        speaker.deinit()
        audio = audiopwmio.PWMAudioOut(SPEAKER_PIN)
        audio.play(audiocore.WaveFile(wav_file))
    except (OSError, ValueError):
        if wav_file is not None:
            wav_file.close()
        wav_file = None
        if audio is None:
            try:
                speaker = pwmio.PWMOut(SPEAKER_PIN, frequency=440, duty_cycle=0, variable_frequency=True)
            except ValueError:
                pass

def voice_busy():
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

def update_ring(colour, now, flash_hz=0):
    """flash_hz = 0 for a steady light, otherwise on/off this many times per second."""
    if flash_hz and int(now * flash_hz * 2) % 2:
        colour = OFF
    ring.fill(colour)
    ring.show()

# ===================== UI LINK =====================
# Thonny keeps the normal console port; the dashboard uses usb_cdc.data.
# Without boot.py, usb_cdc.data is None and the code runs exactly as before.
link = usb_cdc.data
def send_to_ui(text):
    print(text)                                  # still shows in Thonny
    if link is not None:
        try:
            link.write((text + "\n").encode())
        except Exception:
            pass

rx_buf = b""
def read_ui_commands():
    """True if the dashboard sent ACK."""
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
state = "NORMAL"
pending, pending_since = None, None
acknowledged = False
was_pressed = False
ack_armed = False           # a NEW press is needed, so a stuck button can't silence an alarm
bad_since = None
technical = False
recent = []
temp = smooth = None
reason = ""
last_status = None
shown_ack = False
seconds_below_normal = 0.0

MESSAGES = {
    "NORMAL":   "OK - NORMAL: above %.1f C (green)" % NORMAL_ABOVE_C,
    "ADVISORY": "*** ALERT - ADVISORY: between %.1f and %.1f C (amber)" % (CRITICAL_BELOW_C, NORMAL_ABOVE_C),
    "CRITICAL": "!!! ALERT - CRITICAL: below %.1f C (red). Press the button to acknowledge." % CRITICAL_BELOW_C,
}

print("PROBE TRIAL - normal above %.1f C, advisory %.1f to %.1f C, critical below %.1f C"
      % (NORMAL_ABOVE_C, CRITICAL_BELOW_C, NORMAL_ABOVE_C, CRITICAL_BELOW_C))
if not PROBE_LIBS:
    print(">>> Libraries missing: copy adafruit_onewire (folder) and adafruit_ds18x20.py into CIRCUITPY/lib")
find_probe()
last_search = time.monotonic()

next_sample = time.monotonic()
while True:
    now = time.monotonic()

    # ---- acknowledge button: only a new press during Critical counts ----
    pressed = not button.value
    if state == "CRITICAL":
        if not pressed:
            ack_armed = True
        elif ack_armed and not was_pressed:
            acknowledged = True
    else:
        ack_armed = False
        acknowledged = False
    was_pressed = pressed
    if read_ui_commands() and state == "CRITICAL":   # dashboard Acknowledge = bedside button
        acknowledged = True

    if now >= next_sample:
        next_sample += SAMPLE_PERIOD_S

        if probe is None and PROBE_LIBS and now - last_search >= SEARCH_EVERY_S:
            find_probe()
            last_search = now

        # ---- 1. read the probe ----
        temp, reason = read_probe(now)
        starting = reason in ("first reading in progress", "reading in progress", "probe starting up")
        valid = temp is not None and RANGE_MIN_C <= temp <= RANGE_MAX_C
        if temp is not None and not valid:
            reason = "reading %.1f C is outside %.0f-%.0f C" % (temp, RANGE_MIN_C, RANGE_MAX_C)

        # ---- 2. Technical fault: probe bad for FAULT_S seconds (start-up readings do not count) ----
        if valid or starting:
            bad_since = None
        else:
            bad_since = bad_since or now
        technical = bad_since is not None and now - bad_since >= FAULT_S

        if valid:
            # ---- 3. smoothing ----
            recent.append(temp)
            if len(recent) > SMOOTH_N:
                recent.pop(0)
            smooth = sum(recent) / len(recent)
            if smooth <= NORMAL_ABOVE_C:
                seconds_below_normal += SAMPLE_PERIOD_S

            # ---- 4. bands ----
            if smooth > NORMAL_ABOVE_C:
                target = "NORMAL"
            elif smooth >= CRITICAL_BELOW_C:
                target = "ADVISORY"
            else:
                target = "CRITICAL"
            if target == state:
                pending, pending_since = None, None
            elif target != pending:
                pending, pending_since = target, now
            if pending is not None and now - pending_since >= HOLD_S:
                state, pending, pending_since = pending, None, None

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
            print(">>> Critical acknowledged - sound off, ring stays red until the probe warms up")
        shown_ack = acknowledged

        send_to_ui(json.dumps({
            "t": round(now, 1),
            "probe": None if temp is None else round(temp, 2),
            # the dashboard plots its main channel from "contact"; in this trial that channel is the probe
            "contact": None if temp is None else round(temp, 2),
            "smooth": None if smooth is None else round(smooth, 2),
            "state": state,
            "technical": technical,
            "ack": acknowledged,
            "min_at_or_below_19": round(seconds_below_normal / 60, 2),
            "min_below_36": round(seconds_below_normal / 60, 2),     # dashboard name for the same counter
            "fall": 0.0,
            "ring": "CYAN" if technical else {"NORMAL": "GREEN", "ADVISORY": "AMBER", "CRITICAL": "RED"}[state],
            "sound": "TECHNICAL" if technical else ("SILENT" if (state == "NORMAL" or acknowledged) else state),
            "adv": NORMAL_ABOVE_C, "crit": CRITICAL_BELOW_C, "demo": True,
        }))

    # ---- 5. ring and speaker: Technical shows on top ----
    talking = voice_busy()
    if technical:
        update_ring(CYAN, now, TECH_FLASH_HZ)
        if not talking:
            update_sound("TECHNICAL", now, False)
    else:
        colour = {"NORMAL": GREEN, "ADVISORY": AMBER, "CRITICAL": RED}[state]
        update_ring(colour, now, CRITICAL_FLASH_HZ if state == "CRITICAL" else 0)
        if not talking:
            update_sound(state, now, acknowledged)

    time.sleep(0.01)
