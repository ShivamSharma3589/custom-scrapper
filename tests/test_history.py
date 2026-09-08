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

    print("\n=== --latest finds the runs a schedule actually writes ===")
    # Scheduled runs live in <retailer>/YYYY/MM/DD/HH-MM-SS/, not in the
    # output/history/ folder that --archive uses. Looking only in history/
    # meant `changes.py --latest` kept comparing two runs from the first of
    # the month while the scheduled runs beside them went unread -- the tool
    # that exists to spot changes could not see the runs made to find them.
    import json as _json
    import tempfile

    from changes import find_latest_run_folders, load_run  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for retailer, stamps in (
            ("allbeauty", ["11-31-09", "19-33-02", "09-00-00"]),
            ("boots", ["12-08-03"]),
        ):
            for stamp in stamps:
                folder = root / retailer / "2026" / "09" / "07" / stamp
                folder.mkdir(parents=True)
                (folder / "manifest.json").write_text("{}", encoding="utf-8")
                # A run folder holds one file per brand, plus the shared
                # campaigns document.
                for brand, price in (("clinique", 10.0), ("mac", 20.0)):
                    (folder / f"{brand}.json").write_text(_json.dumps({
                        "retailer": retailer,
                        "products": [{
                            "product_id": f"{brand}-1",
                            "brand_matched_to": brand.title(),
                            "current_price": price,
                        }],
                        "campaigns": [{"campaign_id": "c1", "scope": "sitewide"}],
                    }), encoding="utf-8")
                (folder / "campaigns.json").write_text(
                    _json.dumps({"campaigns": []}), encoding="utf-8")

        found = find_latest_run_folders(root, "allbeauty", [])
        stamps = [run[0].parent.name for run in found]
        for label, ok, detail in [
            ("two runs are returned", len(found) == 2, len(found)),
            # 09-00-00 sorts before 11-31-09, so "most recent" means the two
            # latest timestamps, not the last two created.
            ("the two most recent, oldest first",
             stamps == ["11-31-09", "19-33-02"], stamps),
            # Comparing one brand's file would report every other brand's
            # products as removed.
            ("every brand file in the run is gathered",
             all(len(run) == 2 for run in found), [len(r) for r in found]),
            ("the manifest is not mistaken for a run",
             all(p.name != "manifest.json" for run in found for p in run), None),
            ("nor is the shared campaigns document",
             all(p.name != "campaigns.json" for run in found for p in run), None),
        ]:
            failures += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'FAIL'} {label}"
                  f"{('  ' + str(detail)) if not ok else ''}")

        # The merge is what a diff actually reads.
        merged = load_run(found[-1])
        for label, ok, detail in [
            ("merging a run gives every brand's products",
             len(merged["products"]) == 2, len(merged["products"])),
            ("campaigns repeated in each brand file are counted once",
             len(merged["campaigns"]) == 1, len(merged["campaigns"])),
            ("and the brands present are derived from the records",
             merged["target_brands"] == ["Clinique", "Mac"],
             merged["target_brands"]),
        ]:
            failures += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'FAIL'} {label}"
                  f"{('  ' + str(detail)) if not ok else ''}")

        # Diffing two different retailers would report every product as
        # added and every other as removed.
        unfiltered = find_latest_run_folders(root, None, [])
        same = len({p.parents[3].name for run in unfiltered for p in run}) == 1
        failures += 0 if same else 1
        print(f"  {'ok  ' if same else 'FAIL'} both runs come from one retailer")

        # Naming a brand narrows the comparison to that brand's files.
        one_brand = find_latest_run_folders(root, "allbeauty", ["clinique"])
        ok = one_brand and all(
            len(run) == 1 and run[0].stem == "clinique" for run in one_brand
        )
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} a brand filter selects that brand's files")

        single = find_latest_run_folders(root, "boots", [])
        ok = len(single) == 1
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} a retailer with one run yields one, "
              "so the caller can say two are needed")

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
