# -*- coding: utf-8 -*-
"""Install reproducible Colab dependencies, including vendored Fubon Neo SDK.

Usage from repository root:
    python install_colab_dependencies.py

The Fubon Neo Python SDK is distributed by Fubon as a platform-specific binary
archive rather than from PyPI. Put the official Linux 64-bit .zip or .whl under
lib/fubon_neo/. This installer will extract a zip if needed, install the wheel,
and verify that `from fubon_neo.sdk import FubonSDK` succeeds.
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


def find_or_extract_wheel() -> Path:
    FUBON_DIR.mkdir(parents=True, exist_ok=True)

    wheels = sorted(FUBON_DIR.glob("fubon_neo-*.whl"))
    if wheels:
        return wheels[-1]

    archives = sorted(FUBON_DIR.glob("*.zip"))
    if not archives:
        raise FileNotFoundError(
            "找不到 Fubon Neo Linux SDK。請把富邦官方 Linux 64-bit SDK 的 "
            ".zip 或 .whl 上傳到 lib/fubon_neo/ 後再執行。"
        )

    archive = archives[-1]
    print(f"Extracting Fubon SDK: {archive.name}")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(FUBON_DIR)

    wheels = sorted(FUBON_DIR.rglob("fubon_neo-*.whl"))
    if not wheels:
        raise FileNotFoundError(f"{archive.name} 解壓後找不到 fubon_neo-*.whl")
    return wheels[-1]


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
