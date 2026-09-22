from cart import Cart
from pricing import set_price


def test_basic_total():
    cart = Cart()
    cart.add("apple", 2)
    assert cart.total() == 2.0


def test_new_cart_sees_updated_price():
    set_price("apple", 2.5)
    cart = Cart()
    cart.add("apple", 1)
    assert cart.total() == 2.5
