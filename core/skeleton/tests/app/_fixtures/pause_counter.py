"""The run counter of the pause fixture tools, kept apart from the module that registers them."""

from collections import Counter

BODY_RUNS: Counter[str] = Counter()
