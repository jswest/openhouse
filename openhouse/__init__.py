"""openhouse — pull, parse, and query U.S. House financial disclosures."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("openhouse")
except PackageNotFoundError:  # not installed (e.g. running from a bare checkout)
    __version__ = "0.0.0+unknown"
