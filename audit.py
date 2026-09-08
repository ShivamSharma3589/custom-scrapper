"""Audit collected runs for records that cannot be true.

The test suite proves the code behaves as written against fixtures. This asks
a different question of the real output: does any record contradict itself, or
contradict the retailer it claims to come from?

It is deliberately independent of the extraction code. Nothing here imports an
adapter or reuses a parsing helper, so a mistake shared between extraction and
its own test cannot hide from it -- the checks are re-derived from the values
in the file.

    python audit.py                 # every run under output/
    python audit.py output/asos     # one retailer
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

#: Currency every record must be in. A price in anything else is not a UK
#: price and would silently distort every comparison built on it.
EXPECTED_CURRENCY = "GBP"

#: How far a stated discount may differ from the one the prices imply, in
#: percentage points. Retailers round their own percentages.
DISCOUNT_TOLERANCE = 1.5

#: A price no beauty product plausibly has. Not a hard rule about the world,
#: a tripwire for a decimal point read from the wrong element.
MAX_PLAUSIBLE_PRICE = 2000.0


def find_runs(root: Path) -> Iterator[Tuple[Path, Dict[str, Any]]]:
    """Every result document under `root`, newest run per retailer first."""
    for path in sorted(root.rglob("*.json")):
        if path.name == "manifest.json" or "history" in path.parts:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(document, dict) and "products" in document:
            yield path, document


def audit_document(path: Path, document: Dict[str, Any]) -> List[str]:
    """Every way this run's records contradict themselves."""
    problems: List[str] = []
    retailer = document.get("retailer", "?")
    products = document.get("products") or []
    campaigns = document.get("campaigns") or []

    def fault(kind: str, detail: str) -> None:
        problems.append(f"{retailer}: {kind}: {detail}")

    seen_ids: Dict[str, str] = {}
    campaign_ids = {c.get("campaign_id") for c in campaigns}

    for row in products:
        title = (row.get("product_title") or "")[:44]
        current = row.get("current_price")
        original = row.get("original_price")

        if current is None or not isinstance(current, (int, float)):
            fault("no price", title)
        else:
            if current <= 0:
                fault("price is zero or negative", f"{title} = {current}")
            if current > MAX_PLAUSIBLE_PRICE:
                fault("implausible price", f"{title} = {current}")

        if row.get("currency") != EXPECTED_CURRENCY:
            fault("wrong currency", f"{title} = {row.get('currency')}")

        # A was-price below the now-price cannot describe a discount; the two
        # numbers came from unrelated parts of the page.
        if original is not None and current is not None and original < current:
            fault("was-price below now-price", f"{title}: {original} -> {current}")

        # The stated discount must agree with the prices beside it.
        stated = row.get("discount_percent")
        if stated is not None and original and current is not None and original > 0:
            computed = round((original - current) / original * 100, 2)
            if abs(computed - stated) > DISCOUNT_TOLERANCE:
                fault("discount disagrees with prices",
                      f"{title}: says {stated}%, prices imply {computed:.1f}%")

        # discount_amount must be the actual difference.
        amount = row.get("discount_amount")
        if amount is not None and original is not None and current is not None:
            if abs((original - current) - amount) > 0.02:
                fault("discount amount wrong",
                      f"{title}: says {amount}, difference is {original - current:.2f}")

        # A discount claimed with no was-price to support it.
        if stated and original is None:
            fault("discount with no was-price", f"{title}: {stated}%")

        if not row.get("product_title"):
            fault("no title", row.get("product_url", "?"))
        if not row.get("brand"):
            fault("no brand", title)
        if not row.get("brand_verified_by"):
            fault("brand not attributed to a source", title)

        # The URL must belong to the retailer the record claims.
        domain = (document.get("domain") or "").replace("https://", "").strip("/")
        url = row.get("product_url") or ""
        if domain and domain not in url:
            fault("url does not match the retailer", f"{title}: {url[:60]}")

        # Two records for one product id means deduplication failed.
        pid = row.get("product_id")
        if pid:
            if pid in seen_ids and seen_ids[pid] != url:
                fault("duplicate product id", f"{pid}: {url[:50]}")
            seen_ids[pid] = url

        # A campaign id on a product that no campaign in this run defines.
        for applied in row.get("applied_campaigns") or []:
            if applied not in campaign_ids:
                fault("applied campaign not in this run", f"{title}: {applied}")

    # A site-wide campaign applies to everything by definition, so every
    # product should carry it. Fewer means attachment depended on page order.
    sitewide = [c["campaign_id"] for c in campaigns if c.get("scope") == "sitewide"]
    if sitewide and products:
        for campaign_id in sitewide:
            carrying = sum(1 for p in products
                           if campaign_id in (p.get("applied_campaigns") or []))
            if carrying != len(products):
                fault("site-wide campaign missing from products",
                      f"{campaign_id}: on {carrying} of {len(products)}")

    for row in campaigns:
        if not row.get("promotion_text"):
            fault("campaign with no text", row.get("campaign_id", "?"))
        if row.get("scope") == "sitewide" and row.get("scope_value"):
            fault("site-wide campaign carries a scope value",
                  f"{row.get('promotion_text', '')[:40]} -> {row.get('scope_value')}")

    return problems


def main(argv: List[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent / "output"
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2

    documents = list(find_runs(root))
    if not documents:
        print(f"no result documents found under {root}")
        return 0

    total_products = 0
    total_campaigns = 0
    all_problems: List[str] = []
    per_retailer: Dict[str, int] = defaultdict(int)

    for path, document in documents:
        products = len(document.get("products") or [])
        total_products += products
        total_campaigns += len(document.get("campaigns") or [])
        per_retailer[document.get("retailer", "?")] += products
        problems = audit_document(path, document)
        if problems:
            all_problems.extend(problems)

    print(f"audited {len(documents)} run(s): "
          f"{total_products} products, {total_campaigns} campaigns")
    for retailer, count in sorted(per_retailer.items(), key=lambda kv: -kv[1]):
        print(f"  {retailer:20} {count:>6}")

    if not all_problems:
        print("\nno contradictions found")
        return 0

    print(f"\n{len(all_problems)} problem(s):")
    for kind, count in Counter(p.split(": ")[1] for p in all_problems).most_common():
        print(f"  {count:>5}  {kind}")
    print("\nexamples:")
    for problem in all_problems[:20]:
        print(f"  {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
