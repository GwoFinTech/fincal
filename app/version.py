"""Build and version info (Issue #20, Issue #79)."""
import os

_VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")

#: Environment variable a deploy injects into the image with the commit it built.
#: ``Dockerfile`` only copies ``app/``, so the ``VERSION`` file that
#: ``scripts/deploy.sh`` writes next to the source tree never reaches the
#: container: reading only the file made ``/api/admin/health`` report ``dev`` on
#: every deployment, i.e. the service could not attest the code it was running.
COMMIT_ENV = "FINCAL_GIT_COMMIT"


def get_version() -> str:
    """Return the deployed version, or ``'dev'`` when nothing declares it.

    Precedence (Issue #79): the commit injected at image build time
    (``FINCAL_GIT_COMMIT``) wins, because inside the container it is the only
    value that describes the running code; then a ``VERSION`` file next to the
    source tree (host runs, and ``scripts/deploy.sh`` before the image build);
    otherwise ``'dev'``.
    """
    injected = os.getenv(COMMIT_ENV, "").strip()
    if injected:
        return injected
    try:
        with open(_VERSION_FILE) as handle:
            return handle.read().strip() or "dev"
    except FileNotFoundError:
        return "dev"
