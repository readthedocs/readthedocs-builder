"""
Lookup table mapping builder identifiers to backend classes.

Ported from ``readthedocs.doc_builder.loader``. Upstream uses
``importlib.import_module(settings.SPHINX_BACKEND)`` with Django settings to
allow swapping backends; the runner hardcodes the canonical paths.
"""

from builder.backends import mkdocs as mkdocs_backend
from builder.backends import sphinx as sphinx_backend


BUILDER_BY_NAME = {
    # Sphinx HTML variants.
    "sphinx": sphinx_backend.HtmlBuilder,
    "sphinx_htmldir": sphinx_backend.HtmlDirBuilder,
    "sphinx_singlehtml": sphinx_backend.SingleHtmlBuilder,
    # Other Sphinx outputs.
    "sphinx_pdf": sphinx_backend.PdfBuilder,
    "sphinx_epub": sphinx_backend.EpubBuilder,
    "sphinx_singlehtmllocalmedia": sphinx_backend.LocalMediaBuilder,
    # MkDocs.
    "mkdocs": mkdocs_backend.MkdocsHTML,
}


def get_builder_class(name):
    return BUILDER_BY_NAME[name]
