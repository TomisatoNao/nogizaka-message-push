"""Task-local receipt policy for staged durable member delivery."""
import contextvars
from contextlib import contextmanager

_durable_receipts = contextvars.ContextVar("durable_delivery_receipts", default=False)


def durable_receipts_enabled():
    return _durable_receipts.get()


@contextmanager
def durable_delivery_receipts(enabled=True):
    token = _durable_receipts.set(enabled)
    try:
        yield
    finally:
        _durable_receipts.reset(token)
