"""The skeleton's ``files`` extra: whether its packages are installed, and the refusal when they are not.

The content loader (:mod:`tai42_skeleton.template.file_loading`) imports these packages
when it is imported and refuses with :func:`files_not_configured` when one is missing.
:func:`files_extra_installed` answers the same question by locating the packages without
importing them, so a process that only reports the extra's presence loads none of them.
"""

from __future__ import annotations

from importlib.util import find_spec

from tai42_skeleton.template.errors import FilesNotConfiguredError

# The top-level modules of the ``files`` extra that the content loader imports.
FILES_EXTRA_MODULES = ("bs4", "ebooklib", "langchain_community", "magika", "odf", "pandas", "pptx", "striprtf")


#: The install line a content load answers with when the ``files`` extra is missing.
FILES_INSTALL_LINE = (
    "tai42-skeleton 'files' resource loading requires the 'files' optional "
    "dependencies (magika, pandas, langchain-community, pypdf, docx2txt, "
    "openpyxl, tabulate, beautifulsoup4, python-pptx, striprtf, odfpy, "
    "ebooklib). Install them with: pip install 'tai42-skeleton[files]'"
)


def files_not_configured() -> FilesNotConfiguredError:
    """The refusal every content load answers without the ``files`` extra; its message is the install line."""
    return FilesNotConfiguredError(FILES_INSTALL_LINE)


def files_extra_installed() -> bool:
    """Whether every module of the ``files`` extra the content loader imports can be found, importing none of them."""
    return all(find_spec(name) is not None for name in FILES_EXTRA_MODULES)


__all__ = ["FILES_EXTRA_MODULES", "FILES_INSTALL_LINE", "files_extra_installed", "files_not_configured"]
