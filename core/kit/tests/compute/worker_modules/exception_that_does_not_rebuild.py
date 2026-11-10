"""A job raising an exception whose pickle does not rebuild it: its constructor takes two arguments."""


class TwoArgError(Exception):
    def __init__(self, code: int, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def raise_two_arg() -> None:
    raise TwoArgError(7, "bad")
