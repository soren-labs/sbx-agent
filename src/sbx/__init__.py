"""Deployment bootstrap for the public alpha (SOR-98 / Release 0.1).

``python -m sbx`` (or the ``sbx`` console script) walks a clean environment
from clone to a callable ``/v1`` control plane on the user's own Modal
workspace: ``init`` → secrets → ``deploy`` → ``doctor`` → ``smoke``, plus
``upgrade`` and ``uninstall``. Local plaintext (the ``sbx_`` bootstrap key)
is written once under the state dir with mode 0600; the control plane only
ever stores its sha256.
"""

__version__ = "0.1.1"
