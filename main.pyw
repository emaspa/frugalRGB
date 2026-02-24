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
        lock_path = os.path.join(tempfile.gettempdir(), "frugalrgb.lock")
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

    if not check_admin():
        log.warning(
            "Not running as Administrator. SMBus devices (RAM RGB) won't be available. "
            "USB devices (motherboard RGB) may still work."
        )

    # Initialize SMBus (optional — needed for DRAM RGB, not for USB devices)
    bus = None
    if check_admin():
        try:
            from frugalrgb.smbus import get_smbus
            log.info("Initializing SMBus...")
            bus = get_smbus()
            bus.open()
        except Exception as e:
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
