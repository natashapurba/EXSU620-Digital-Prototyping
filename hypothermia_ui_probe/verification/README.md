# Verification kit (engineering requirements ER-1 to ER-8, version 2)

This folder turns your recorded bench data into the figures and the pass/fail table for the verification report (D7). It contains no results. Every number in the report must come from your own runs.

## One-time setup

```bash
pip3 install numpy matplotlib
cd ~/Downloads/hypothermia_ui_probe/verification
mkdir data
cp templates/*.csv data/
```

## How each requirement is tested

Start the dashboard as usual. For every run, type the label, press **Start case**, do the run, press **Stop case**.

| ER | Label | Input | What you do | Runs |
|---|---|---|---|---|
| ER-1 | (none) | real probe | Calibration: 6 baths of about 33, 35, 36.5, 38, 39.5 and 41 °C (a fever thermometer reads only 32 to 42 °C; with the lab reference thermometer use 26 to 41 °C), probe and reference thermometer side by side, 5 paired readings per bath into `data/calibration_log.csv` (probe value from the dashboard line "raw reading") | 6 baths |
| ER-2 | `ER2-run1` | real probe | Leave the probe in a stable bath for at least 10 min. Do not touch the wiring | 1 |
| ER-3 | `ER3-w2c-1` ... and `ER3-c2w-1` ... | real probe | Press **Mark** at the moment you move the probe between a warm and a cold bath, then wait 60 s | 3 + 3 |
| ER-4 | `ER4-1` ... `ER4-5` | script | After Start case press **ER-4 Advisory**, wait about 1.5 min, Stop case | 5 |
| ER-5 | `ER5-1` ... `ER5-5` | script | After Start case press **ER-5 Critical**. When the ring turns red, check the phone, press **Acknowledge**, wait until green (about 3 min in total) | 5 |
| ER-4 and ER-5 | `ER45-1` ... `ER45-5` | real probe | Press **Script off** first. Probe in warm water (about 37.5 °C) until green, press **Mark** and move it into cool water (about 34.5 °C). Wait for red and the message, press **Acknowledge**, press **Mark** and move it back to warm water, wait for green | 5 |
| ER-6 | `ER6-1` | script | After Start case press **ER-6 Minutes**, wait about 11 min, then press **Handover / Export** | 1 |
| ER-7 | `ER7-1` ... `ER7-10` | real probe | Press **Script off** first. Press **Mark**, pull the DAT wire, wait 10 s, put it back | 10 |
| ER-7 | `ER7R-1` | script | After Start case press **ER-7 Out of range** (45 °C for 30 s) | 1 |
| ER-8 | (none) | | Not verified: one channel only. Report the cause and the planned protocol | 0 |

All dwell times run on a 30 s bench time scale (clinical 300 s), set by `DWELL_S` in `pico/code.py`. In the real water cycles the temperature falls by more than 0.5 °C within seconds, so the Figure 3 fall rule raises Critical at the same moment Advisory becomes due. Advisory on its own is therefore shown by the scripted ER-4 runs, which drop only 0.4 °C.

In a scripted run the real Pico code runs the real alarm logic, ring, speaker and message path, and only the temperature value comes from a fixed profile instead of the probe. This makes the 5-minute rules testable and repeatable. Say so in the method section of the report.

Fill in `data/manual_checks.csv` while you test (0.1 °C display, messages received on the phone, exposure shown in the export).

## After testing

```bash
python3 analyze.py
```

The figures and `results/results.md` (the pass/fail table, with the number of runs) appear in `results/`. A result reads INCOMPLETE when there are fewer runs than the criterion asks for. Keep `../server/monitor.db` and `data/` as your evidence.

## Before testing: software-in-the-loop check

`sim/bench_sim.py` runs the real `pico/code.py` and the real `server/app.py` together on a simulated bench, and `python3 analyze.py --demo` analyses that simulated session. This was used to find and fix design errors before bench time. Its outputs are watermarked SIMULATED and are **not** verification evidence.
