"""Matching the same product across different retailers."""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
        """Offers whose price means the same thing."""
        priced = [
            o for o in self.offers
            if o.get("current_price") is not None and not o.get("price_is_from")
        ]
        if not priced:
            return []

        common = Counter(
            o.get("currency") for o in priced if o.get("currency")
        ).most_common(1)
        if not common:
            return priced
        return [o for o in priced if o.get("currency") == common[0][0]]

    @property
    def currency_mismatch(self) -> bool:
        """True when the offers are not all priced in the same currency."""
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
        """True when the retailers disagree about this product's RRP."""
        rrps = [o["original_price"] for o in self.offers
                if o.get("original_price") is not None]
        if len(rrps) >= 2 and max(rrps) > min(rrps) * 1.25:
            return True

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
    """Return a normalised size key for a title, or None if it states no size."""
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
    """Reduce a title to comparable tokens."""
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
    """Jaccard overlap of two token MULTISETS, 0.0 to 1.0."""
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
    """Group products sold by more than one retailer."""
    buckets: Dict[Tuple[str, Optional[str]], List[Dict[str, Any]]] = {}
    for product in products:
        brand = (product.get("brand_matched_to") or product.get("brand") or "").casefold()
        size = extract_size(product.get("product_title") or "")
        buckets.setdefault((brand, size), []).append(product)

    matches: List[ProductMatch] = []
    unmatched: List[Dict[str, Any]] = []

    for (brand, size), bucket in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        clusters: List[List[Dict[str, Any]]] = []
        token_cache = {
            id(p): normalize_title(p.get("product_title") or "",
                                   p.get("brand_matched_to") or p.get("brand"))
            for p in bucket
        }

        for product in bucket:
            placed = False
            for cluster in clusters:
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
