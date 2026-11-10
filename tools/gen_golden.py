"""Record what the Python controllers send, as fixtures for the Rust drivers.

The Python controllers in tools/reference are what frugalRGB shipped up to
0.3.0. They are kept as the reference the Rust drivers were ported from.

Each controller is driven through a set of scenarios against a fake device.
Everything it writes, and every reply the fake hands back, is recorded in
order. The Rust tests replay the same scenarios against the same replies and
must produce the same bytes (crates/frugalrgb-core/tests/golden.rs).

Run from the repository root:  python tools/gen_golden.py
"""

import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "reference"))

OUT = ROOT / "crates" / "frugalrgb-core" / "tests" / "golden"

ORANGE = [255, 128, 0]
BLUE = [0, 0, 255]
WHITE = [255, 255, 255]
DIM = [10, 200, 37]
CORRECTION = [1.0, 0.262, 0.505]


# --------------------------------------------------------------------------- #
#  Fake transports                                                            #
# --------------------------------------------------------------------------- #

class FakeHID:
    """A hidapi device that records traffic and answers from a responder.

    responder(kind, data) is called for every write ("w") and feature write
    ("sf"); it returns the input reports that write provokes. Feature reads
    ("gf") are answered by responder("gf", [report_id]).
    """

    def __init__(self, responder):
        self.responder = responder
        self.transcript = []
        self.pending = []

    def open_path(self, path):
        pass

    def close(self):
        pass

    def set_nonblocking(self, flag):
        return 0

    def write(self, data):
        data = bytes(data)
        self.transcript.append(["w", data.hex()])
        self.pending.extend(self.responder("w", data) or [])
        return len(data)

    def read(self, size, timeout_ms=0):
        if not self.pending:
            return []
        data = bytes(self.pending.pop(0))[:size]
        self.transcript.append(["r", data.hex()])
        return list(data)

    def send_feature_report(self, data):
        data = bytes(data)
        self.transcript.append(["sf", data.hex()])
        self.pending.extend(self.responder("sf", data) or [])
        return len(data)

    def get_feature_report(self, report_id, size):
        data = bytes(self.responder("gf", bytes([report_id])))[:size]
        self.transcript.append(["gf", data.hex()])
        return list(data)


class FakeBus:
    """An SMBus with ENE-style devices: a word write to command 0x00 selects
    a 16-bit register, command 0x81 reads it and command 0x01 writes it.

    devices: {address: {register: value}}. Other addresses do not answer.
    """

    def __init__(self, devices):
        self.devices = {addr: dict(regs) for addr, regs in devices.items()}
        self.selected = {}
        self.transcript = []

    def _device(self, kind, addr, payload):
        if addr not in self.devices:
            self.transcript.append([kind + "_nak", bytes([addr] + payload).hex()])
            raise OSError(6, "No such device or address")
        return self.devices[addr]

    def write_word_data(self, addr, cmd, value):
        payload = [cmd, value & 0xFF, value >> 8]
        self._device("ww", addr, payload)
        self.transcript.append(["ww", bytes([addr] + payload).hex()])
        if cmd == 0x00:
            self.selected[addr] = ((value & 0xFF) << 8) | (value >> 8)

    def write_byte_data(self, addr, cmd, value):
        regs = self._device("wb", addr, [cmd, value])
        self.transcript.append(["wb", bytes([addr, cmd, value]).hex()])
        if cmd == 0x01:
            regs[self.selected.get(addr, 0)] = value

    def read_byte_data(self, addr, cmd):
        regs = self._device("rb", addr, [cmd])
        value = regs.get(self.selected.get(addr, 0), 0) if cmd == 0x81 else 0
        self.transcript.append(["rb", bytes([addr, cmd, value]).hex()])
        return value

    def write_block_data(self, addr, cmd, data):
        raise AssertionError("no controller uses block writes")


def ene_registers(name=None, led_count_at=None, led_count=0, extra=None):
    regs = {}
    if name:
        regs.update({0x1000 + i: c for i, c in enumerate(name.encode())})
    if led_count_at is not None:
        regs[0x1C00 + led_count_at] = led_count
    regs.update(extra or {})
    return regs


