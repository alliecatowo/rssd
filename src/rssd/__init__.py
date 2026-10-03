"""rssd: a file-based RSS daemon. The filesystem is the API."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rssd-fs")
except PackageNotFoundError:  # running from a bare source tree
    __version__ = "0+unknown"
