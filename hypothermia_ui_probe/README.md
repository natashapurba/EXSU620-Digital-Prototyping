# Perioperative Hypothermia Monitor (EXSU 620, Project 05)

A bench prototype of a temperature monitor for patients during surgery. A waterproof DS18B20 probe measures temperature. A Raspberry Pi Pico 2 W sorts each reading into an alarm state and shows it on a NeoPixel ring, a speaker and an acknowledge button. A Flask dashboard on the laptop shows the same data live.

> **The main project folder is `hypothermia_ui_probe/`.**
> It contains the current Pico code and the dashboard. Other folders or older files in this repository (MCP9808 versions, infrared tests, speaker tests, probe-only trials) are earlier development steps, kept for reference only. Do not load them onto the board.

---

## 1. Folder structure

```
hypothermia_ui_probe/          <- MAIN FOLDER
├── pico/
│   ├── boot.py                <- turns on the second USB port (data port) for the dashboard
│   └── code.py                <- the monitor program (probe, ring, speaker, button)
├── .gitignore             <- keeps secrets and the database off GitHub
├── server/
│   ├── app.py                 <- Flask dashboard, reads the Pico and serves the web page
│   ├── static/index.html      <- the dashboard page
│   └── whatsapp_settings.example.txt  <- template for the Twilio WhatsApp settings
├── check_ports.py             <- tool that finds which USB port is sending data
└── README.md
```

---

## 2. Alarm logic (Figure 3 of the project card, clinical thresholds, 30 s bench time scale)

All dwell times are 30 s on the bench (`DWELL_S` in `pico/code.py`); the clinical value is 300 s.


| Rule | Result | Ring | Sound |
|---|---|---|---|
| Probe above 36.0 °C (and no alarm active) | NORMAL | Green, steady | Silent |
| Below 36.0 °C continuously for the dwell | ADVISORY | Amber, steady | 3 beeps every 30 s |
| In Advisory: below 35.5 °C for the dwell, or a fall of 0.5 °C within 15 min | CRITICAL (latched) | Red, flashing 2.5 Hz | 10 beeps every 10 s, plus one phone alert |
| In Advisory: above 36.2 °C for the dwell | back to NORMAL | Green | Silent |
| In Critical: acknowledged AND back at or above 36.0 °C | back to NORMAL | Green | Silent |
| No valid reading for 3 s, or reading outside 25 to 42 °C | TECHNICAL | Cyan, flashing | 1 chirp every 5 s |
| No valid reading yet (start-up, the first seconds of a fault) | NO READING | Dim white, never green | Silent |

- The alarm rules use a 3-sample moving average. The raw value is reported and used for the time-below-36.0 °C counter.
- Acknowledge (dashboard button) silences the Critical sound only. The ring stays red until the device is back in range.
- Pressing **Start case** resets the time-below counter, so the handover export covers that case only.
- **Scripted test mode:** the dashboard buttons ER-4, ER-5, ER-6 and ER-7 make the Pico follow a fixed temperature profile instead of the probe, so the 5-minute rules can be verified repeatably. See `verification/README.md`.

## 3. Hardware and wiring

Board: Raspberry Pi Pico 2 W on a Grove Shield, running CircuitPython 10.x.

| Part | Part pin | Pico pin |
|---|---|---|
| DS18B20 probe adapter | DAT | GP17 |
| | VCC | 3V3 |
| | GND | GND |
| NeoPixel ring (16 LEDs) | Data Input | GP16 |
| | Power | 3V3 |
| | GND | GND |
| PAM8302 amplifier | A+ | GP20 |
| | A− and GND | GND |
| | Vin | VSYS |
| | SD | leave empty |
| Speaker | 2 wires | amplifier output terminals (the two small middle holes) |
| Acknowledge button | one leg | GP21 |
| | other leg | GND |

Avoid the **3V3_EN** and **RUN** pins. A wire there switches the board off and CIRCUITPY disappears.

---

## 4. Setting up the Pico (one time)

1. Install CircuitPython 10.x on the Pico 2 W. The **CIRCUITPY** drive will appear.
2. From the CircuitPython library bundle, copy these into `CIRCUITPY/lib`:
   - `neopixel.mpy`
   - the `adafruit_onewire` folder
   - `adafruit_ds18x20.mpy`