def install_fake_hid(devices):
    """Replace the `hid` module so detect functions find our fake devices.

    devices: list of (info dict, FakeHID). Returns nothing; detection opens
    them in enumeration order.
    """
    queue = list(devices)
    by_path = {info["path"]: dev for info, dev in devices}

    class Device:
        def __init__(self):
            self._dev = None

        def open_path(self, path):
            self._dev = by_path[path]

        def open(self, vid, pid):
            self._dev = next(dev for info, dev in queue
                             if info["vendor_id"] == vid and info["product_id"] == pid)

        def __getattr__(self, name):
            return getattr(self._dev, name)

    def enumerate(vid=0, pid=0):
        return [
            info for info, _dev in queue
            if (not vid or info["vendor_id"] == vid)
            and (not pid or info["product_id"] == pid)
        ]

    module = types.ModuleType("hid")
    module.enumerate = enumerate
    module.device = Device
    sys.modules["hid"] = module


def hid_info(vid, pid, path, interface=-1, usage_page=0, usage=0, product=""):
    return {
        "vendor_id": vid, "product_id": pid, "path": path,
        "interface_number": interface, "usage_page": usage_page,
        "usage": usage, "product_string": product,
    }


# --------------------------------------------------------------------------- #
#  Scenario runner                                                            #
# --------------------------------------------------------------------------- #

def run_ops(ctrl, ops):
    from frugalrgb.controllers.base import RGBMode

    modes = {
        "off": RGBMode.OFF, "static": RGBMode.STATIC,
        "breathing": RGBMode.BREATHING, "color_cycle": RGBMode.COLOR_CYCLE,
        "rainbow": RGBMode.RAINBOW, "strobe": RGBMode.STROBE,
    }

    def write_zones(zones):
        for zone, color in zones:
            if color is None:
                ctrl.set_zone_off(zone)
            else:
                ctrl.set_color(*color, zone=zone)
        ctrl.apply()

    for op in ops:
        kind = op["op"]
        if kind == "correction":
            ctrl.color_correction = tuple(op["value"])
        elif kind == "apply":
            ctrl.set_mode(modes[op["mode"]], op["speed"])
            write_zones(op["zones"])
        elif kind == "frame":
            write_zones(op["zones"])
        elif kind == "save":
            ctrl.save_to_hardware()
        else:
            raise ValueError(kind)


def apply(mode, speed, zones):
    return {"op": "apply", "mode": mode, "speed": speed, "zones": zones}


def single_zone_cases(zone, can_save=True):
    """Scenarios for a device with one zone (or only an all-zones card)."""
    lit, mixed, dark = [[zone, ORANGE]], [[zone, DIM]], [[zone, None]]
    cases = [
        ("static_lit", [apply("static", 1, lit)]),
        ("static_dark", [apply("static", 1, dark)]),
        ("static_with_correction", [
            {"op": "correction", "value": CORRECTION},
            apply("static", 1, lit),
        ]),
        ("leds_off", [apply("off", 1, [[zone, [0, 0, 0]]])]),
        ("nothing_to_write", [apply("static", 1, [])]),
        ("mode_changes_in_one_session", [
            apply("static", 1, lit),
            apply("rainbow", 2, mixed),
            apply("breathing", 0, lit),
            apply("off", 1, [[zone, [0, 0, 0]]]),
            apply("static", 1, mixed),
        ]),
        ("software_frames", [
            apply("breathing", 1, lit),
            {"op": "frame", "zones": mixed},
            {"op": "frame", "zones": [[zone, [0, 0, 0]]]},
            apply("strobe", 1, lit),
            {"op": "frame", "zones": dark},
        ]),
    ]
    for mode in ("breathing", "strobe", "color_cycle", "rainbow"):
        for speed in (0, 3, 5):
            cases.append((f"{mode}_speed_{speed}", [apply(mode, speed, lit)]))
    if can_save:
        cases.append(("save_after_apply", [apply("static", 1, lit), {"op": "save"}]))
    return cases


