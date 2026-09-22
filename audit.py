"""Audit collected runs for records that cannot be true."""

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

EXPECTED_CURRENCY = "GBP"

DISCOUNT_TOLERANCE = 1.5

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

        if original is not None and current is not None and original < current:
            fault("was-price below now-price", f"{title}: {original} -> {current}")

        stated = row.get("discount_percent")
        if stated is not None and original and current is not None and original > 0:
            computed = round((original - current) / original * 100, 2)
            if abs(computed - stated) > DISCOUNT_TOLERANCE:
                fault("discount disagrees with prices",
                      f"{title}: says {stated}%, prices imply {computed:.1f}%")

        amount = row.get("discount_amount")
        if amount is not None and original is not None and current is not None:
            if abs((original - current) - amount) > 0.02:
                fault("discount amount wrong",
                      f"{title}: says {amount}, difference is {original - current:.2f}")

        if stated and original is None:
            fault("discount with no was-price", f"{title}: {stated}%")

        if not row.get("product_title"):
            fault("no title", row.get("product_url", "?"))
        if not row.get("brand"):
            fault("no brand", title)
        if not row.get("brand_verified_by"):
            fault("brand not attributed to a source", title)

        domain = (document.get("domain") or "").replace("https://", "").strip("/")
        url = row.get("product_url") or ""
        if domain and domain not in url:
            fault("url does not match the retailer", f"{title}: {url[:60]}")

        pid = row.get("product_id")
        if pid:
            if pid in seen_ids and seen_ids[pid] != url:
                fault("duplicate product id", f"{pid}: {url[:50]}")
            seen_ids[pid] = url

        for applied in row.get("applied_campaigns") or []:
            if applied not in campaign_ids:
                fault("applied campaign not in this run", f"{title}: {applied}")

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
