#!/usr/bin/env python3
"""Package the extension for the Chrome Web Store.

Built by script rather than by zipping the folder by hand, because the two
things most likely to go wrong are silent: shipping a file that should not
leave the machine, and shipping a manifest that references a file the package
does not contain. Both are checked here before anything is written.

  python deploy/build_extension.py

Produces dist/talent-pilot-extension-<version>.zip, ready to upload.
"""

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "extension"
DIST = ROOT / "dist"

# The Windows console defaults to cp1252, which cannot encode the em dash in
# the extension's name or a non-ASCII character in the project path — and the
# resulting UnicodeEncodeError killed the script *after* the zip was written,
# reporting failure for a build that had actually succeeded.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Anything matching these never enters the package. rules.js is listed even
# though it no longer exists: it used to hold real personal details, and an old
# working copy on someone's disk must not be able to ship.
EXCLUDE_NAMES = {"rules.js", "rules.example.js", ".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIXES = {".md", ".log", ".zip", ".crx", ".pem"}
EXCLUDE_DIRS = {"node_modules", "__pycache__", ".git"}

# Strings that must never appear in a shipped file. The extension used to embed
# one person's contact details; this makes a regression loud instead of silent.
FORBIDDEN = ["anipy2000", "7900842067", "aniruddh", "parashar"]

# Host permissions that exist for local development and must not reach a user.
#
# The manifest in the repo grants http://localhost:8000 so an unpacked build can
# be pointed at an API running on the same machine. Shipped, it is a permission
# every install carries and nobody uses, and a reviewer reads it as exactly what
# it is — a debugging leftover. Stripped here rather than deleted from the
# manifest so loading extension/ unpacked still works with no local edit.
DEV_ONLY_HOST_PREFIXES = ("http://localhost", "http://127.0.0.1", "https://localhost")


def shipped_manifest(manifest: dict) -> tuple[dict, list[str]]:
    """The manifest as users receive it, and what was taken out of it."""
    shipped = json.loads(json.dumps(manifest))

    hosts = shipped.get("host_permissions", [])
    stripped = [h for h in hosts if h.startswith(DEV_ONLY_HOST_PREFIXES)]

    if stripped:
        shipped["host_permissions"] = [h for h in hosts if h not in stripped]

    return shipped, stripped


def collect() -> list[Path]:
    files = []

    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.relative_to(SOURCE).parts):
            continue
        if path.name in EXCLUDE_NAMES or path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        files.append(path)

    return files


def check_manifest(files: list[Path]) -> dict:
    """Every file the manifest names must be in the package."""
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))

    referenced = [
        manifest["background"]["service_worker"],
        manifest["action"]["default_popup"],
    ]
    referenced += list(manifest.get("icons", {}).values())
    referenced += list(manifest["action"].get("default_icon", {}).values())
    for entry in manifest.get("content_scripts", []):
        referenced += entry.get("js", []) + entry.get("css", [])

    packaged = {str(f.relative_to(SOURCE)).replace("\\", "/") for f in files}
    missing = [ref for ref in dict.fromkeys(referenced) if ref not in packaged]

    if missing:
        sys.exit(f"ERROR: manifest references files not in the package: {missing}")

    if not manifest.get("icons"):
        sys.exit("ERROR: no icons declared; Chrome will show a puzzle piece.")

    return manifest


def check_no_personal_data(files: list[Path]) -> None:
    hits = []

    for path in files:
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".woff", ".woff2"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for needle in FORBIDDEN:
            if needle in text:
                hits.append(f"{path.relative_to(SOURCE)} contains {needle!r}")

    if hits:
        sys.exit("ERROR: personal data would ship:\n  " + "\n  ".join(hits))


def main() -> int:
    if not SOURCE.is_dir():
        sys.exit(f"ERROR: {SOURCE} does not exist")

    files = collect()
    manifest = check_manifest(files)
    check_no_personal_data(files)
    shipped, stripped = shipped_manifest(manifest)

    DIST.mkdir(exist_ok=True)
    archive = DIST / f"talent-pilot-extension-{manifest['version']}.zip"

    # The Web Store expects the manifest at the archive root, not inside a
    # folder, so paths are stored relative to extension/.
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            # The manifest is written from the stripped copy rather than copied
            # off disk, so the file the store reads is the one checked above
            # minus anything that was only ever for local development.
            if path.name == "manifest.json" and path.parent == SOURCE:
                bundle.writestr("manifest.json", json.dumps(shipped, indent=2) + "\n")
                continue
            bundle.write(path, path.relative_to(SOURCE))

    print(f"{manifest['name']} v{manifest['version']}")
    print(f"  {len(files)} files -> {archive}")
    print(f"  {archive.stat().st_size / 1024:.1f} KB\n")

    for path in files:
        print(f"    {path.relative_to(SOURCE)}")

    if stripped:
        print("\n  Development-only host permissions left out of the package:")
        for host in stripped:
            print(f"    - {host}")

    print("\n  Host permissions users will be asked for:")
    for host in shipped.get("host_permissions", []):
        print(f"    - {host}")
    for host in shipped.get("optional_host_permissions", []):
        print(f"    - {host} (optional, requested per site)")

    print("\nChecks passed: manifest complete, no personal data, icons present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
