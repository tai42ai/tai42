"""A compute job that evaluates a round of jq programs in a worker and reports the worker's resident memory."""

from tai42_kit.utils.data.jq_worker import JqSource, compile_and_evaluate_job


def resident_kib() -> int:
    """This process's resident set size in KiB, from procfs."""
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    raise RuntimeError("/proc/self/status has no VmRSS line")


def evaluate_round(sources: list[str], input_text: str) -> int:
    """Compile and evaluate each of ``sources`` over the envelope ``input_text``; the worker's resident KiB after."""
    for source in sources:
        compile_and_evaluate_job(source, JqSource(source, "", ()), input_text)
    return resident_kib()
