# -*- coding: utf-8 -*-
"""Install reproducible Colab dependencies, including vendored Fubon Neo SDK.

Usage from repository root:
    python install_colab_dependencies.py

The Fubon Neo Python SDK is distributed by Fubon as a platform-specific binary
archive rather than from PyPI. Put the official Linux 64-bit .zip or .whl under
lib/fubon_neo/. This installer will extract a zip if needed, select a Linux
wheel only, install it, and verify that `from fubon_neo.sdk import FubonSDK`
succeeds.
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements-github.txt"
FUBON_DIR = ROOT / "lib" / "fubon_neo"


def run(*args: str) -> None:
    print("+", " ".join(args))
    subprocess.run(args, check=True)


def _is_linux_wheel(path: Path) -> bool:
    name = path.name.lower()
    return (
        "manylinux" in name
        or "linux_x86_64" in name
        or "linux-aarch64" in name
        or "linux_aarch64" in name
    ) and "win_" not in name and "macosx" not in name


def _pick_linux_wheel(wheels: list[Path]) -> Path | None:
    linux = sorted([p for p in wheels if _is_linux_wheel(p)])
    if linux:
        return linux[-1]
    return None


def find_or_extract_wheel() -> Path:
    FUBON_DIR.mkdir(parents=True, exist_ok=True)

    direct_wheels = list(FUBON_DIR.glob("fubon_neo-*.whl"))
    wheel = _pick_linux_wheel(direct_wheels)
    if wheel is not None:
        return wheel

    archives = sorted(FUBON_DIR.glob("*.zip"))
    for archive in archives:
        print(f"Extracting Fubon SDK: {archive.name}")
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(FUBON_DIR)

    all_wheels = list(FUBON_DIR.rglob("fubon_neo-*.whl"))
    wheel = _pick_linux_wheel(all_wheels)
    if wheel is not None:
        return wheel

    found = ", ".join(sorted(p.name for p in all_wheels)) or "none"
    raise FileNotFoundError(
        "找不到可供 Colab 使用的 Fubon Neo Linux 64-bit wheel。"
        "目前找到的 wheel: " + found + "。"
        "請從富邦官方下載 Linux 64-bit SDK；Windows 的 win_amd64.whl 無法在 Colab 安裝。"
    )


def main() -> None:
    if not REQUIREMENTS.exists():
        raise FileNotFoundError(f"找不到 {REQUIREMENTS}")

    run(sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS))
    wheel = find_or_extract_wheel()
    print(f"Installing Fubon Neo SDK: {wheel.relative_to(ROOT)}")
    run(sys.executable, "-m", "pip", "install", str(wheel))

    from fubon_neo.sdk import FubonSDK  # noqa: F401

    print("[OK] fubon_neo SDK import successful")


if __name__ == "__main__":
    main()
