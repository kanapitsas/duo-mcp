"""Price catalogue. Prices are edited at runtime by the admin UI via set_price()."""

from util import memoize

_PRICES = {"apple": 1.00, "pear": 1.50, "plum": 0.80}


def set_price(item: str, price: float) -> None:
    _PRICES[item] = price


@memoize
def price_of(item: str) -> float:
    return _PRICES[item]
