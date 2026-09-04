"""Custom offer vocabularies: loading, matching, and replacing the defaults.

The rule this suite exists to protect: supplying a keyword file REPLACES the
built-in retail rules. If custom keywords quietly extended the defaults, a
run would match things the file never mentions and nobody could tell what
they were actually searching for.

    python tests/test_keywords.py
"""

from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retailscraper.keywords import (  # noqa: E402
    KeywordFileError,
    compile_keywords,
    describe,
    load_keywords,
)
from retailscraper.promotions import (  # noqa: E402
    active_keywords,
    classify_promotion,
    is_confident_offer,
    looks_like_offer,
    set_offer_keywords,
)

PACKS = Path(__file__).resolve().parent.parent / "keywords"


def run() -> int:
    failures = 0

    def check(label, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label:54} expected={want!r} got={got!r}")

    try:
        print("=== the shipped packs load ===")
        for filename, least in [("black-friday.txt", 20), ("betting.txt", 8)]:
            path = PACKS / filename
            if not path.exists():
                print(f"  MISS {filename} not found")
                failures += 1
                continue
            ks = load_keywords(path)
            check(f"{filename} loads", len(ks) >= least, True)

        print("\n=== a phrase matches whole words only ===")
        ks = compile_keywords(["bonus", "free spins"], name="t")
        check("'bonus' matches 'Welcome bonus'", ks.matches("Welcome bonus"), True)
        # A bare substring search would match "bonuses"; word boundaries do not.
        check("'bonus' does not match 'bonuses'", ks.matches("bonuses today"), False)
        check("phrase matches", ks.matches("Claim 50 FREE SPINS"), True)
        check("case-insensitive", ks.matches("free SPINS"), True)
        check("absent keyword does not match", ks.matches("20% off"), False)

        print("\n=== a non-word edge does not kill the match ===")
        # "20%" wrapped as \b20%\b can never match: the trailing boundary
        # needs a word character right after the "%", and real copy reads
        # "20% off". It compiles cleanly, so validation cannot catch it -- the
        # keyword would silently match nothing, which looks exactly like a
        # site running no promotions.
        edge = compile_keywords(["20%", "£10", "50% off"], name="edge")
        check("'20%' matches '20% off everything'",
              edge.matches("20% off everything"), True)
        check("'£10' matches 'Save £10 today'", edge.matches("Save £10 today"), True)
        check("'50% off' still matches", edge.matches("Get 50% off now"), True)
        # The boundary must still apply on the side that can carry one.
        check("boundary kept where it works",
              compile_keywords(["bonus"], name="e").matches("bonuses"), False)

        print("\n=== regex keywords are honoured as written ===")
        rx = compile_keywords([r"bet .* get", r"[0-9]+x wager"], name="t")
        check("'bet .* get'", rx.matches("Bet 10 get 30 in free bets"), True)
        check("'[0-9]+x wager'", rx.matches("subject to 5x wager"), True)
        check("no false match", rx.matches("no deposit needed"), False)

        print("\n=== a broken keyword is reported, not skipped ===")
        try:
            compile_keywords(["valid", "[unclosed"], name="t")
            check("invalid regex raises", False, True)
        except KeywordFileError as exc:
            check("invalid regex raises", "[unclosed" in str(exc), True)
        try:
            compile_keywords([], name="t")
            check("empty set raises", False, True)
        except KeywordFileError:
            check("empty set raises", True, True)

        print("\n=== custom keywords REPLACE the defaults ===")
        # Built-in behaviour first, as a baseline.
        check("baseline: '25% off' is an offer", is_confident_offer("25% off"), True)
        check("baseline: 'free spins' is not", is_confident_offer("free spins"), False)

        set_offer_keywords(compile_keywords(["free spins", "no deposit"], name="betting"))
        check("custom is active", active_keywords() is not None, True)
        check("custom keyword now matches", is_confident_offer("50 free spins"), True)
        # The point of the whole feature: the defaults are gone, not merged.
        check("default '25% off' NO LONGER matches", is_confident_offer("25% off"), False)
        check("default 'half price' NO LONGER matches", is_confident_offer("half price"), False)
        check("both offer tests use the custom set", looks_like_offer("no deposit"), True)
        check("looks_like_offer drops defaults too", looks_like_offer("gift"), False)

        print("\n=== classification still works under a custom vocabulary ===")
        # Type classification is generic retail semantics and is deliberately
        # left alone; a custom match that fits no known shape becomes "other".
        check("percentage still classified",
              classify_promotion("Black Friday 30% off"), "percentage_discount")
        # "free spins" contains "free", so the existing gift rule claims it.
        # That is the intended behaviour: classification is independent of
        # which vocabulary decided the text was an offer in the first place.
        check("betting wording classified by the generic rules",
              classify_promotion("50 free spins"), "gift_with_purchase")
        check("wording matching no known shape becomes other",
              classify_promotion("acca insurance"), "other")

        print("\n=== restoring the defaults ===")
        set_offer_keywords(None)
        check("custom cleared", active_keywords(), None)
        check("'25% off' matches again", is_confident_offer("25% off"), True)
        check("'free spins' does not", is_confident_offer("free spins"), False)

        print("\n=== file formats ===")
        with tempfile.TemporaryDirectory() as tmp:
            txt = Path(tmp) / "t.txt"
            txt.write_text("# a comment\nblack friday\n\ndoorbuster  # trailing\n",
                           encoding="utf-8")
            ks = load_keywords(txt)
            check("text: comments and blanks skipped", len(ks), 2)
            check("text: matches", ks.matches("BLACK FRIDAY deals"), True)

            js = Path(tmp) / "t.json"
            js.write_text('{"name": "bf", "keywords": ["cyber monday"]}', encoding="utf-8")
            ks = load_keywords(js)
            check("json: name honoured", ks.name, "bf")
            check("json: matches", ks.matches("Cyber Monday"), True)

            bare = Path(tmp) / "b.json"
            bare.write_text('["doorbuster"]', encoding="utf-8")
            check("json: bare list accepted", len(load_keywords(bare)), 1)

            bad = Path(tmp) / "bad.json"
            bad.write_text('{"name": "x"}', encoding="utf-8")
            try:
                load_keywords(bad)
                check("json without keywords raises", False, True)
            except KeywordFileError:
                check("json without keywords raises", True, True)

            try:
                load_keywords(Path(tmp) / "missing.txt")
                check("missing file raises", False, True)
            except KeywordFileError:
                check("missing file raises", True, True)

        print("\n=== the run header says which vocabulary is in force ===")
        check("default described", describe(None), "built-in retail offer rules")
        check("custom described",
              describe(compile_keywords(["a", "b"], name="pack")).startswith("pack (2 keywords"),
              True)

    finally:
        # Never leave a custom vocabulary set: this is process-global state and
        # would silently change every suite that runs after this one.
        set_offer_keywords(None)

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
