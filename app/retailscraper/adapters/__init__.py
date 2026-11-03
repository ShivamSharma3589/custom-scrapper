"""Retailer adapters."""

from .base import RetailerAdapter, available_adapters, get_adapter, register
from . import lookfantastic
from . import boots
from . import johnlewis
from . import allbeauty
from . import asos
from . import marksandspencer
from . import amazon
from . import next

__all__ = ["RetailerAdapter", "available_adapters", "get_adapter", "register"]
