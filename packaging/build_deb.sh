#!/bin/bash
# Build a Debian/Ubuntu package for frugalRGB.
#
# Same layout as the AUR PKGBUILD: the binary in /usr/bin, udev rules,
# modules-load config and desktop integration.
#
# Build deps:  cargo, pkg-config, libudev-dev, libfontconfig1-dev, dpkg
# Usage:       bash packaging/build_deb.sh
# Output:      dist/frugalrgb_<version>_<arch>.deb
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/Cargo.toml" | head -n 1)"
[ -n "$VERSION" ] || { echo "error: could not determine version" >&2; exit 1; }
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"

cargo build --manifest-path "$ROOT/Cargo.toml" --locked --release -p frugalrgb

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/frugalrgb"

install -Dm755 "$ROOT/target/release/frugalrgb" "$PKG/usr/bin/frugalrgb"

# --- Device access + kernel modules for DRAM RGB -------------------------
install -Dm644 "$ROOT/70-frugalrgb.rules" \
    "$PKG/usr/lib/udev/rules.d/70-frugalrgb.rules"
mkdir -p "$PKG/usr/lib/modules-load.d"
printf 'i2c-dev\ni2c-piix4\ni2c-i801\n' > "$PKG/usr/lib/modules-load.d/frugalrgb.conf"

# --- Desktop integration --------------------------------------------------
install -Dm644 "$ROOT/packaging/frugalrgb.desktop" \
    "$PKG/usr/share/applications/frugalrgb.desktop"
install -Dm644 "$ROOT/packaging/frugalrgb.png" \
    "$PKG/usr/share/icons/hicolor/256x256/apps/frugalrgb.png"
install -Dm644 "$ROOT/LICENSE" "$PKG/usr/share/doc/frugalrgb/copyright"

# --- Package metadata -----------------------------------------------------
# libfontconfig and libudev are linked; the windowing libraries are loaded
# at run time, for whichever of Wayland and X11 the session uses.
mkdir -p "$PKG/DEBIAN"
cat > "$PKG/DEBIAN/control" <<CONTROL
Package: frugalrgb
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Depends: libc6, libgcc-s1, libfontconfig1, libudev1, libxkbcommon0, libwayland-client0, libx11-6, libxcursor1, libxi6, libxrandr2
Recommends: i2c-tools
Maintainer: Emanuele Sparvoli <sparvoli@gmail.com>
Homepage: https://github.com/emaspa/frugalRGB
Description: Lightweight standalone RGB controller for PC hardware
 no bloat, just LEDs. Controls ASRock Polychrome, MSI Mystic Light,
 Gigabyte RGB Fusion 2.0 and ASUS Aura USB motherboard RGB, ASUS GPU
 RGB (ENE over i2c-dev), ENE DRAM RGB over SMBus and the Creative
 Sound BlasterX Katana.
CONTROL

cat > "$PKG/DEBIAN/postinst" <<'POSTINST'
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
POSTINST
chmod 755 "$PKG/DEBIAN/postinst"

# --- Build ----------------------------------------------------------------
mkdir -p "$ROOT/dist"
dpkg-deb --build --root-owner-group "$PKG" "$ROOT/dist/frugalrgb_${VERSION}_${ARCH}.deb"
echo "Built: dist/frugalrgb_${VERSION}_${ARCH}.deb"
