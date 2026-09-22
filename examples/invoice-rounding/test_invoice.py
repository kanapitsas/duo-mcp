from invoice import invoice_total, line_total


def test_simple_line():
    assert line_total(1.10, 3) == 3.30


def test_half_cent_rounds_up():
    assert line_total(2.675, 1) == 2.68


def test_invoice_total():
    assert invoice_total([(0.125, 1), (1.00, 2)]) == 2.13
