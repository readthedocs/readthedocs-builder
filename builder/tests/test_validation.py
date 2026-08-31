"""
Tests for the validation helpers.

Ported from ``readthedocs/config/tests/test_validation.py``. ``validate_dict``
and ``validate_path_pattern`` have no upstream tests but are exercised by the
parser, so they're covered here.
"""

from pytest import raises

from builder.config.exceptions import ConfigValidationError
from builder.config.validation import validate_bool
from builder.config.validation import validate_choice
from builder.config.validation import validate_dict
from builder.config.validation import validate_list
from builder.config.validation import validate_path
from builder.config.validation import validate_path_pattern
from builder.config.validation import validate_string


class TestValidateBool:
    def test_it_accepts_true(self):
        assert validate_bool(True) is True

    def test_it_accepts_false(self):
        assert validate_bool(False) is False

    def test_it_accepts_0(self):
        assert validate_bool(0) is False

    def test_it_accepts_1(self):
        assert validate_bool(1) is True

    def test_it_fails_on_string(self):
        with raises(ConfigValidationError) as excinfo:
            validate_bool("random string")
        assert excinfo.value.message_id == ConfigValidationError.INVALID_BOOL


class TestValidateChoice:
    def test_it_accepts_valid_choice(self):
        result = validate_choice("choice", ("choice", "another_choice"))
        assert result == "choice"

    def test_it_rejects_a_string_as_the_choices_container(self):
        # A string is not a valid list of choices, even when the value is one
        # of its characters.
        with raises(ConfigValidationError) as excinfo:
            validate_choice("c", "abc")
        assert excinfo.value.message_id == ConfigValidationError.INVALID_LIST

    def test_it_rejects_invalid_choice(self):
        with raises(ConfigValidationError) as excinfo:
            validate_choice("not-a-choice", ("choice", "another_choice"))
        assert excinfo.value.message_id == ConfigValidationError.INVALID_CHOICE


class TestValidateDict:
    def test_it_accepts_a_dict(self):
        assert validate_dict({"key": "value"}) is None

    def test_it_rejects_a_list(self):
        with raises(ConfigValidationError) as excinfo:
            validate_dict(["key"])
        assert excinfo.value.message_id == ConfigValidationError.INVALID_DICT

    def test_it_rejects_a_string(self):
        with raises(ConfigValidationError) as excinfo:
            validate_dict("key")
        assert excinfo.value.message_id == ConfigValidationError.INVALID_DICT


class TestValidateList:
    def test_it_accepts_a_list(self):
        assert validate_list(["choice", "another_choice"]) == ["choice", "another_choice"]

    def test_it_accepts_a_tuple(self):
        assert validate_list(("choice", "another_choice")) == ["choice", "another_choice"]

    def test_it_accepts_a_generator(self):
        def iterator():
            yield "choice"

        assert validate_list(iterator()) == ["choice"]

    def test_it_rejects_string_types(self):
        with raises(ConfigValidationError) as excinfo:
            validate_list("choice")
        assert excinfo.value.message_id == ConfigValidationError.INVALID_LIST

    def test_it_rejects_dict_types(self):
        with raises(ConfigValidationError) as excinfo:
            validate_list({"key": "value"})
        assert excinfo.value.message_id == ConfigValidationError.INVALID_LIST

    def test_it_rejects_non_iterables(self):
        with raises(ConfigValidationError) as excinfo:
            validate_list(123)
        assert excinfo.value.message_id == ConfigValidationError.INVALID_LIST


class TestValidatePath:
    def test_it_accepts_relative_path(self, tmpdir):
        tmpdir.mkdir("a directory")
        validate_path("a directory", str(tmpdir))

    def test_it_accepts_files(self, tmpdir):
        tmpdir.join("file").write("content")
        validate_path("file", str(tmpdir))

    def test_it_accepts_absolute_path(self, tmpdir):
        path = str(tmpdir.mkdir("a directory"))
        validate_path(path, "does not matter")

    def test_it_returns_relative_path(self, tmpdir):
        tmpdir.mkdir("a directory")
        path = validate_path("a directory", str(tmpdir))
        assert path == "a directory"

    def test_it_only_accepts_strings(self):
        with raises(ConfigValidationError) as excinfo:
            validate_path(None, "")
        assert excinfo.value.message_id == ConfigValidationError.INVALID_STRING

    def test_it_rejects_an_empty_string(self):
        with raises(ConfigValidationError) as excinfo:
            validate_path("", "does not matter")
        assert excinfo.value.message_id == ConfigValidationError.INVALID_PATH


class TestValidatePathPattern:
    def test_it_strips_the_leading_slash(self):
        assert validate_path_pattern("/api/index.html") == "api/index.html"

    def test_it_accepts_a_relative_pattern(self):
        assert validate_path_pattern("api/index.html") == "api/index.html"

    def test_it_collapses_repeated_slashes(self):
        assert validate_path_pattern("//api///index.html") == "api/index.html"

    def test_it_normalizes_relative_segments(self):
        assert validate_path_pattern("api/../index.html") == "index.html"

    def test_it_rejects_a_pattern_that_escapes_the_root(self):
        # Normalizing "/../.." lands back at "/", which leaves nothing to match.
        with raises(ConfigValidationError) as excinfo:
            validate_path_pattern("../..")
        assert excinfo.value.message_id == ConfigValidationError.INVALID_PATH_PATTERN

    def test_it_only_accepts_strings(self):
        with raises(ConfigValidationError) as excinfo:
            validate_path_pattern(None)
        assert excinfo.value.message_id == ConfigValidationError.INVALID_STRING


class TestValidateString:
    def test_it_accepts_unicode(self):
        result = validate_string("Unicöde")
        assert isinstance(result, str)

    def test_it_accepts_nonunicode(self):
        result = validate_string("Unicode")
        assert isinstance(result, str)

    def test_it_rejects_float(self):
        with raises(ConfigValidationError) as excinfo:
            validate_string(123.456)
        assert excinfo.value.message_id == ConfigValidationError.INVALID_STRING

    def test_it_rejects_none(self):
        with raises(ConfigValidationError) as excinfo:
            validate_string(None)
        assert excinfo.value.message_id == ConfigValidationError.INVALID_STRING
