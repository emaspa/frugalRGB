"""Set up frugalRGB as a scheduled task that runs as admin without UAC prompt.

Run this script once as Administrator:
    python setup_task.py

It will:
1. Create a scheduled task "frugalRGB" that runs with highest privileges
2. Create a desktop shortcut "frugalRGB.lnk" to launch it
3. Optionally add it to Startup folder for auto-start at login
"""

import os
import subprocess
import sys
import shutil


def find_pythonw() -> str:
    """Find pythonw.exe path."""
    pythonw = shutil.which("pythonw")
    if pythonw:
        return os.path.abspath(pythonw)
    # Fallback: same dir as current python
    base = os.path.dirname(sys.executable)
    candidate = os.path.join(base, "pythonw.exe")
    if os.path.exists(candidate):
        return candidate
    raise FileNotFoundError("Cannot find pythonw.exe")


def create_scheduled_task(pythonw_path: str, main_pyw_path: str, working_dir: str) -> None:
    """Create a scheduled task that runs frugalRGB as admin."""
    task_name = "frugalRGB"

    # Delete existing task if any
    subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True,
    )

    # Build the XML for the task (schtasks /create with /rl highest)
    cmd = [
        "schtasks", "/create",
        "/tn", task_name,
        "/tr", f'"{pythonw_path}" "{main_pyw_path}"',
        "/sc", "ONCE",  # Dummy schedule — we trigger it manually
        "/st", "00:00",
        "/rl", "HIGHEST",
        "/f",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: Failed to create task:\n{result.stderr}")
        sys.exit(1)
    print(f"Scheduled task '{task_name}' created.")


def create_shortcut(dest_path: str) -> None:
    """Create a .lnk shortcut that runs the scheduled task."""
    # Use PowerShell to create the shortcut
    ps_script = f'''
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("{dest_path}")
$s.TargetPath = "schtasks.exe"
$s.Arguments = "/run /tn frugalRGB"
$s.Description = "frugalRGB - no bloat, just LEDs"
$s.WindowStyle = 7
$s.Save()
'''
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"WARNING: Failed to create shortcut:\n{result.stderr}")
    else:
        print(f"Shortcut created: {dest_path}")


def main() -> None:
    # Paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    main_pyw = os.path.join(script_dir, "main.pyw")
    pythonw = find_pythonw()

    print(f"pythonw:  {pythonw}")
    print(f"main.pyw: {main_pyw}")
    print()

    if not os.path.exists(main_pyw):
        print("ERROR: main.pyw not found!")
        sys.exit(1)

    # Check admin
    try:
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            print("ERROR: Run this script as Administrator.")
            sys.exit(1)
    except Exception:
        pass

    # 1. Create scheduled task
    create_scheduled_task(pythonw, main_pyw, script_dir)

    # 2. Desktop shortcut
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(desktop):
        create_shortcut(os.path.join(desktop, "frugalRGB.lnk"))

    # 3. Startup folder (optional)
    startup = os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )
    answer = input("\nAdd to Startup folder (auto-start at login)? [y/N] ").strip().lower()
    if answer == "y" and os.path.isdir(startup):
        create_shortcut(os.path.join(startup, "frugalRGB.lnk"))
        print("Added to Startup folder.")
    else:
        print("Skipped Startup folder.")

    print("\nDone! You can now launch frugalRGB from the desktop shortcut (no UAC prompt).")


if __name__ == "__main__":
    main()
