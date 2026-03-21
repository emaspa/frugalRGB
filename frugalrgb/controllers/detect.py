import logging

from ..smbus.interface import SMBusInterface
from .base import RGBController
from .asrock_polychrome import detect_asrock_polychrome_usb
from .ene_dram import detect_ene_dram
from .ene_dram_ddr5 import detect_ene_ddr5
from .asus_gpu import detect_asus_gpu
from .gigabyte_rgb_fusion2 import detect_gigabyte_rgb_fusion2
from .msi_mystic_light import detect_msi_mystic_light

log = logging.getLogger(__name__)


def detect_all(bus: SMBusInterface | None = None) -> list[RGBController]:
    """Scan for all supported RGB controllers."""
    controllers: list[RGBController] = []

    log.info("Scanning for RGB devices...")

    # ASRock Polychrome USB (newer boards use USB HID, not SMBus)
    polychrome = detect_asrock_polychrome_usb()
    if polychrome is not None:
        log.info("  Found: %s", polychrome.name)
        controllers.append(polychrome)

    # MSI Mystic Light USB HID
    msi = detect_msi_mystic_light()
    if msi is not None:
        log.info("  Found: %s", msi.name)
        controllers.append(msi)

    # Gigabyte RGB Fusion 2.0 USB HID
    gigabyte = detect_gigabyte_rgb_fusion2()
    if gigabyte is not None:
        log.info("  Found: %s", gigabyte.name)
        controllers.append(gigabyte)

    # ASUS GPU RGB via NvAPI I2C
    asus = detect_asus_gpu()
    if asus is not None:
        log.info("  Found: %s", asus.name)
        controllers.append(asus)

    # ENE DRAM controllers via SMBus
    if bus is not None:
        # DDR5 (AUDA-series) first
        ddr5_devices = detect_ene_ddr5(bus)
        for dev in ddr5_devices:
            log.info("  Found: %s", dev.name)
            controllers.append(dev)

        # DDR4 (classic ENE) fallback
        ene_devices = detect_ene_dram(bus)
        for dev in ene_devices:
            log.info("  Found: %s", dev.name)
            controllers.append(dev)

    if not controllers:
        log.warning("No RGB devices found")

    return controllers