def standard_cases(zone_ids, all_id=0, can_save=True, unknown_zone=True):
    """Scenarios that make sense for any multi-zone device.

    zone_ids: the real zones; all_id: the all-zones pseudo zone, or None.
    """
    real = list(zone_ids)
    base = [[all_id, ORANGE]] if all_id is not None else []

    everything = base + [[z, ORANGE] for z in real]
    one_off = base + [[z, ORANGE] for z in real[:-1]] + [[real[-1], None]]
    mixed = base + [[z, [BLUE, DIM, WHITE][i % 3]] for i, z in enumerate(real)]
    all_dark_base = ([[all_id, None]] if all_id is not None else []) + \
        [[real[0], BLUE]] + [[z, None] for z in real[1:]]
    blackout = ([[all_id, [0, 0, 0]]] if all_id is not None else []) + \
        [[z, [0, 0, 0]] for z in real]

    cases = [
        ("static_all_lit", [apply("static", 1, everything)]),
        ("static_last_zone_off", [apply("static", 1, one_off)]),
        ("static_mixed_colours", [apply("static", 1, mixed)]),
        ("static_all_zones_unchecked", [apply("static", 1, all_dark_base)]),
        ("static_with_correction", [
            {"op": "correction", "value": CORRECTION},
            apply("static", 1, one_off),
        ]),
        ("leds_off", [apply("off", 1, blackout)]),
        ("mode_changes_in_one_session", [
            apply("static", 1, everything),
            apply("rainbow", 2, one_off),
            apply("static", 1, everything),
            apply("breathing", 0, mixed),
            apply("off", 1, blackout),
            apply("static", 1, mixed),
        ]),
        ("software_frames", [
            apply("breathing", 1, one_off),
            {"op": "frame", "zones": base + [[z, DIM] for z in real[:-1]] + [[real[-1], None]]},
            {"op": "frame", "zones": base + [[z, [0, 0, 0]] for z in real[:-1]] + [[real[-1], None]]},
            apply("strobe", 1, one_off),
            {"op": "frame", "zones": one_off},
        ]),
    ]
    for mode in ("breathing", "strobe", "color_cycle", "rainbow"):
        for speed in (0, 3, 5):
            cases.append((f"{mode}_speed_{speed}", [apply(mode, speed, one_off)]))
    if unknown_zone:
        cases.append(("unknown_zone_is_ignored", [apply("static", 1, base + [[200, BLUE]])]))
    if can_save:
        cases.append(("save_after_apply", [apply("static", 1, one_off), {"op": "save"}]))
    return cases


def record(build, cases, **extra):
    """Run every case on a freshly built controller and collect transcripts."""
    out = []
    for name, ops in cases:
        ctrl, devices = build()
        run_ops(ctrl, ops)
        transcript = [event for dev in devices for event in dev.transcript]
        assert len(devices) == 1, "interleaving of several devices is not recorded"
        out.append({
            "name": name,
            "device_name": ctrl.name,
            "zones": [[z.zone_id, z.name, bool(z.is_all)] for z in ctrl.zones],
            "ops": ops,
            "transcript": transcript,
            **extra,
        })
    return out


# --------------------------------------------------------------------------- #
#  Controllers                                                                #
# --------------------------------------------------------------------------- #

def gigabyte(pid):
    from frugalrgb.controllers import gigabyte_rgb_fusion2 as gb

    def responder(kind, data):
        if kind == "gf":
            info = bytearray(64)
            info[0] = 0xCC
            info[4:8] = bytes([1, 0, 6, 12])
            info[12:12 + 25] = b"IT5711-GIGABYTE V1.0.06.C"
            info[56:60] = (0x5711C000).to_bytes(4, "little")
            return info
        return []

    def build():
        dev = FakeHID(responder)
        install_fake_hid([(hid_info(gb.GIGABYTE_VID, pid, b"/dev/fake0"), dev)])
        ctrl = gb.detect_gigabyte_rgb_fusion2()
        assert ctrl is not None
        return ctrl, [dev]

    zones = range(1, 8)
    return record(build, standard_cases(zones), pid=pid)


def katana(reject_pulsate=False, noisy=False):
    from frugalrgb.controllers import creative_katana as ck

    def frame(cmd, data):
        return (bytes([0x5A, cmd, len(data)]) + bytes(data)).ljust(64, b"\x00")

    def responder(kind, data):
        if kind != "w":
            return []
        cmd, length = data[2], data[3]
        payload = data[4:4 + length]
        replies = []
        if noisy:
            # An unrelated async event (volume change) ahead of the answer
            replies.append(frame(0x23, [0x01, 0x02]))
        if cmd == ck.CMD_SYSINFO:
            replies.append(frame(ck.CMD_SYSINFO, b"2.6.220401.080\x00"))
        elif cmd == ck.CMD_LIGHTING and payload[0] == ck.SUB_CUR_PROFILE:
            replies.append(frame(ck.CMD_LIGHTING, [ck.SUB_CUR_PROFILE, 0x05]))
        elif cmd == ck.CMD_LIGHTING:
            sub = payload[0]
            code = 0x00
            if (reject_pulsate and sub == ck.SUB_SET_PARAM
                    and payload[2] == ck.TYPE_PULSATE
                    and payload[3] == ck.PARAM_PALETTE):
                code = 0x05
            replies.append(frame(ck.CMD_ERROR, [ck.CMD_LIGHTING, code, sub]))
        return replies

    def build():
        dev = FakeHID(responder)
        install_fake_hid([
            (hid_info(ck.KATANA_VID, ck.KATANA_PID, b"/dev/fake4", interface=4), dev),
        ])
        ctrl = ck.detect_creative_katana()
        assert ctrl is not None
        return ctrl, [dev]

    return record(build, standard_cases(range(1, 8), can_save=False))


