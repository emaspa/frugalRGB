"""Zone on/off behaviour: card resolution and the packets that reach the board.

Run with: python -m unittest discover tests
"""

import struct
import unittest

from frugalrgb.controllers.base import RGBController, RGBMode, RGBZone
from frugalrgb.controllers.gigabyte_rgb_fusion2 import (
    PID_IT5702,
    PID_IT5711,
    GigabyteRGBFusion2Controller,
)
from frugalrgb.effects.engine import EffectEngine, resolve_entries

ORANGE = (255, 128, 0)
BLUE = (0, 0, 255)


class FakeController(RGBController):
    """Records every call so tests can assert on what would hit the device."""

    def __init__(self, zones, hardware=True):
        self._zones = zones
        self._hardware = hardware
        self.calls = []

    name = "Fake"
    supported_modes = list(RGBMode)

    @property
    def zones(self):
        return self._zones

    @property
    def has_hardware_mode(self):
        return self._hardware

    def set_color(self, r, g, b, zone=None):
        self.calls.append(("color", zone, (r, g, b)))

    def set_zone_off(self, zone=None):
        self.calls.append(("off", zone))

    def set_mode(self, mode, speed=3):
        self.calls.append(("mode", mode))

    def apply(self):
        self.calls.append(("apply",))


def multi_zone():
    return FakeController([
        RGBZone(0, "All Zones", is_all=True),
        RGBZone(1, "Logo"),
        RGBZone(2, "Accent"),
    ])


class ResolveEntriesTest(unittest.TestCase):
    def test_unchecked_zone_is_off_even_when_all_zones_is_checked(self):
        ctrl = multi_zone()
        entries = resolve_entries([
            (ctrl, 0, True, ORANGE),
            (ctrl, 1, True, ORANGE),
            (ctrl, 2, False, (0, 0, 0)),
        ])
        self.assertEqual(entries, [
            (ctrl, 0, ORANGE),
            (ctrl, 1, ORANGE),
            (ctrl, 2, None),
        ])

    def test_all_zones_is_written_before_the_zones_it_covers(self):
        ctrl = multi_zone()
        entries = resolve_entries([
            (ctrl, 1, True, BLUE),
            (ctrl, 2, False, ORANGE),
            (ctrl, 0, True, ORANGE),
        ])
        self.assertEqual([zid for _c, zid, _s in entries], [0, 1, 2])

    def test_unchecked_all_zones_turns_the_rest_of_the_device_off(self):
        ctrl = multi_zone()
        entries = resolve_entries([
            (ctrl, 0, False, ORANGE),
            (ctrl, 1, True, BLUE),
            (ctrl, 2, False, ORANGE),
        ])
        self.assertEqual(entries, [
            (ctrl, 0, None),
            (ctrl, 1, BLUE),
            (ctrl, 2, None),
        ])

    def test_device_with_nothing_checked_is_left_alone(self):
        lit, skipped = multi_zone(), multi_zone()
        entries = resolve_entries([
            (lit, 0, True, ORANGE),
            (skipped, 0, False, ORANGE),
            (skipped, 1, False, ORANGE),
        ])
        self.assertEqual(entries, [(lit, 0, ORANGE)])

    def test_zone_zero_is_a_real_zone_when_not_flagged_all(self):
        # ASRock numbers its physical zones from 0 and has no all-zones card
        ctrl = FakeController([RGBZone(0, "RGB Header 1"), RGBZone(1, "PCH")])
        entries = resolve_entries([
            (ctrl, 0, False, ORANGE),
            (ctrl, 1, True, BLUE),
        ])
        self.assertEqual(entries, [(ctrl, 0, None), (ctrl, 1, BLUE)])


class EngineTest(unittest.TestCase):
    def test_off_zone_uses_set_zone_off_inside_one_apply(self):
        ctrl = multi_zone()
        engine = EffectEngine()
        engine.start_effect("color_cycle", 1.0, [
            (ctrl, 0, ORANGE), (ctrl, 2, None),
        ])
        self.assertEqual(ctrl.calls, [
            ("mode", RGBMode.COLOR_CYCLE),
            ("color", 0, ORANGE),
            ("off", 2),
            ("apply",),
        ])

    def test_software_loop_skips_hardware_animated_devices(self):
        hw = multi_zone()
        sw = FakeController([RGBZone(0, "Strip")], hardware=False)
        engine = EffectEngine()
        try:
            engine.start_effect("breathing", 1.0, [
                (hw, 0, ORANGE), (sw, 0, ORANGE),
            ])
            self.assertEqual({id(e[0]) for e in engine._entries}, {id(sw)})
        finally:
            engine.stop()


