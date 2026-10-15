from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ("run.cmd", "pyproject.toml", "uv.lock", "README.md")
SOURCE_DIRECTORIES = ("apps", "packages", "tools", "docs")
EXCLUDED_PARTS = {
    ".git",
    ".github",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def source_files() -> list[Path]:
    files = [ROOT / name for name in ROOT_FILES]
    for directory_name in SOURCE_DIRECTORIES:
        directory = ROOT / directory_name
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file()
            and not any(part in EXCLUDED_PARTS for part in path.relative_to(ROOT).parts)
            and path.suffix.lower() not in {".pyc", ".pyo"}
        )
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise RuntimeError(f"Release input is missing: {missing[0]}")
    return sorted(set(files), key=lambda path: path.relative_to(ROOT).as_posix())


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def package_version() -> str:
    source = (ROOT / "apps/studio/src/trade_router_studio/__init__.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    if not match:
        raise RuntimeError("Package version was not found.")
    return match.group(1)


def build(version: str, output: Path) -> tuple[Path, Path]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError("Release version must use MAJOR.MINOR.PATCH.")
    configured_versions = {project_version(), package_version()}
    if configured_versions != {version}:
        raise RuntimeError(
            f"Release version {version} does not match source versions: "
            + ", ".join(sorted(configured_versions))
        )

    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"trade-router-v{version}-windows.zip"
    checksum = output / f"{archive.name}.sha256"
    files = source_files()
    manifest = {
        "format": 1,
        "version": version,
        "files": [
            {
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": sha256(path),
            }
            for path in files
        ],
    }
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(ROOT).as_posix())
        bundle.writestr(
            "release-manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    checksum.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    return archive, checksum


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Trade Router Windows release ZIP.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    arguments = parser.parse_args()
    archive, checksum = build(arguments.version, arguments.output.resolve())
    print(archive)
    print(checksum)


if __name__ == "__main__":
    main()