def ene_ddr5():
    from frugalrgb.controllers import ene_dram_ddr5 as ddr5

    def build():
        bus = FakeBus({
            0x70: ene_registers("AUDA0-E6K5-0101", led_count_at=2, led_count=8),
            0x71: ene_registers("AUDA0-E6K5-0101", led_count_at=2, led_count=5),
            0x72: ene_registers("AUDA0-E6K5-0101"),           # no LED count: 8
            0x73: ene_registers("LED-something"),              # not an AUDA chip
            0x75: ene_registers("AU\x01"),                      # not text
        })
        found = ddr5.detect_ene_ddr5(bus)
        assert [c.name for c in found] == [
            "ENE DDR5 (0x70)", "ENE DDR5 (0x71)", "ENE DDR5 (0x72)"]
        return found[1], [bus]   # odd slot: reversed direction, 5 LEDs

    return record(build, standard_cases(range(1, 6)), pick=1)


def ene_dram():
    from frugalrgb.controllers import ene_dram as dram

    pattern = {0x00A0 + i: i for i in range(16)}

    def build():
        bus = FakeBus({
            0x70: {**pattern, 0x00A5: 0x77},                   # broken pattern
            0x71: {**pattern, **{0x1030 + i: c for i, c in enumerate(b"Micron")}},
            0x72: dict(pattern),
        })
        found = dram.detect_ene_dram(bus)
        assert [c.name for c in found] == ["ENE DRAM (0x72)"]
        return found[0], [bus]

    return record(build, single_zone_cases(0, can_save=False), pick=0)


def asus_gpu(leds):
    from frugalrgb.controllers import asus_gpu as gpu

    label = "TUF RTX 5090 OC" if leds > 1 else ""

    def build():
        bus = FakeBus({gpu.ENE_ADDR: ene_registers("AUMA0-E6K5-0107", led_count_at=3,
                                                   led_count=leds)})
        # Detection reads the chip name before handing the bus over
        ene_name = gpu._read_ene_name(bus, gpu.ENE_ADDR)
        assert ene_name == "AUMA0-E6K5-0107"
        return gpu.ASUSGPUController(bus, "NVIDIA GPU 0000:01:00.0", label, ene_name), [bus]

    cases = standard_cases(range(1, leds + 1)) if leds > 1 else single_zone_cases(0)
    return record(build, cases)


def aura(onboard=5, headers=2, addressable=3, config=True, strip_report_id=False,
         real_zones=4):
    from frugalrgb.controllers import asus_aura_usb as au

    def reply(reply_id, payload):
        body = bytes([0xEC, reply_id]) + bytes(payload)
        body = body.ljust(65, b"\x00")
        return body[1:] if strip_report_id else body

    def responder(kind, data):
        if kind != "w" or not config:
            return []
        if data[1] == au.AURA_REQUEST_FIRMWARE:
            return [reply(au.REPLY_FIRMWARE, b"AULA3-AR32-0207\x00")]
        if data[1] == au.AURA_REQUEST_CONFIG:
            table = bytearray(60)
            table[0x02] = addressable
            table[0x1B] = onboard
            table[0x1D] = headers
            return [reply(au.REPLY_CONFIG, bytes(2) + bytes(table))]
        return []

    def build():
        dev = FakeHID(responder)
        install_fake_hid([
            (hid_info(au.ASUS_AURA_VID, 0x19AF, b"/dev/fake2",
                      product=" AURA LED Controller "), dev),
        ])
        ctrl = au.detect_asus_aura_usb()
        assert ctrl is not None
        return ctrl, [dev]

    return record(build, standard_cases(range(1, real_zones + 1)),
                  pid=0x19AF, product=" AURA LED Controller ")


