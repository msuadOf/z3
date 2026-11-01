"""Build and verify relocatable native packages for this fork's GitHub releases."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parent.parent


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def smoke(prefix, platform_name):
    windows = platform_name.startswith("windows-")
    executable = prefix / "bin" / ("z3.exe" if windows else "z3")
    run(executable, "--version")
    query = """
(set-option :rlimit 2000000)
(declare-const x (_ FloatingPoint 8 24))
(declare-datatypes () ((Mode A B C D E F)))
(declare-const mode Mode)
(assert (= mode F))
(assert (fp.isNaN (fp.sqrt roundNearestTiesToAway x)))
(check-sat-using default)
(get-value (mode))
"""
    result = run(executable, "-in", input=query, text=True, capture_output=True, timeout=120)
    if result.stdout.strip() != "sat\n((mode F))":
        raise RuntimeError(f"Unexpected FP/enum result: {result.stdout!r} {result.stderr!r}")
    library = (prefix / "bin" / "libz3.dll" if windows else
               prefix / "lib" / ("libz3.dylib" if platform_name.startswith("macos-") else "libz3.so"))
    # A child releases the DLL before TemporaryDirectory cleanup on Windows.
    run(sys.executable, "-c", """
import ctypes, sys
lib = ctypes.CDLL(sys.argv[1])
values = [ctypes.c_uint() for _ in range(4)]
lib.Z3_get_version(*(ctypes.byref(v) for v in values))
assert values[0].value >= 4, "Shared library version check failed"
""", library, timeout=30)
    if not (prefix / "include" / "z3.h").is_file():
        raise RuntimeError("Missing C API headers")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True,
                        choices=["linux-x64", "linux-arm64", "macos-x64", "macos-arm64", "windows-x64"])
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build" / "fork-release")
    parser.add_argument("--skip-build", action="store_true", help="Package an already-built tree (local verification)")
    args = parser.parse_args()
    os.chdir(ROOT)
    expected = args.platform.split("-")[-1]
    actual = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if actual != expected:
        raise RuntimeError(f"Expected {expected}, running on {platform.machine()}")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    name = f"z3-fork-{sha[:12]}-{args.platform}"
    build = args.build_dir.resolve()
    stage = build / "package-stage" / name
    dist = ROOT / "dist"
    if stage.exists():
        shutil.rmtree(stage)
    stage.parent.mkdir(parents=True, exist_ok=True)
    dist.mkdir(exist_ok=True)
    if not args.skip_build:
        configure = ["cmake", "-S", ROOT, "-B", build, "-DCMAKE_BUILD_TYPE=Release",
                     "-DBUILD_SHARED_LIBS=ON", "-DCMAKE_INSTALL_LIBDIR=lib",
                     "-DZ3_BUILD_PYTHON_BINDINGS=OFF", "-DZ3_ENABLE_EXAMPLE_TARGETS=OFF"]
        if args.platform.startswith("macos-"):
            configure += ["-DCMAKE_OSX_DEPLOYMENT_TARGET=13.3", "-DCMAKE_INSTALL_RPATH=@loader_path/../lib"]
        elif args.platform.startswith("linux-"):
            configure += ["-DCMAKE_INSTALL_RPATH=$ORIGIN/../lib"]
        run(*configure)
        run("cmake", "--build", build, "--config", "Release", "--target", "shell", "libz3", "test-z3",
            "--parallel", min(os.cpu_count() or 2, 4))
    test = build / ("Release/test-z3.exe" if args.platform.startswith("windows-") else "test-z3")
    run(test, "fpa", timeout=600)
    run("cmake", "--install", build, "--config", "Release", "--prefix", stage)
    shutil.copy2(ROOT / "LICENSE.txt", stage / "LICENSE.txt")
    (stage / "BUILD-INFO.json").write_text(json.dumps({
        "repository": os.environ.get("GITHUB_REPOSITORY", "msuadOf/z3"),
        "source_commit": sha, "platform": args.platform, "system": platform.platform(),
        "run_id": os.environ.get("GITHUB_RUN_ID"), "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "configuration": "Release", "strategy": "Enum-aware QFFP probe with dt2bv inside QFFP (upstream PR #11506)",
    }, indent=2) + "\n")
    windows = args.platform.startswith("windows-")
    archive = Path(shutil.make_archive(str(dist / name), "zip" if windows else "gztar",
                                       root_dir=stage.parent, base_dir=name))
    # Test the extracted package at a different location, not just the build tree.
    with tempfile.TemporaryDirectory(prefix="z3-fork-smoke-") as tmp:
        if windows:
            with zipfile.ZipFile(archive) as package:
                package.extractall(tmp)
        else:
            with tarfile.open(archive) as package:
                package.extractall(tmp, filter="data")
        smoke(Path(tmp) / name, args.platform)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n")
    print(f"Verified package: {archive}")


if __name__ == "__main__":
    main()
