"""Small helpers ported from ``readthedocs.core.utils``."""

_DEFAULT = object()


def get_dotted_attribute(obj, attribute, default=_DEFAULT):
    """
    Resolve a nested attribute on ``obj`` using dot notation.

    Behaves like ``getattr`` but walks through nested attributes. If the
    attribute is missing and a ``default`` was provided, ``default`` is
    returned; otherwise ``AttributeError`` is raised.
    """
    for attr in attribute.split("."):
        if hasattr(obj, attr):
            obj = getattr(obj, attr)
        elif default is not _DEFAULT:
            return default
        else:
            raise AttributeError(f"Object {obj} has no attribute {attr}")
    return obj
