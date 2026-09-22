from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import DealObservation


class SourceError(RuntimeError):
    pass


class DealSource(ABC):
    source_name = ""

    @abstractmethod
    def fetch(self) -> list[DealObservation]:
        raise NotImplementedError

    def revalidate(self, deal: DealObservation) -> DealObservation | None:
        """Return a fresh exact-item observation, or None when it cannot be verified."""
        return None
