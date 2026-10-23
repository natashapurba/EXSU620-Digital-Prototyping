import glob, serial, time
ports = sorted(glob.glob("/dev/cu.usbmodem*"))
print("Ports found:", ports)
for port in ports:
    try:
        s = serial.Serial(port, 115200, timeout=0.5)
    except Exception as e:
        print(port, "-> cannot open:", e); continue
    got = b""; end = time.time() + 3
    while time.time() < end:
        try:
            got += s.read(200)
        except Exception:
            print(port, "-> busy (another program is using it)"); break
    s.close()
    print(port, "->", got[:150] if got else "nothing received in 3 s")
