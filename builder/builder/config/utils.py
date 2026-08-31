"""Shared helpers for the config module."""


def list_to_dict(list_):
    """Transform a list to a dict keyed by string-cast indices."""
    return {str(i): element for i, element in enumerate(list_)}
