#!/bin/bash
# Build a Debian/Ubuntu package for frugalRGB.
#
# Mirrors the AUR PKGBUILD layout: app in /usr/lib/frugalrgb, launcher in
# /usr/bin, udev rules, modules-load config, and desktop integration.
# Compiled/packaged dependencies come from the distro (python3-hid is the
# same trezor/cython-hidapi library as the PyPI "hidapi" wheel, built
# against hidraw); the pure-Python deps that Debian doesn't package
# (customtkinter, CTkColorPicker, darkdetect) are vendored into
# /usr/lib/frugalrgb/vendor at build time.
#
# Build deps:  dpkg-deb (dpkg), python3-pil, python3-pip
# Usage:       bash packaging/build_deb.sh [version]
# Output:      dist/frugalrgb_<version>_all.deb
set -euo pipefail

# Use the system interpreter: the deb targets it, and build steps (icon
# rendering via python3-pil) must run against the same site-packages.
PY=/usr/bin/python3

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(sed -n 's/^pkgver=//p' "$ROOT/aur/PKGBUILD")}"
[ -n "$VERSION" ] || { echo "error: could not determine version" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/frugalrgb"
LIB="$PKG/usr/lib/frugalrgb"

# --- Application code ---------------------------------------------------
mkdir -p "$LIB"
cp -r "$ROOT/frugalrgb" "$LIB/frugalrgb"
cp "$ROOT/main.pyw" "$LIB/main.pyw"
find "$LIB" -name __pycache__ -type d -exec rm -rf {} +

# --- Vendored pure-Python deps not packaged by Debian -------------------
"$PY" -m pip install --quiet --no-deps --break-system-packages \
    --target "$LIB/vendor" \
    'customtkinter>=5.2.0,<6' 'CTkColorPicker>=0.9.0' 'darkdetect'
rm -rf "$LIB/vendor/bin"
find "$LIB/vendor" -name __pycache__ -type d -exec rm -rf {} +

# --- Launcher ------------------------------------------------------------
mkdir -p "$PKG/usr/bin"
cat > "$PKG/usr/bin/frugalrgb" <<'EOF'
#!/bin/sh
export PYTHONPATH="/usr/lib/frugalrgb/vendor${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 /usr/lib/frugalrgb/main.pyw "$@"
EOF
chmod 755 "$PKG/usr/bin/frugalrgb"

# --- Device access + kernel modules for DRAM RGB -------------------------
install -Dm644 "$ROOT/70-frugalrgb.rules" \
    "$PKG/usr/lib/udev/rules.d/70-frugalrgb.rules"
mkdir -p "$PKG/usr/lib/modules-load.d"
printf 'i2c-dev\ni2c-piix4\ni2c-i801\n' > "$PKG/usr/lib/modules-load.d/frugalrgb.conf"

# --- Desktop integration --------------------------------------------------
install -Dm644 "$ROOT/packaging/frugalrgb.desktop" \
    "$PKG/usr/share/applications/frugalrgb.desktop"
"$PY" -c "import sys; sys.path.insert(0, '$ROOT'); \
    from frugalrgb.icon import create_app_icon; \
    create_app_icon(256).save('$STAGE/frugalrgb.png')"
install -Dm644 "$STAGE/frugalrgb.png" \
    "$PKG/usr/share/icons/hicolor/256x256/apps/frugalrgb.png"
install -Dm644 "$ROOT/LICENSE" "$PKG/usr/share/doc/frugalrgb/copyright"

# --- Package metadata -----------------------------------------------------
mkdir -p "$PKG/DEBIAN"
cat > "$PKG/DEBIAN/control" <<EOF
Package: frugalrgb
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-tk, python3-pil, python3-pil.imagetk, python3-hid, python3-pystray, python3-smbus2, python3-packaging
Recommends: i2c-tools, python3-gi, gir1.2-ayatanaappindicator3-0.1
Maintainer: Emanuele Sparvoli <sparvoli@gmail.com>
Homepage: https://github.com/emaspa/frugalRGB
Description: Lightweight standalone RGB controller for PC hardware
 no bloat, just LEDs. Controls ASRock Polychrome, MSI Mystic Light,
 Gigabyte RGB Fusion 2.0 and ASUS Aura USB motherboard RGB, ASUS GPU
 RGB (ENE over i2c-dev), and ENE DDR5 DRAM RGB over SMBus.
EOF

cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    # Apply the udev rules and load the SMBus modules without a reboot.
    udevadm control --reload >/dev/null 2>&1 || true
    udevadm trigger >/dev/null 2>&1 || true
    modprobe -a i2c-dev i2c-piix4 i2c-i801 2>/dev/null || true
    cat <<'MSG'
==> frugalRGB setup:
    DRAM RGB / GPU RGB (SMBus, i2c-dev):
      1. Add your user to the i2c group, then log out and back in:
           sudo usermod -aG i2c $USER
      2. On boards whose BIOS claims the SMBus (most Gigabyte boards),
         the kernel refuses to bind the SMBus driver. Add
           acpi_enforce_resources=lax
         to GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub, then run
           sudo update-grub
         and reboot. See the README.
MSG
fi
EOF
chmod 755 "$PKG/DEBIAN/postinst"

# --- Build ----------------------------------------------------------------
find "$PKG" -type d -exec chmod 755 {} +
find "$PKG" -type f -not -path "*/DEBIAN/*" -not -name frugalrgb -exec chmod 644 {} +
chmod 755 "$PKG/usr/bin/frugalrgb"

mkdir -p "$ROOT/dist"
dpkg-deb --build --root-owner-group "$PKG" "$ROOT/dist/frugalrgb_${VERSION}_all.deb"
echo "Built: dist/frugalrgb_${VERSION}_all.deb"
