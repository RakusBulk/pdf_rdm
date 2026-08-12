"""Derive a stable hardware fingerprint for the current machine.

Windows: MachineGuid from the registry (stable per-OS-install, survives
reboots, not tied to any single removable component).
macOS:   IOPlatformUUID from ioreg (stable per-logical-board).

Both are hashed (never sent or stored raw) so the server only ever
sees an opaque, non-reversible identifier.
"""
from __future__ import annotations

import hashlib
import platform
import subprocess


class MachineIdError(RuntimeError):
    pass


def _raw_windows_id() -> str:
    import winreg  # available only on Windows

    key = winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
    )
    try:
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
        return value
    finally:
        winreg.CloseKey(key)


def _raw_macos_id() -> str:
    out = subprocess.check_output(
        ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        stderr=subprocess.DEVNULL,
    ).decode("utf-8", errors="ignore")
    for line in out.splitlines():
        if "IOPlatformUUID" in line:
            # line looks like:  "IOPlatformUUID" = "XXXXXXXX-XXXX-..."
            return line.split("=", 1)[1].strip().strip('"')
    raise MachineIdError("IOPlatformUUID not found in ioreg output")


def get_raw_machine_id() -> str:
    system = platform.system()
    if system == "Windows":
        return _raw_windows_id()
    if system == "Darwin":
        return _raw_macos_id()
    raise MachineIdError(f"Unsupported platform: {system}")


def get_machine_fingerprint() -> str:
    """Return a stable, opaque SHA-256 hex fingerprint for this machine."""
    raw = get_raw_machine_id()
    salted = f"pdf-drm-v1:{platform.system()}:{raw}"
    return hashlib.sha256(salted.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    print(get_machine_fingerprint())
