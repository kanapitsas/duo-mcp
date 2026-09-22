import functools


def memoize(fn):
    """Cache results of a pure function by its positional arguments."""
    cache = {}

    @functools.wraps(fn)
    def wrapper(*args):
        if args not in cache:
            cache[args] = fn(*args)
        return cache[args]

    return wrapper
