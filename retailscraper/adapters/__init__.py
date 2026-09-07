"""Retailer adapters.

Importing this package makes the built-in adapters available through the
registry in `base`.
"""

from .base import RetailerAdapter, available_adapters, get_adapter, register
from . import lookfantastic  # noqa: F401  (import registers the adapter)
from . import boots  # noqa: F401  (import registers the adapter)
from . import johnlewis  # noqa: F401  (import registers the adapter)
from . import allbeauty  # noqa: F401  (import registers the adapter)
from . import asos  # noqa: F401  (import registers the adapter)
from . import marksandspencer  # noqa: F401  (import registers the adapter)
from . import amazon  # noqa: F401  (import registers the adapter)
from . import next  # noqa: F401  (import registers the adapter)

__all__ = ["RetailerAdapter", "available_adapters", "get_adapter", "register"]
