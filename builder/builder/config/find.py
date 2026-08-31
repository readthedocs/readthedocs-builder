"""File-system helpers for locating the config file."""

import os
import re


def find_one(path, filename_regex):
    """Return the absolute path of the first file in ``path`` matching ``filename_regex``."""
    _path = os.path.abspath(path)
    for filename in os.listdir(_path):
        if re.match(filename_regex, filename):
            return os.path.join(_path, filename)
    return ""
