# Perioperative temperature monitor: UI structure

```
 Pico 2 W  (pico/code.py, run from Thonny)
   MCP9808 -> state machine -> NeoPixel ring + speaker + bedside button
        |  JSON line once per second
        |  USB "data" port (usb_cdc.data)  <- Thonny keeps the normal console port
        v
 Laptop service  (server/app.py, Flask)
   reads serial -> SQLite (readings, events, notifications)
                -> CR-4 disagreement check (IR vs contact, never averaged)
                -> WhatsApp notifier (own thread, status tracked)
                -> Server-Sent Events -> Dashboard (server/static/index.html)
   Dashboard "Acknowledge" -> POST /api/ack -> "ACK\n" back to the Pico
```

## Run it
1. No hardware: `pip install flask`, then `python server/app.py --sim`, open http://127.0.0.1:5050, press Start case.
2. With the Pico:
   - Copy `pico/boot.py` to CIRCUITPY and reset the board once. Run `pico/code.py` from Thonny as before.
   - `pip install flask pyserial`, then `python server/app.py --list-ports` and pick the second Pico port (not Thonny's).
   - `python server/app.py --port COM7`

## WhatsApp
Set `WA_PROVIDER` to `callmebot` (WA_PHONE, WA_APIKEY) or `twilio` (TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, WA_TO).
Default `none` is a dry run: the UI shows "not configured", never "sent".

## Changes to your code.py
Additions only, each marked "UI ADDITION": usb_cdc data port, send_to_ui() in place of print, ACK command, and extra JSON
fields (ring, sound, adv, crit, demo). The alarm logic is untouched.

## Not built yet
MLX90614 on the Pico (dashboard shows IR as "not connected" and skips the disagreement check until "ir" is in the JSON),
the disagreement margin (DISAGREE_C = 1.0 C is a placeholder for your engineering requirement), and a handover page
beyond the JSON/CSV export.
