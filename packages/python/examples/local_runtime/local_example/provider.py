from typeflux.testing import FakeProvider

from .models import Value


class OfflineProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__([Value(text="local result")])