class FakeHID:
    """Stands in for a hidapi device: records feature reports sent to it."""

    def __init__(self):
        self.sent = []
        self.fail = False

    def send_feature_report(self, data):
        if self.fail:
            return -1
        self.sent.append(bytes(data))
        return len(data)

    def get_feature_report(self, report_id, size):
        info = bytearray(size)
        info[0] = report_id
        info[12:28] = b"IT5711-GIGABYTE\x00"
        return list(info)

    def close(self):
        pass


def effect_packets(packets):
    """(register, zone mask, effect type, 0xRRGGBB) for each effect packet."""
    out = []
    for p in packets:
        if 0x20 <= p[1] <= 0x27 or 0x90 <= p[1] <= 0x92:
            mask = struct.unpack_from("<I", p, 2)[0]
            color = struct.unpack_from("<I", p, 14)[0]
            out.append((p[1], mask, p[11], color))
    return out


class GigabyteZoneOffTest(unittest.TestCase):
    def setUp(self):
        self.hid = FakeHID()
        self.ctrl = GigabyteRGBFusion2Controller(
            self.hid, "fake", PID_IT5711, "GIGABYTE Device"
        )
        self.hid.sent.clear()
        self.zone = {z.name: z.zone_id for z in self.ctrl.zones}

    def test_it5711_exposes_all_four_argb_headers(self):
        self.assertEqual(
            [z.name for z in self.ctrl.zones],
            ["All Zones", "12V RGB Header", "D_LED1", "D_LED2", "D_LED3",
             "D_LED4", "Logo", "Accent"],
        )

    def test_unchecked_accent_ends_up_dark_after_all_zones_lit_it(self):
        cards = [(self.ctrl, z.zone_id, z.name != "Accent", ORANGE)
                 for z in self.ctrl.zones]
        EffectEngine().start_effect("static", 1.0, resolve_entries(cards))

        packets = effect_packets(self.hid.sent)
        # All Zones: one broadcast over all 11 LED registers, static orange
        self.assertEqual(packets[0], (0x20, 0x07FF, 1, 0xFF8000))
        # Accent (LED 10, register 0x92) is the last write and it is "off"
        self.assertEqual(packets[-1], (0x92, 1 << 10, 0, 0x000000))
        # D_LED4 (LED 8, register 0x90) is addressed on its own
        self.assertIn((0x90, 1 << 8, 1, 0xFF8000), packets)
        # A single apply commits everything that was touched
        applies = [p for p in self.hid.sent if p[1] == 0x28]
        self.assertEqual(len(applies), 1)
        self.assertEqual(struct.unpack_from("<I", applies[0], 2)[0], 0x07FF)

    def test_off_zone_stays_off_in_a_colour_cycling_mode(self):
        self.ctrl.set_mode(RGBMode.COLOR_CYCLE)
        self.ctrl.set_zone_off(self.zone["Accent"])
        self.ctrl.set_color(*ORANGE, zone=self.zone["Logo"])

        accent, logo = effect_packets(self.hid.sent)
        self.assertEqual(accent[2], 0)   # EFFECT_NONE, not colour cycle
        self.assertEqual(logo[2], 4)     # mode restored for the next zone

    def test_save_to_hardware_enables_persistence_then_writes_flash(self):
        self.ctrl.save_to_hardware()
        self.assertEqual([(p[1], p[2]) for p in self.hid.sent],
                         [(0x47, 0x01), (0x5E, 0x00)])

    def test_failed_hid_write_raises(self):
        self.hid.fail = True
        with self.assertRaises(IOError):
            self.ctrl.set_color(*ORANGE, zone=self.zone["Logo"])


class GigabyteIT5702Test(unittest.TestCase):
    """IT5702 is an IT8297-class chip, not an IT5711."""

    def setUp(self):
        self.hid = FakeHID()
        self.ctrl = GigabyteRGBFusion2Controller(
            self.hid, "fake", PID_IT5702, "GIGABYTE Device"
        )

    def test_init_never_touches_it5711_only_registers(self):
        commands = {p[1] for p in self.hid.sent}
        self.assertFalse(commands & {0x61, 0x90, 0x91, 0x92})
        fast_apply = next(p for p in self.hid.sent if p[1] == 0x28)
        self.assertEqual((fast_apply[2], fast_apply[3]), (0xFF, 0x00))

    def test_layout_and_all_zones_mask_cover_the_eight_base_leds(self):
        self.assertEqual(
            [z.name for z in self.ctrl.zones],
            ["All Zones", "Back I/O", "CPU Header", "PCIe", "LED C1/C2",
             "12V RGB Header", "D_LED1", "D_LED2"],
        )
        self.hid.sent.clear()
        self.ctrl.set_color(*ORANGE, zone=0)
        self.assertEqual(effect_packets(self.hid.sent)[0][:2], (0x20, 0xFF))


if __name__ == "__main__":
    unittest.main()
