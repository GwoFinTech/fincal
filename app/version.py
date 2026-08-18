"""Build and version info (Issue #20)."""
import os

_VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")


def get_version() -> str:
    """Read the deployed version from VERSION file, or 'dev'."""
    try:
        return open(_VERSION_FILE).read().strip()
    except FileNotFoundError:
        return "dev"
