from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256

from sqlalchemy import Engine, text


class LockNotAcquiredError(RuntimeError):
    """Raised when another process already owns the channel crawl lock."""


@contextmanager
def mysql_named_lock(engine: Engine, name: str) -> Iterator[None]:
    lock_name = _bounded_lock_name(name)
    with engine.connect() as connection:
        acquired = connection.scalar(
            text("SELECT GET_LOCK(:lock_name, 0)"), {"lock_name": lock_name}
        )
        if acquired != 1:
            raise LockNotAcquiredError(f"crawl lock is already held: {lock_name}")
        try:
            yield
        finally:
            connection.execute(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": lock_name})


def _bounded_lock_name(name: str) -> str:
    if len(name.encode("utf-8")) <= 64:
        return name
    digest = sha256(name.encode("utf-8")).hexdigest()
    return f"device-price:{digest[:51]}"
