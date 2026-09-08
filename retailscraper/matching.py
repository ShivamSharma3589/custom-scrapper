"""Matching the same product across different retailers.

Retailers name the same item differently, so "Clinique Anti Blemish
Solutions Cleansing Foam 125ml" and "Clinique Anti-Blemish Solutions(TM)
Cleansing Foam 125ml" have to be recognised as one product.

The rule is deliberately strict, because a false match produces a confident
price comparison between two different products -- worse than no match:

  1. same brand, compared on `brand_matched_to`
  2. same size -- non-negotiable, since a 125ml and a 200ml balm share
     almost every word but are different products at different prices
  3. what remains of the titles must overlap strongly

Anything failing those is left unmatched rather than guessed at.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Size as retailers write it: 125ml, 1.9g, 50 ML, 100H is NOT a size.
_SIZE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(ml|l|g|kg|oz|cl)\b",
    re.IGNORECASE,
)

_PACK_RE = re.compile(r"\b(\d+)\s*[x×]\s*\d+(?:\.\d+)?\s*(?:ml|l|g|kg|oz|cl)\b", re.IGNORECASE)

_NOISE_RE = re.compile(r"[™®©�]")

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "for", "with", "of", "in", "to",
    "various", "shades", "shade", "colour", "color", "new",
})

_UNIT_TO_BASE = {"ml": 1.0, "cl": 10.0, "l": 1000.0, "g": 1.0, "kg": 1000.0, "oz": 28.35}
_UNIT_KIND = {"ml": "volume", "cl": "volume", "l": "volume",
              "g": "weight", "kg": "weight", "oz": "weight"}


@dataclass
class ProductMatch:
    """One product, as sold by two or more retailers."""

    brand: str
    title: str
    size: Optional[str]
    score: float
    offers: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def _comparable(self) -> List[Dict[str, Any]]:
        """Offers whose price means the same thing.

        A "from" price is the cheapest of several sizes behind one page, not
        the price of a specific item. Ranking it against another retailer's
        single-size price makes that retailer look far cheaper than it is --
        John Lewis quoting "£33.60 – £156.00" for Black Orchid would be named
        cheapest over Lookfantastic's £86.40 for the 50ml, a £52.80 gap that
        does not exist. Those offers stay in `offers` so the reader can see
        them; they are simply not ranked.

        Offers in another currency are excluded for the same reason. Nothing
        in the pipeline stops a non-sterling price reaching the output --
        validation does not check currency, and the adapters that read it off
        the page pass on whatever it says -- so a EUR price would otherwise be
        ranked against a GBP one as though 45 and 50 were the same kind of
        number, and named the cheaper of the two.
        """
        priced = [
            o for o in self.offers
            if o.get("current_price") is not None and not o.get("price_is_from")
        ]
        if not priced:
            return []

        # the currency most offers agree on -- the odd one out is the suspect
        common = Counter(
            o.get("currency") for o in priced if o.get("currency")
        ).most_common(1)
        if not common:
            return priced
        return [o for o in priced if o.get("currency") == common[0][0]]

    @property
    def currency_mismatch(self) -> bool:
        """True when the offers are not all priced in the same currency.

        Reported rather than silently resolved: it means one retailer served
        a page in another currency, which is a fact about the run worth
        seeing, not a row to quietly drop.
        """
        seen = {o.get("currency") for o in self.offers if o.get("current_price") is not None}
        return len({c for c in seen if c}) > 1

    @property
    def cheapest(self) -> Optional[Dict[str, Any]]:
        comparable = self._comparable
        return min(comparable, key=lambda o: o["current_price"]) if comparable else None

    @property
    def price_gap(self) -> Optional[float]:
        """Difference between the highest and lowest directly comparable price."""
        prices = [o["current_price"] for o in self._comparable]
        return round(max(prices) - min(prices), 2) if len(prices) > 1 else None

    @property
    def from_price_count(self) -> int:
        """How many offers were excluded from ranking as "from" prices."""
        return sum(1 for o in self.offers if o.get("price_is_from"))

    @property
    def rrp_disagreement(self) -> bool:
        """True when the retailers disagree about this product's RRP.

        Two retailers selling the same item quote roughly the same
        recommended price. A wide spread means the titles matched but the
        products did not: "TOM FORD Noir Eau de Parfum, 100ml" appears at
        John Lewis with
        an RRP of 158.00 and at Lookfantastic with an RRP of 320.00, which is
        a different fragrance sharing a name. The match is still reported --
        it may be a genuine repricing -- but flagged so a reader does not
        quote a 132.01 saving that is not real.
        """
        rrps = [o["original_price"] for o in self.offers
                if o.get("original_price") is not None]
        if len(rrps) >= 2 and max(rrps) > min(rrps) * 1.25:
            return True

        # no RRP to compare, so use the prices: two shops selling the same
        # item do not differ by 2x -- 126.40 / 320.00 is two fragrances
        prices = [o["current_price"] for o in self._comparable]
        if len(prices) >= 2 and max(prices) > min(prices) * 2.0:
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        cheapest = self.cheapest
        return {
            "brand": self.brand,
            "title": self.title,
            "size": self.size,
            "match_score": round(self.score, 3),
            "retailer_count": len(self.offers),
            "price_gap": self.price_gap,
            "cheapest_retailer": cheapest["retailer"] if cheapest else None,
            "cheapest_price": cheapest["current_price"] if cheapest else None,
            "from_price_offers": self.from_price_count,
            "rrp_disagreement": self.rrp_disagreement,
            "currency_mismatch": self.currency_mismatch,
            "offers": self.offers,
        }


def extract_size(title: str) -> Optional[str]:
    """Return a normalised size key for a title, or None if it states no size.

    Normalising to a base unit means "1L" and "1000ml" match, while keeping
    volume and weight separate so "50g" never matches "50ml".

    >>> extract_size("Clinique Take The Day Off Cleansing Balm 125ml")
    'volume:125.0'
    """
    pack = _PACK_RE.search(title)
    multiplier = int(pack.group(1)) if pack else 1

    match = _SIZE_RE.search(title)
    if not match:
        return None

    amount = float(match.group(1))
    unit = match.group(2).lower()
    base = amount * _UNIT_TO_BASE[unit] * multiplier
    return f"{_UNIT_KIND[unit]}:{round(base, 2)}"


def normalize_title(title: str, brand: Optional[str] = None) -> List[str]:
    """Reduce a title to comparable tokens.

    Strips the brand (it is compared separately and would otherwise inflate
    every score), trademark symbols, punctuation and stopwords.
    """
    text = _NOISE_RE.sub(" ", title or "").casefold()

    if brand:
        brand_pattern = r"[^a-z0-9]+".join(
            re.escape(part) for part in re.split(r"[^a-z0-9]+", brand.casefold()) if part
        )
        if brand_pattern:
            text = re.sub(brand_pattern, " ", text)

    tokens = re.split(r"[^a-z0-9.]+", text)
    return [t for t in tokens if t and t not in _STOPWORDS and len(t) > 1]


def title_similarity(a_tokens: Sequence[str], b_tokens: Sequence[str]) -> float:
    """Jaccard overlap of two token MULTISETS, 0.0 to 1.0.

    Order is ignored on purpose: retailers reorder words ("Cleansing Balm
    125ml" vs "125ml Cleansing Balm") far more often than they use genuinely
    different ones.

    Repetition, however, is not ignored. Comparing plain sets scored
    "TOM FORD Noir Eau de Parfum" and "TOM FORD Noir De Noir Eau de Parfum"
    at a perfect 1.00 -- the duplicated "noir" and "de" collapse away, and
    the two are different fragrances. That match reported John Lewis as
    193.60 cheaper than Lookfantastic on the same product, which it is not.
    Counting occurrences keeps the repetition that distinguishes them.
    """
    a, b = Counter(a_tokens), Counter(b_tokens)
    if not a or not b:
        return 0.0
    intersection = sum((a & b).values())
    union = sum((a | b).values())
    return intersection / union if union else 0.0


def _offer(product: Dict[str, Any]) -> Dict[str, Any]:
    """The per-retailer facts worth carrying into a comparison row."""
    return {
        "retailer": product.get("retailer"),
        "product_title": product.get("product_title"),
        "product_url": product.get("product_url"),
        "current_price": product.get("current_price"),
        "price_is_from": bool(product.get("price_is_from")),
        "original_price": product.get("original_price"),
        "discount_percent": product.get("discount_percent"),
        "currency": product.get("currency"),
        "availability": product.get("availability"),
        "promotional_copy": product.get("promotional_copy"),
    }


def match_across_retailers(
    products: Sequence[Dict[str, Any]],
    threshold: float = 0.85,
) -> Tuple[List[ProductMatch], List[Dict[str, Any]]]:
    """Group products sold by more than one retailer.

    Returns (matches, unmatched). Products found at only one retailer are
    returned as unmatched rather than dropped -- "only Boots stocks this" is
    itself worth knowing.

    :param threshold: minimum title similarity, 0-1. Raising it produces
        fewer but safer matches.

        The default is deliberately strict. At 0.6, measured across 2,672
        real products from six retailers, it matched 439 products but paired
        a Lookfantastic listing at 28.00 with a Boots one at 125.00 as "the
        same" Jo Malone cologne. At 0.85 it finds 236, and they hold up --
        Advanced Night Repair 50ml at 52.20 (AllBeauty) against 89.00
        (Boots), scored 0.91. A wrong match invents a price gap, which is
        worse for this project than missing a real one.
    """
    buckets: Dict[Tuple[str, Optional[str]], List[Dict[str, Any]]] = {}
    for product in products:
        brand = (product.get("brand_matched_to") or product.get("brand") or "").casefold()
        size = extract_size(product.get("product_title") or "")
        buckets.setdefault((brand, size), []).append(product)

    matches: List[ProductMatch] = []
    unmatched: List[Dict[str, Any]] = []

    # explicit key: `size` is None for sizeless products, and None cannot
    # be compared against a string
    for (brand, size), bucket in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        # greedy: join a similar-enough cluster, or start a new one
        clusters: List[List[Dict[str, Any]]] = []
        token_cache = {
            id(p): normalize_title(p.get("product_title") or "",
                                   p.get("brand_matched_to") or p.get("brand"))
            for p in bucket
        }

        for product in bucket:
            placed = False
            for cluster in clusters:
                # a shop sells a product once, so two of its items in one
                # cluster means they are different products that read alike.
                # Merging them invented a 208.00 spread that did not exist.
                if any(other.get("retailer") == product.get("retailer")
                       for other in cluster):
                    continue

                score = min(
                    title_similarity(token_cache[id(product)], token_cache[id(other)])
                    for other in cluster
                )
                if score >= threshold:
                    cluster.append(product)
                    placed = True
                    break
            if not placed:
                clusters.append([product])

        for cluster in clusters:
            retailers = {p.get("retailer") for p in cluster}
            if len(retailers) < 2:
                unmatched.extend(cluster)
                continue

            # score on the weakest pair, so it is a floor not an average
            score = 1.0
            for i, a in enumerate(cluster):
                for b in cluster[i + 1:]:
                    score = min(score, title_similarity(token_cache[id(a)], token_cache[id(b)]))

            matches.append(ProductMatch(
                brand=cluster[0].get("brand_matched_to") or cluster[0].get("brand") or brand,
                title=min((p.get("product_title") or "" for p in cluster), key=len),
                size=size,
                score=score,
                offers=[_offer(p) for p in cluster],
            ))

    matches.sort(key=lambda m: (-(m.price_gap or 0), m.brand, m.title))
    return matches, unmatched
