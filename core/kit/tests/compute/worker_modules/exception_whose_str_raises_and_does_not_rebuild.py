"""A job raising an exception whose ``str`` raises and whose pickle does not rebuild it."""


class UnprintableTwoArgError(Exception):
    def __init__(self, code: int, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code

    def __str__(self) -> str:
        raise ValueError("str broken")


def raise_unprintable() -> None:
    raise UnprintableTwoArgError(7, "bad")
