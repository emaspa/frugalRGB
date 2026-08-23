"""frugalRGB — Lightweight standalone RGB controller."""

import ctypes
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("frugalrgb")


def check_admin() -> bool:
    """Check if the process has admin/root privileges."""
    if sys.platform == "win32":
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    else:
        import os
        return os.geteuid() == 0


def _check_single_instance() -> bool:
    """Return True if this is the only instance, False if another is already running."""
    if sys.platform == "win32":
        ctypes.windll.kernel32.CreateMutexW(None, False, "frugalRGB_SingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.user32.MessageBoxW(
                0, "Another instance of frugalRGB is running.", "frugalRGB", 0x40,
            )
            return False
    else:
        import fcntl
        import os
        import tempfile
        lock_path = os.path.join(tempfile.gettempdir(), "frugalrgb.lock")
        # Keep a module-level reference so the fd (and its flock) survives
        # this function returning.
        global _lock_file
        _lock_file = open(lock_path, "w")
        try:
            fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("frugalRGB", "Another instance of frugalRGB is running.")
            root.destroy()
            return False
    return True


def main() -> None:
    if not _check_single_instance():
        sys.exit(0)

    # Set app ID before any window is created so Windows uses our icon in taskbar
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("frugalrgb")

    # Initialize SMBus (optional — needed for DRAM RGB, not for USB devices).
    # On Windows the kernel driver requires Administrator, so don't even try
    # without it. On Linux what matters is /dev/i2c-* access (root OR i2c
    # group membership), so always attempt and fall back on failure.
    bus = None
    if sys.platform == "win32" and not check_admin():
        log.warning(
            "Not running as Administrator. SMBus devices (RAM RGB) won't be available. "
            "USB devices (motherboard RGB) may still work."
        )
    else:
        try:
            from frugalrgb.smbus import get_smbus
            log.info("Initializing SMBus...")
            bus = get_smbus()
            bus.open()
        except Exception as e:
            if sys.platform != "win32":
                log.warning(
                    "SMBus init failed (DRAM RGB unavailable): %s — "
                    "add your user to the 'i2c' group or run as root.", e
                )
            else:
                log.warning("SMBus init failed (DRAM RGB unavailable): %s", e)
            bus = None

    # Detect devices (both USB HID and SMBus)
    from frugalrgb.controllers.detect import detect_all

    controllers = detect_all(bus)
    log.info("Found %d RGB device(s)", len(controllers))

    # Launch GUI
    from frugalrgb.gui.app import FrugalRGBApp

    apply_quit = "--apply-quit" in sys.argv
    app = FrugalRGBApp(controllers, apply_quit=apply_quit, bus=bus)
    try:
        app.mainloop()
    finally:
        if bus is not None:
            bus.close()


if __name__ == "__main__":
    main()
