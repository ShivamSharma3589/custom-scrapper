"""Retail promotion & pricing scraper.

A generic framework for tracking how third-party retailers discount and
promote a given set of brands.

The framework (crawling, validation, deduplication, output) is retailer
agnostic. Everything that is genuinely specific to one retailer -- where its
product URLs live and how its HTML is laid out -- is confined to a single
adapter class in `retailscraper.adapters`.
"""

__version__ = "0.1.0"
