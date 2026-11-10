# Build the Windows release: frugalrgb.exe zipped with its license.
#
# Needs:   Rust (MSVC toolchain) with the Visual Studio C++ build tools
# Usage:   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# Output:  dist\frugalRGB-<version>-windows-x64.zip
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$version = (Select-String -Path "$root\Cargo.toml" -Pattern '^version = "(.*)"' | Select-Object -First 1).Matches.Groups[1].Value
if (-not $version) { throw "could not determine version" }

cargo build --manifest-path "$root\Cargo.toml" --locked --release -p frugalrgb
if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }

$stage = Join-Path $root "dist\frugalRGB"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item "$root\target\release\frugalrgb.exe" "$stage\frugalRGB.exe"
Copy-Item "$root\LICENSE" "$stage\LICENSE.txt"
Copy-Item "$root\README.md" "$stage\README.md"

$zip = Join-Path $root "dist\frugalRGB-$version-windows-x64.zip"
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path $stage -DestinationPath $zip
Write-Output "Built: $zip"
