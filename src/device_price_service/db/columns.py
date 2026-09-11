"""Common database timestamp columns without business model registration."""

from datetime import datetime

from sqlalchemy import func, text
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.orm import Mapped, mapped_column


def created_at_column() -> Mapped[datetime]:
    return mapped_column(
        DATETIME(fsp=3),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(
        DATETIME(fsp=3),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(3)"),
        onupdate=func.current_timestamp(),
    )
