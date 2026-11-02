# Copy to CIRCUITPY as boot.py, then press the Pico's reset (or unplug/replug) once.
# Keeps the normal console for Thonny AND adds a second serial port (usb_cdc.data) for the dashboard.
import usb_cdc
usb_cdc.enable(console=True, data=True)
