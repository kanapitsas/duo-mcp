from pricing import price_of


class Cart:
    def __init__(self, lines=[]):
        self.lines = lines

    def add(self, item: str, qty: int = 1) -> None:
        self.lines.append((item, qty))

    def total(self) -> float:
        return round(sum(price_of(item) * qty for item, qty in self.lines), 2)
