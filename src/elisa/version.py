"""The el version, always taken from pyproject.toml.

Run from a source checkout, the version is read straight from the project's
pyproject.toml, so a bump (for example with `cz bump`) shows up immediately.
Installed as a package, it comes from the package metadata, which the build
fills in from the same pyproject.toml.
"""

import tomllib
from importlib import metadata
from pathlib import Path


PACKAGE = "elisa"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def get_version() -> str:
    """Return the version declared in pyproject.toml."""
    try:
        with PYPROJECT.open("rb") as fh:
            project = tomllib.load(fh).get("project", {})
        if project.get("name") == PACKAGE and "version" in project:
            return str(project["version"])
    except (OSError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version(PACKAGE)
    except metadata.PackageNotFoundError:
        return "unknown"
