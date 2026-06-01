"""Standalone ASUS Aura mainboard RGB test (issue #2 — B760M BTF).

Run as Administrator with ASUS Armoury Crate / LightingService CLOSED:

    python test_asus_aura.py

It dumps the firmware version + config table for the ASUS Aura USB controller
(VID 0x0B05) and then cycles the onboard + addressable lighting through
red / green / blue so you can confirm it actually drives the board. Paste the
output into the GitHub issue.

Only depends on the `hid` package (pip install hid). It does not import
frugalRGB, so it can be dropped onto any machine with Python + hid.
"""

import time

import hid

VID = 0x0B05
MAINBOARD_PIDS = {0x19AF, 0x1939, 0x18F3}
SIZE = 65
PFX = 0xEC


def pad(data):
    return (list(data) + [0x00] * SIZE)[:SIZE]


def write(dev, data):
    dev.write(pad([PFX] + list(data)))


def query(dev, request, reply_id, data_offset, length):
    """Send a request and return ``length`` payload bytes of the matching reply.

    Payload starts ``data_offset`` bytes after the reply-id byte (1 for the
    firmware string, 3 for the config table).
    """
    write(dev, [request])
    try:
        resp = list(dev.read(SIZE, timeout_ms=1000))
    except Exception as e:
        print(f"    read failed: {e}")
        return None
    if not resp:
        print("    no response")
        return None
    print("    raw reply:", " ".join(f"{b:02X}" for b in resp[:8]), "...")
    for idx in (1, 0):
        if idx < len(resp) and resp[idx] == reply_id:
            start = idx + data_offset
            return resp[start:start + length]
    return None


def dump_device(info):
    pid = info["product_id"]
    path = info["path"]
    product = (info.get("product_string") or "").strip()
    print(f"\n=== VID=0x{VID:04X} PID=0x{pid:04X}  {product} ===")
    print(f"    path: {path}")

    dev = hid.device()
    try:
        dev.open_path(path)
    except Exception as e:
        print(f"    could not open: {e} (is Armoury Crate running?)")
        return

    try:
        fw = query(dev, 0x82, 0x02, data_offset=1, length=16)
        if fw:
            text = bytes(b & 0xFF for b in fw).split(b"\x00", 1)[0]
            print(f"    firmware: {text.decode('ascii', 'replace')!r}")

        cfg = query(dev, 0xB0, 0x30, data_offset=3, length=60)
        if not cfg:
            print("    >> could not read config table on this interface")
            return

        print("    config table:")
        for i in range(0, 60, 12):
            print("      " + " ".join(f"{b:02X}" for b in cfg[i:i + 12]))
        onboard = cfg[0x1B]
        headers = cfg[0x1D]
        addr = cfg[0x02]
        print(f"    -> onboard LEDs={onboard}  RGB headers={headers}  "
              f"addressable headers={addr}")

        ask = input("    Run color test on this interface (onboard static "
                    "R/G/B)? [y/N] ").strip().lower()
        if ask == "y":
            color_test(dev, onboard or 1)
    finally:
        dev.close()


def color_test(dev, onboard_leds):
    # Gen1 init handshake
    write(dev, [0x52, 0x53, 0x00, 0x01])
    count = min(onboard_leds, 16)
    mask = ((1 << count) - 1)
    for name, rgb in (("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)),
                      ("BLUE", (0, 0, 255))):
        print(f"    onboard -> {name}")
        write(dev, [0x35, 0x00, 0x00, 0x00, 0x01])          # effect: static
        write(dev, [0x36, (mask >> 8) & 0xFF, mask & 0xFF, 0x00]
              + list(rgb) * count)                           # color
        write(dev, [0x3F, 0x55])                             # commit
        time.sleep(1.5)
    print("    Did the onboard lighting change colour? Report y/n in the issue.")


def main():
    print("=== ASUS Aura mainboard RGB test (issue #2) ===")
    found = [d for d in hid.enumerate(VID) if d["product_id"] in MAINBOARD_PIDS]
    if not found:
        print("No ASUS Aura mainboard controller (VID 0x0B05, PID 0x19AF/1939/"
              "18F3) found.")
        print("All VID 0x0B05 devices present:")
        for d in hid.enumerate(VID):
            print(f"  PID=0x{d['product_id']:04X}  {d.get('product_string')}")
        return
    for info in found:
        dump_device(info)


if __name__ == "__main__":
    main()
