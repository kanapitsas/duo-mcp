"""Invoice maths. Amounts are in euros; the spec says: round half up to the cent."""


def line_total(unit_price: float, qty: int) -> float:
    return round(unit_price * qty, 2)


def invoice_total(lines: list[tuple[float, int]]) -> float:
    return round(sum(line_total(p, q) for p, q in lines), 2)
