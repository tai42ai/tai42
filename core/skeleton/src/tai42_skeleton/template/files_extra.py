"""The skeleton's ``files`` extra: whether its packages are installed, and the refusal when they are not.

The content loader (:mod:`tai42_skeleton.template.file_loading`) imports these packages
when it is imported and refuses with :func:`files_not_configured` when one is missing.
:func:`files_extra_installed` answers the same question by locating the packages without
importing them, so a process that only reports the extra's presence loads none of them.
"""

from __future__ import annotations

from importlib.util import find_spec

from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.template.errors import FILES_NOT_CONFIGURED_CODE

# The top-level modules of the ``files`` extra that the content loader imports.
FILES_EXTRA_MODULES = ("bs4", "ebooklib", "langchain_community", "magika", "odf", "pandas", "pptx", "striprtf")


def files_not_configured() -> NotSupportedError:
    """The ``501`` refusal every content load answers without the ``files`` extra; its message is the install line."""
    return NotSupportedError(
        "tai42-skeleton 'files' resource loading requires the 'files' optional "
        "dependencies (magika, pandas, langchain-community, pypdf, docx2txt, "
        "openpyxl, tabulate, beautifulsoup4, python-pptx, striprtf, odfpy, "
        "ebooklib). Install them with: pip install 'tai42-skeleton[files]'",
        extra={"code": FILES_NOT_CONFIGURED_CODE},
    )


def files_extra_installed() -> bool:
    """Whether every module of the ``files`` extra the content loader imports can be found, importing none of them."""
    return all(find_spec(name) is not None for name in FILES_EXTRA_MODULES)


__all__ = ["FILES_EXTRA_MODULES", "files_extra_installed", "files_not_configured"]