3. Copy `hypothermia_ui_probe/pico/boot.py` and `hypothermia_ui_probe/pico/code.py` to the top level of CIRCUITPY. Replace any existing files, and delete any copies such as `code 2.py`.
4. **Unplug the USB and plug it back in.** This is required, because `boot.py` only runs at power-up.
5. The ring should light green within a few seconds.

To check the board in Thonny, the first line in the Shell should be:
```
MONITOR v3.1 - advisory below 36.0 C for 30 s, critical below 35.5 C for 30 s or falling 0.5 C in 15 min
```

---

## 5. Setting up the laptop (one time)

Requires Python 3.9 or newer.

```bash
pip3 install flask pyserial
```

Put the `check_ports.py` file in the `hypothermia_ui_probe` folder if it is not there already.

---

## 6. Running the dashboard

1. Plug in the Pico and wait about 5 seconds. **Close Thonny.**
2. Open Terminal and go to the main folder:
   ```bash
   cd ~/Downloads/hypothermia_ui_probe
   ```
3. Find the data port:
   ```bash
   python3 check_ports.py
   ```
   Two ports are listed. Use the **second (higher-numbered)** one, for example `/dev/cu.usbmodem11203`.
4. Start the dashboard with that port:
   ```bash
   python3 server/app.py --port /dev/cu.usbmodem11203
   ```
5. Open **http://127.0.0.1:5050** in a browser.

To try the dashboard with no hardware, run a scripted demo:
```bash
python3 server/app.py --sim
```

### Phone alerts (optional)

The dashboard sends **one phone alert each time the monitor enters CRITICAL**, while a case is running. The default is **iMessage from the Mac** running the dashboard (free, uses the Messages app). Twilio WhatsApp is also supported but needs a paid account for custom text.

1. Copy `server/whatsapp_settings.example.txt`, rename the copy to `whatsapp_settings.txt`, and put your number after `IMESSAGE_TO`.
2. Make sure the Messages app on the Mac is signed in to iMessage.
3. Test it:
   ```bash
   python3 server/app.py --test-whatsapp
   ```
   The first time, macOS asks to let Terminal control Messages. Click **OK**.
4. Start the dashboard as usual and press **Start case**.

**Never upload `whatsapp_settings.txt` to GitHub.** The `.gitignore` file already excludes it.

### Stopping

- Dashboard: press **Control+C** in its Terminal window.
- Pico: unplug the USB.

---

## 7. Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| Dashboard says "Waiting for device" | Wrong port, the program is not running, or old code is on the board | Run `python3 check_ports.py`. Use the port that shows `"probe"` in its output. Unplug and replug the Pico. |
| Only one port is listed | `boot.py` is missing or was not activated | Copy `boot.py` to CIRCUITPY, then unplug and replug |
| `device reports readiness to read but returned no data` | Two programs are using the same port | Run `lsof /dev/cu.usbmodem*`, then `kill <PID>`. Never run Thonny and the dashboard at the same time. |
| "Access denied" at port 5000 | macOS AirPlay Receiver uses port 5000 | Use port 5050 (the default) |
| JSON shows `"contact"` but no `"probe"` | Old MCP9808 code is on the board | Copy `pico/code.py` from the main folder again |
| Ring cyan, "probe not found" | Probe wiring or screw terminals | Check DAT to GP17, VCC to 3V3, GND to GND |
| Reading of 85.0 °C | The probe's power-on value | Ignored automatically. Wait a second. |
| No sound | Speaker in the corner mounting holes, or SD wired | Use the two small middle output holes and leave SD empty |
| `SyntaxError` in `<stdin>` | Code was pasted into the Thonny Shell | Save it as `code.py` on CIRCUITPY instead |
| CIRCUITPY disappears | Wire in 3V3_EN or RUN, or a short between 3V3 and GND | Unplug the board and check the wires |

---

## 8. Known limitations

- The probe measures a surface or water temperature, not core body temperature. A clinical device would need a validated core site (oesophageal, bladder or nasopharyngeal) and a correction strategy.
- The thresholds are for bench testing only.
- The prototype has not been tested against IEC 60601-1-8 alarm requirements. That standard was used as design guidance only.
- The dashboard runs locally on one laptop, with no login and no encryption. It is not suitable for patient data.
- Alarm fatigue has not been assessed. The dwell times and repeat intervals would need evaluation in a simulated theatre setting.

---

## 9. Team

EXSU 620 Digital Prototyping, McGill University. Group of 5.