def msi(pid, product, state_len=185):
    from frugalrgb.controllers import msi_mystic_light as ms

    def responder(kind, data):
        if kind == "gf":
            state = bytearray((i * 7 + 3) & 0xFF for i in range(state_len))
            state[0] = ms.REPORT_ID
            return state
        return []

    def build():
        dev = FakeHID(responder)
        install_fake_hid([
            (hid_info(ms.MSI_VID, pid, b"/dev/fake1", usage_page=1, usage=0,
                      product=product), dev),
        ])
        ctrl = ms.detect_msi_mystic_light()
        assert ctrl is not None
        return ctrl, [dev]

    known = pid in ms.MSI_BOARD_DB
    cases = standard_cases([1, 2]) if known else single_zone_cases(0)
    return record(build, cases, pid=pid)


def asrock(answers=True):
    from frugalrgb.controllers import asrock_polychrome as ar

    def responder(kind, data):
        if kind != "w" or not answers:
            return []
        reply = bytearray(64)
        reply[1] = data[1]
        if data[1] == ar.CMD_READ_HEADER:
            if data[3] == ar.CFG_LED_COUNT:
                reply[4:12] = bytes([1, 0x1E, 60, 0x1E, 1, 3, 0x1E, 0x1E])
            elif data[3] == ar.CFG_RGSWAP:
                reply[4] = 0x05
            elif data[3] == ar.CFG_ZONE_AVAIL:
                reply[4] = 0b0011_0111
        return [reply]

    def build():
        dev = FakeHID(responder)
        install_fake_hid([(hid_info(ar.ASROCK_VID, ar.ASROCK_PID, b"/dev/fake3"), dev)])
        ctrl = ar.detect_asrock_polychrome_usb()
        assert ctrl is not None
        return ctrl, [dev]

    if answers:
        # Zone 1 has no LEDs and zone 3 is masked out: 0, 2, 4 and 5 remain
        cases = standard_cases([0, 2, 4, 5], all_id=None, can_save=False,
                               unknown_zone=False)
    else:
        cases = [("commit_only", [apply("static", 1, [])])]
    return record(build, cases)


def software_frames():
    """Colours the software effect loop computes, for the Rust engine."""
    from frugalrgb.effects.engine import EffectEngine

    engine = EffectEngine()
    rows = []
    for effect in ("breathing", "color_cycle", "rainbow", "strobe", "static"):
        engine._effect = effect
        for step in range(0, 400):
            t = step * (1.0 / 30.0) * 1.7
            for base in ((255, 128, 0), (10, 200, 37)):
                rows.append([effect, t, list(base), list(engine._compute_frame(t, base))])
    return rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    frames = software_frames()
    (OUT / "software_frames.json").write_text(
        "[\n" + ",\n".join(json.dumps(r) for r in frames) + "\n]\n")
    print(f"software_frames.json: {len(frames)} frames")
    suites = {
        "gigabyte_it5711": lambda: gigabyte(0x5711),
        "gigabyte_it5702": lambda: gigabyte(0x5702),
        "gigabyte_it8297": lambda: gigabyte(0x8297),
        "katana": katana,
        "katana_rejects_pulsate_colour": lambda: katana(reject_pulsate=True),
        "katana_with_async_events": lambda: katana(noisy=True),
        "ene_ddr5": ene_ddr5,
        "ene_dram": ene_dram,
        "asus_gpu_4_leds": lambda: asus_gpu(4),
        "asus_gpu_1_led": lambda: asus_gpu(1),
        "aura": aura,
        "aura_no_config_table": lambda: aura(config=False, real_zones=1),
        "aura_stripped_report_id": lambda: aura(strip_report_id=True),
        "aura_addressable_only": lambda: aura(onboard=0, headers=0, addressable=2,
                                              real_zones=2),
        "aura_many_onboard_leds": lambda: aura(onboard=20, headers=4, addressable=1,
                                               real_zones=2),
        "msi_known_board": lambda: msi(0x7E03, "MSI MYSTIC LIGHT "),
        "msi_unknown_board": lambda: msi(0x7B93, "MYSTIC LIGHT "),
        "msi_short_report": lambda: msi(0x7E03, "MYSTIC LIGHT ", state_len=150),
        "asrock": asrock,
        "asrock_no_replies": lambda: asrock(answers=False),
    }
    for name, build in suites.items():
        cases = build()
        path = OUT / f"{name}.json"
        path.write_text(
            "[\n" + ",\n".join(json.dumps(c, separators=(",", ":")) for c in cases) + "\n]\n")
        events = sum(len(c["transcript"]) for c in cases)
        print(f"{path.relative_to(ROOT)}: {len(cases)} cases, {events} events")


if __name__ == "__main__":
    main()
