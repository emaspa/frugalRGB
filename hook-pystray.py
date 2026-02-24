import sys
hiddenimports = []
if sys.platform == "win32":
    hiddenimports = ["pystray._win32"]
elif sys.platform == "darwin":
    hiddenimports = ["pystray._darwin"]
else:
    hiddenimports = ["pystray._xorg", "pystray._appindicator"]
