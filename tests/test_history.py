"""Change detection between two runs.

    python tests/test_history.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retailscraper.history import (  # noqa: E402
    CHANGE_AVAILABILITY,
    CHANGE_CAMPAIGN_ENDED,
    CHANGE_CAMPAIGN_STARTED,
    CHANGE_DISCOUNT_DEEPENED,
    CHANGE_DISCOUNT_ENDED,
    CHANGE_DISCOUNT_STARTED,
    CHANGE_PRICE_DROP,
    CHANGE_PRICE_RISE,
    CHANGE_PRODUCT_ADDED,
    CHANGE_PRODUCT_REMOVED,
    diff_runs,
)


def prod(pid, title, price, **extra):
    row = {
        "retailer": "Lookfantastic",
        "brand": "Clinique",
        "brand_matched_to": "Clinique",
        "product_id": pid,
        "product_title": title,
        "product_url": f"https://example.test/p/{pid}",
        "current_price": price,
        "availability": "InStock",
    }
    row.update(extra)
    return row


def camp(text, scope="sitewide"):
    from retailscraper.models import Campaign
    return Campaign(retailer="Lookfantastic", promotion_text=text,
                    promotion_type="percentage_discount", scope=scope,
                    source_url="https://example.test/").to_dict()


BEFORE = {
    "retailer": "Lookfantastic",
    "scraped_at": "2026-08-30T00:00:00+00:00",
    "products": [
        prod("111", "Clinique Moisture Surge 50ml", 42.0),                    # -> price drop
        prod("222", "Clinique Cleansing Balm 125ml", 34.0, discount_percent=None),  # -> discount starts
        prod("333", "Clinique Almost Lipstick", 25.0, discount_percent=25.0),  # -> discount deepens
        prod("444", "Clinique Even Better Foundation", 39.0, discount_percent=20.0),  # -> discount ends
        prod("555", "Clinique Superpowder", 30.0),                            # -> availability
        prod("666", "Clinique Discontinued Item", 20.0),                      # -> removed
        prod("777", "Clinique Unchanged Item", 15.0),                         # -> no change
    ],
    "campaigns": [camp("Up To 40% Off"), camp("Old Ended Campaign")],
}

AFTER = {
    "retailer": "Lookfantastic",
    "scraped_at": "2026-08-31T00:00:00+00:00",
    "products": [
        prod("111", "Clinique Moisture Surge 50ml", 31.5),
        prod("222", "Clinique Cleansing Balm 125ml", 25.5, discount_percent=25.0),
        prod("333", "Clinique Almost Lipstick", 18.75, discount_percent=40.0),
        prod("444", "Clinique Even Better Foundation", 39.0, discount_percent=None),
        prod("555", "Clinique Superpowder", 30.0, availability="OutOfStock"),
        prod("888", "Clinique Brand New Item", 28.0),                         # -> added
        prod("777", "Clinique Unchanged Item", 15.0),
    ],
    "campaigns": [camp("Up To 40% Off"), camp("Brand New Campaign")],
}

EXPECTED = {
    ("111", CHANGE_PRICE_DROP),
    ("222", CHANGE_DISCOUNT_STARTED),
    ("333", CHANGE_DISCOUNT_DEEPENED),
    ("444", CHANGE_DISCOUNT_ENDED),
    ("555", CHANGE_AVAILABILITY),
    ("666", CHANGE_PRODUCT_REMOVED),
    ("888", CHANGE_PRODUCT_ADDED),
}


def run() -> int:
    failures = 0
    result = diff_runs(BEFORE, AFTER)

    print("=== detected changes ===")
    for change in result["changes"]:
        print(f"  {change['kind']:22} {change['subject'][:40]:42} {change['detail']}")

    kinds = {c["kind"] for c in result["changes"]}
    subjects = {(c["subject"], c["kind"]) for c in result["changes"]}

    print("\n=== checks ===")
    checks = [
        ("price drop detected", CHANGE_PRICE_DROP in kinds),
        ("discount start detected", CHANGE_DISCOUNT_STARTED in kinds),
        ("discount deepening detected", CHANGE_DISCOUNT_DEEPENED in kinds),
        ("discount end detected", CHANGE_DISCOUNT_ENDED in kinds),
        ("availability change detected", CHANGE_AVAILABILITY in kinds),
        ("removed product detected", CHANGE_PRODUCT_REMOVED in kinds),
        ("added product detected", CHANGE_PRODUCT_ADDED in kinds),
        ("new campaign detected", CHANGE_CAMPAIGN_STARTED in kinds),
        ("ended campaign detected", CHANGE_CAMPAIGN_ENDED in kinds),
        ("no price rise reported", CHANGE_PRICE_RISE not in kinds),
        ("unchanged product produces no change",
         not any("Unchanged" in s for s, _ in subjects)),
        ("continuing campaign produces no change",
         not any("Up To 40% Off" == s for s, _ in subjects)),
    ]
    for label, ok in checks:
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")

    # Diffing two different retailers must be refused, not silently reported
    # as a total replacement of the catalogue.
    try:
        diff_runs(BEFORE, {**AFTER, "retailer": "Boots"})
        print("  FAIL cross-retailer diff should have been refused")
        failures += 1
    except ValueError:
        print("  ok   cross-retailer diff refused")

    print("\n=== incomparable runs are refused ===")
    # Two runs of one retailer covering different brands would report every
    # product in the first as removed and every product in the second as
    # added -- noise dressed as a result, and indistinguishable in the output
    # from a retailer genuinely dropping a brand overnight.
    base = {"retailer": "Lookfantastic", "scraped_at": "2026-09-01T00:00:00+00:00",
            "target_brands": ["Clinique"], "products": [], "campaigns": []}
    def refuses(before, after, expect: str) -> bool:
        try:
            diff_runs(before, after)
            return False
        except ValueError as exc:
            return expect in str(exc)

    later = dict(base, scraped_at="2026-09-05T00:00:00+00:00")
    valid_after = dict(base, scraped_at="2026-09-02T00:00:00+00:00")

    for label, ok in [
        ("brand mismatch raises",
         refuses(base, dict(valid_after, target_brands=["Tom Ford"]), "different brands")),
        # Passed the wrong way round, every price cut reads as a rise.
        ("reversed order raises", refuses(later, base, "newer than")),
        ("same brands in the right order still works",
         diff_runs(base, valid_after)["retailer"] == "Lookfantastic"),
    ]:
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
