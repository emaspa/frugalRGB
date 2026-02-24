"""Build frugalRGB into a standalone exe using PyInstaller."""

import os
import subprocess
import sys


def generate_icon():
    """Generate the app icon as a .ico file for embedding in the exe."""
    from PIL import Image, ImageDraw, ImageFilter

    size = 256
    r_ch = Image.new("L", (size, size), 0)
    g_ch = Image.new("L", (size, size), 0)
    b_ch = Image.new("L", (size, size), 0)

    cx, cy = size // 2, size // 2
    radius = int(size * 0.32)
    spread = int(size * 0.16)

    circles = [
        (r_ch, cx, cy - spread),
        (g_ch, cx - int(spread * 0.87), cy + int(spread * 0.5)),
        (b_ch, cx + int(spread * 0.87), cy + int(spread * 0.5)),
    ]
    for ch, ox, oy in circles:
        ImageDraw.Draw(ch).ellipse(
            [ox - radius, oy - radius, ox + radius, oy + radius], fill=255,
        )

    rgb = Image.merge("RGB", (r_ch, g_ch, b_ch))
    glow = rgb.filter(ImageFilter.GaussianBlur(radius=size // 16))
    rgb = Image.blend(rgb, glow, 0.4)

    alpha = rgb.convert("L")
    img = rgb.convert("RGBA")
    img.putalpha(alpha)

    ico_path = os.path.join(os.path.dirname(__file__), "frugalrgb.ico")
    img.save(ico_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    print(f"Icon saved: {ico_path}")
    return ico_path


def build(ico_path):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    main_pyw = os.path.join(script_dir, "main.pyw")

    modules_dir = os.path.join(script_dir, "modules")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--name", "frugalRGB",
        f"--icon={ico_path}",
        "--collect-all", "customtkinter",
        "--collect-all", "CTkColorPicker",
        "--collect-all", "hidapi",
        "--hidden-import", "pystray._win32",
        "--additional-hooks-dir", script_dir,
        "--add-data", f"{modules_dir}{os.pathsep}modules",
        main_pyw,
    ]

    print("Running PyInstaller...")
    print(" ".join(cmd))
    print()
    result = subprocess.run(cmd, cwd=script_dir)
    if result.returncode == 0:
        dist = os.path.join(script_dir, "dist", "frugalRGB")
        print(f"\nBuild complete! Output: {dist}")
        print(f"Run: {os.path.join(dist, 'frugalRGB.exe')}")
    else:
        print("\nBuild failed!")
        sys.exit(1)


def main():
    # Check pyinstaller is installed
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not installed. Run: pip install pyinstaller")
        sys.exit(1)

    ico_path = generate_icon()
    build(ico_path)


if __name__ == "__main__":
    main()
