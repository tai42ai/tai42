"""The Redis checkpoint saver version the kit's codec and sweep were proven on."""

from importlib.metadata import version


def test_the_redis_saver_is_the_proven_version():
    assert version("langgraph-checkpoint-redis") == "0.5.2", (
        "re-run the checkpoint codec round trip, the adelete_thread cap check and the metadata-merge check "
        "before moving the Redis saver pin"
    )
