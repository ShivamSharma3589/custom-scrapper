"""Scheduling: run folders, verdicts, locks and partial saves.

The verdict is the part worth testing hardest. Before it existed every run
exited 0, so cron could not tell a good run from a blocked one -- and the two
mistakes it can make are both expensive: calling a blocked run successful
loads rubbish into the warehouse, and calling a good run failed wakes someone
at 3am for nothing. Both directions are pinned here with real numbers from
real runs.

    python tests/test_runs.py
"""

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retailscraper.runs import (  # noqa: E402
    DEFAULT_REFUSAL_LIMIT,
    PartialWriter,
    RunLockBusy,
    STATUS_FAILED,
    STATUS_OK,
    build_manifest,
    close_run_log,
    new_run_id,
    refusal_rate,
    retailer_folder_name,
    run_folder,
    run_lock,
    verdict,
    write_manifest,
)


class FakeAdapter:
    def __init__(self, display_name, name="fake", domain="example.test"):
        self.display_name = display_name
        self.name = name
        self.domain = domain


def stats(total, **statuses):
    return {
        "requests_count": total,
        "response_status_count": {f"status_{k}": v for k, v in statuses.items()},
    }


def run() -> int:
    failures = 0

    def check(label, ok, detail=None):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}"
              f"{('  ' + str(detail)) if detail is not None and not ok else ''}")

    print("=== folder names come from the retailer ===")
    for display, want in [
        ("John Lewis", "john_lewis"),
        ("Marks & Spencer", "marks_and_spencer"),
        ("Amazon UK", "amazon_uk"),
        ("AllBeauty", "allbeauty"),
    ]:
        got = retailer_folder_name(FakeAdapter(display))
        check(f"{display:18} -> {got}", got == want, got)

    print("\n=== a run folder is dated, padded and in UTC ===")
    when = datetime(2026, 9, 7, 14, 30, 0, tzinfo=timezone.utc)
    folder = run_folder(Path("output"), FakeAdapter("John Lewis"), when)
    check("path is retailer/YYYY/MM/DD/HH-MM-SS",
          folder.as_posix().endswith("john_lewis/2026/09/07/14-30-00"),
          folder.as_posix())
    # Unpadded, month 10 sorts before month 2 and a listing is out of order.
    january = run_folder(Path("o"), FakeAdapter("X"), when.replace(month=1, day=2))
    october = run_folder(Path("o"), FakeAdapter("X"), when.replace(month=10, day=2))
    check("months sort chronologically as text",
          january.as_posix() < october.as_posix(),
          (january.as_posix(), october.as_posix()))

    print("\n=== run ids sort by time and stay unique ===")
    early = new_run_id(when)
    late = new_run_id(when.replace(hour=15))
    check("earlier id sorts first", early < late, (early, late))
    check("two ids in the same second differ",
          new_run_id(when) != new_run_id(when))

    print("\n=== what counts as a refusal ===")
    # A 404 is an answer, not a refusal. Asking M&S for Tom Ford, which it
    # does not stock, 404s all four of its listing pages -- counting those
    # scored a correct run at 80% refused and failed it.
    refused, total, share = refusal_rate(stats(5, **{"200": 1, "404": 4}))
    check("404s are not refusals", refused == 0, refused)
    refused, _, _ = refusal_rate(stats(10, **{"200": 6, "403": 3, "429": 1}))
    check("403 and 429 are", refused == 4, refused)

    print("\n=== the verdict, on real runs ===")
    cases = [
        # (label, stats, products, expected, empty, want_status)
        ("Next: 1,501 of 1,840 refused, 5 products",
         stats(1840, **{"200": 339, "403": 1501}), 5,
         ["Estee Lauder"], ["Estee Lauder"], STATUS_FAILED),

        ("M&S: Tom Ford not stocked, 4 pages 404",
         stats(5, **{"200": 1, "404": 4}), 0,
         [], ["Tom Ford"], STATUS_OK),

        ("John Lewis: 33.8% refused but 770 products",
         stats(1235, **{"200": 800, "404": 18, "403": 417}), 770,
         ["Clinique", "MAC"], [], STATUS_OK),

        ("a stocked brand silently returning nothing",
         stats(400, **{"200": 400}), 120,
         ["Clinique", "Estee Lauder"], ["Estee Lauder"], STATUS_FAILED),

        ("a clean run",
         stats(300, **{"200": 300}), 250, ["Clinique"], [], STATUS_OK),

        # ASOS stocks neither Jo Malone nor Tom Ford, and its adapter cannot
        # confirm stocking, so nothing is expected of it. The first full
        # sweep failed this exact run -- 775 products, zero refusals -- over
        # two brands it was never going to have.
        ("ASOS: 775 products, two brands it does not stock",
         stats(28, **{"200": 28}), 775,
         [], ["Tom Ford", "Jo Malone"], STATUS_OK),
    ]
    for label, s, products, expected, empty, want in cases:
        # John Lewis is given the raised limit it runs with in the sweep.
        limit = 0.45 if "John Lewis" in label else DEFAULT_REFUSAL_LIMIT
        status, reason = verdict(s, products, expected, empty, refusal_limit=limit)
        ok = status == want
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label[:52]:54} {status}"
              f"{'' if ok else f' (wanted {want})'}")
        if status == STATUS_FAILED and not reason:
            failures += 1
            print("  FAIL a failure with no reason given")

    print("\n=== a crawl cut short is not a successful crawl ===")
    # John Lewis states `results: 236` for Clinique on page one and then
    # answers page three with a 404 -- a soft block on deep pagination. The
    # crawl collects two pages per brand, every request succeeds, and without
    # this the run reports OK on a fifth of the catalogue.
    healthy = stats(1200, **{"200": 1200})
    for label, products, expected, want in [
        ("336 of 1,232 -- soft-blocked pagination", 336, 1232, STATUS_FAILED),
        ("1,100 of 1,232 -- a normal crawl", 1100, 1232, STATUS_OK),
        # A stated total counts things a brand crawl will not return: other
        # colours of one product, items out of stock. The bar is deliberately
        # not 100%.
        ("700 of 1,232 -- short but plausible", 700, 1232, STATUS_OK),
        ("no stated total, so nothing to compare", 336, None, STATUS_OK),
    ]:
        status, reason = verdict(healthy, products, [], [],
                                 expected_products=expected)
        ok = status == want
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label[:46]:48} {status}")
        if status == STATUS_FAILED and reason and "of the" not in reason:
            failures += 1
            print("  FAIL the reason does not say how much was collected")

    print("\n=== a campaigns-only run is judged on its campaigns ===")
    # Without this the mode had no failure check at all: a retailer redesign
    # that breaks the offers-hub selector returns zero campaigns, and the run
    # exited 0 -- the silent success the exit codes exist to prevent.
    clean = stats(10, **{"200": 10})
    check("zero campaigns fails",
          verdict(clean, 0, [], [], campaigns=0)[0] == STATUS_FAILED)
    check("campaigns found passes",
          verdict(clean, 0, [], [], campaigns=22)[0] == STATUS_OK)
    check("a normal run is not judged on campaigns",
          verdict(clean, 250, ["Clinique"], [])[0] == STATUS_OK)

    print("\n=== the run log detaches from every logger it was attached to ===")
    # close_run_log used to remove the handler from root only, leaving a
    # CLOSED file attached to the crawler's non-propagating logger. The next
    # record written there raises "I/O operation on closed file".
    import logging
    from retailscraper.runs import capture_logger, open_run_log
    with tempfile.TemporaryDirectory() as tmp:
        handler = open_run_log(Path(tmp))
        crawler_log = logging.getLogger("scrapling.spiders.test-detach")
        crawler_log.propagate = False
        capture_logger(handler, crawler_log)
        check("attached to the crawler's logger", handler in crawler_log.handlers)
        close_run_log(handler)
        check("detached again", handler not in crawler_log.handlers)
        try:
            crawler_log.warning("after close")
            check("logging after close does not raise", True)
        except ValueError as exc:
            check("logging after close does not raise", False, exc)

    print("\n=== only an adapter that can confirm stocking judges empty brands ===")
    # The empty-brand rule is sound only where prepare() actually resolves a
    # brand page. M&S, John Lewis and Next do; the rest cannot tell an empty
    # brand from one the shop does not carry, and must not fail a run over it.
    from retailscraper.adapters.base import available_adapters, get_adapter
    confirms = {n for n in available_adapters()
                if get_adapter(n).confirms_brand_stocking}
    check("the three that resolve brand pages declare it",
          confirms == {"marksandspencer", "johnlewis", "next"}, sorted(confirms))
    check("ASOS does not", not get_adapter("asos").confirms_brand_stocking)

    print("\n=== a lock stops a second run of the same retailer ===")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        adapter = FakeAdapter("John Lewis")
        with run_lock(base, adapter):
            try:
                with run_lock(base, adapter):
                    check("second run is refused", False, "it was allowed")
            except RunLockBusy as exc:
                check("second run is refused", True)
                check("and says who holds it", "John Lewis" in str(exc), str(exc))
            # A different retailer is unaffected -- they do not share a
            # rate limit, and the sweep would stall if they did.
            try:
                with run_lock(base, FakeAdapter("Boots")):
                    check("another retailer is unaffected", True)
            except RunLockBusy:
                check("another retailer is unaffected", False)
        check("the lock is released afterwards",
              not (base / ".john_lewis.lock").exists())

    print("\n=== products are saved before the run ends ===")
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        writer = PartialWriter(folder, every=3)
        for i in range(5):
            writer.add({"product_id": f"p{i}"})
        # Three of five have crossed the threshold; two are still pending.
        check("a killed run leaves what it had", writer.path.exists())
        check("and only what was flushed",
              writer.path.read_text(encoding="utf-8").count("\n") == 3,
              writer.path.read_text(encoding="utf-8").count("\n"))
        writer.flush()
        check("a clean finish flushes the rest",
              writer.path.read_text(encoding="utf-8").count("\n") == 5)
        writer.discard()
        check("and the partial file is removed once the real output exists",
              not writer.path.exists())

    print("\n=== the manifest ===")
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        started = datetime(2026, 9, 7, 14, 0, 0, tzinfo=timezone.utc)
        manifest = build_manifest(
            run_id="20260907T140000Z-abcd1234",
            adapter=FakeAdapter("John Lewis", "johnlewis", "www.johnlewis.com"),
            started_at=started,
            finished_at=started.replace(minute=58),
            status=STATUS_OK,
            stats=stats(1235, **{"200": 800, "403": 417, "404": 18}),
            products=770, campaigns=27, rejected=0,
            brands_requested=["Clinique", "MAC"],
            brands_empty=[],
            warnings=["no brand page for 'Weleda'"],
            files=["johnlewis_clinique_products.csv"],
        )
        path = write_manifest(folder, manifest)
        check("it is written", path.exists())
        for field in ("run_id", "retailer", "started_at", "finished_at",
                      "status", "products", "campaigns", "rejected",
                      "requests_total", "requests_refused",
                      "brands_requested", "brands_empty"):
            check(f"carries {field}", field in manifest, sorted(manifest))
        check("duration is computed", manifest["duration_seconds"] == 3480.0,
              manifest["duration_seconds"])
        check("refusals are counted for the record",
              manifest["requests_refused"] == 417, manifest["requests_refused"])

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
