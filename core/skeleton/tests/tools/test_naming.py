"""The served-name rule lives in a leaf module a consumer imports without loading the binding engine."""

from __future__ import annotations

import subprocess
import sys

from tai42_skeleton.tools.binding import ToolBinding
from tai42_skeleton.tools.naming import normalized_name


def test_a_mixed_case_upstream_name_is_served_lower_cased_under_its_prefix():
    assert normalized_name("acme_docs_a1", "getAccessibleThings") == "acme_docs_a1_getaccessiblethings"
    assert normalized_name("Acme-Docs", "list-things") == "acme_docs_list_things"


def test_the_binding_engine_names_tools_by_the_same_rule():
    assert ToolBinding.normalized_name("acme_docs_a1", "getAccessibleThings") == normalized_name(
        "acme_docs_a1", "getAccessibleThings"
    )


def test_the_rule_imports_first_in_a_fresh_interpreter():
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "from tai42_skeleton.tools.naming import normalized_name; print(normalized_name('p', 'X'))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (done.returncode, done.stdout.strip()) == (0, "p_x"), done.stderr
