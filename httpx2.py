"""Compatibility import for Starlette TestClient when httpx2 is unavailable.

The deployed application keeps using the declared ``httpx`` dependency directly.
This module only provides Starlette's ``import httpx2 as httpx`` test-client
path in environments where the httpx2 package is not yet installed.
"""

import httpx as _httpx
from httpx import *  # noqa: F403

_client = _httpx._client
_types = _httpx._types
__version__ = _httpx.__version__
