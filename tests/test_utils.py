"""Unit tests for src.utils security controls."""


import pytest
from pathlib import Path

from src.utils import sanitize_slug, resolve_safe_path, validate_mime_type, build_search_query


# ---------------------------------------------------------------------------
# sanitize_slug
# ---------------------------------------------------------------------------

class TestSanitizeSlug:
    def test_basic_phrase(self) -> None:
        assert sanitize_slug("Samosa Chaat & Chutney!") == "samosa_chaat_chutney"

    def test_lowercasing(self) -> None:
        assert sanitize_slug("UPPER CASE") == "upper_case"

    def test_multiple_spaces_collapse(self) -> None:
        assert sanitize_slug("a   b") == "a_b"

    def test_leading_trailing_spaces(self) -> None:
        assert sanitize_slug("  spaces  ") == "spaces"

    def test_special_characters_removed(self) -> None:
        assert sanitize_slug("café & crème!") == "caf_crme"

    def test_numbers_preserved(self) -> None:
        assert sanitize_slug("Top 10 Dishes") == "top_10_dishes"

    def test_hyphens_preserved(self) -> None:
        assert sanitize_slug("iced-coffee") == "iced-coffee"

    def test_consecutive_separators_collapsed(self) -> None:
        assert sanitize_slug("foo--bar__baz") == "foo_bar_baz"

    def test_empty_string(self) -> None:
        assert sanitize_slug("") == ""

    def test_only_special_chars(self) -> None:
        assert sanitize_slug("!@#$%^&*()") == ""

    def test_numeric_only(self) -> None:
        assert sanitize_slug("42") == "42"


# ---------------------------------------------------------------------------
# resolve_safe_path
# ---------------------------------------------------------------------------

class TestResolveSafePath:
    def test_valid_subdirectory(self, tmp_path: Path) -> None:
        result = resolve_safe_path(tmp_path, "my_downloads/cats")
        assert result == (tmp_path / "my_downloads" / "cats").resolve()

    def test_returns_path_object(self, tmp_path: Path) -> None:
        result = resolve_safe_path(tmp_path, "subdir")
        assert isinstance(result, Path)

    def test_accepts_string_base(self, tmp_path: Path) -> None:
        result = resolve_safe_path(str(tmp_path), "subdir")
        assert result.is_relative_to(tmp_path)

    def test_traversal_double_dot(self, tmp_path: Path) -> None:
        with pytest.raises(PermissionError):
            resolve_safe_path(tmp_path, "../../etc/passwd")

    def test_traversal_absolute_escape(self, tmp_path: Path) -> None:
        with pytest.raises(PermissionError):
            resolve_safe_path(tmp_path, "/etc/passwd")

    def test_traversal_deep_escape(self, tmp_path: Path) -> None:
        with pytest.raises(PermissionError):
            resolve_safe_path(tmp_path, "a/b/c/../../../../../../../etc")

    def test_system_root_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(PermissionError):
            resolve_safe_path("/", "etc/passwd")

    def test_usr_root_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(PermissionError):
            resolve_safe_path("/usr", "local/bin")

    def test_nested_valid_path(self, tmp_path: Path) -> None:
        result = resolve_safe_path(tmp_path, "a/b/c/d")
        assert str(result).startswith(str(tmp_path))

    def test_same_as_base_allowed(self, tmp_path: Path) -> None:
        result = resolve_safe_path(tmp_path, ".")
        assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# validate_mime_type
# ---------------------------------------------------------------------------

class TestValidateMimeType:
    @pytest.mark.parametrize("mime", [
        "image/jpeg",
        "image/png",
        "image/webp",
    ])
    def test_approved_types_accepted(self, mime: str) -> None:
        assert validate_mime_type(mime) is True

    def test_case_insensitive(self) -> None:
        assert validate_mime_type("IMAGE/JPEG") is True
        assert validate_mime_type("Image/PNG") is True

    def test_strips_parameters(self) -> None:
        assert validate_mime_type("image/jpeg; charset=utf-8") is True

    def test_strips_quality_parameter(self) -> None:
        assert validate_mime_type("image/webp;q=0.9") is True

    @pytest.mark.parametrize("mime", [
        "text/html",
        "application/octet-stream",
        "application/javascript",
        "text/plain",
        "image/gif",
        "image/svg+xml",
        "image/tiff",
        "",
        "multipart/form-data",
    ])
    def test_rejected_types_blocked(self, mime: str) -> None:
        assert validate_mime_type(mime) is False

    def test_partial_match_rejected(self) -> None:
        assert validate_mime_type("text/image/jpeg") is False

    def test_empty_header(self) -> None:
        assert validate_mime_type("") is False


# ---------------------------------------------------------------------------
# build_search_query
# ---------------------------------------------------------------------------

class TestBuildSearchQuery:
    def test_item_only(self) -> None:
        q = build_search_query("Frok")
        assert q.startswith("Frok")
        assert "-watermark" in q

    def test_scope_prepended(self) -> None:
        q = build_search_query("Frok", collection_scope="baby girl clothing")
        assert q.startswith("baby girl clothing Frok")

    def test_spec_appended(self) -> None:
        q = build_search_query("Frok", item_spec="pastel colors front view")
        assert "Frok pastel colors front view" in q

    def test_style_appended(self) -> None:
        q = build_search_query("Frok", style_suffix="product photography white background")
        assert "Frok product photography white background" in q

    def test_full_combination(self) -> None:
        q = build_search_query(
            "Frok",
            collection_scope="baby girl clothing 6-12 months",
            item_spec="pastel colors front view",
            style_suffix="product photography white background",
        )
        assert "baby girl clothing 6-12 months Frok pastel colors front view product photography white background" in q

    def test_exclude_keywords_become_negative_terms(self) -> None:
        q = build_search_query("Frok", exclude_keywords="cartoon, watermark")
        assert "-cartoon" in q
        assert "-watermark" in q

    def test_empty_exclude_keywords_ignored(self) -> None:
        # Anti-watermark terms are always present; user-supplied negatives are not added
        q = build_search_query("Frok", exclude_keywords="")
        assert "-cartoon" not in q
        assert "-watermark" in q  # from built-in anti-watermark filter

    def test_exclude_strips_whitespace(self) -> None:
        q = build_search_query("BMW M3", exclude_keywords="  cartoon ,  watermark  ")
        assert "-cartoon" in q
        assert "-watermark" in q

    def test_empty_parts_skipped(self) -> None:
        q = build_search_query("BMW M3", collection_scope="", item_spec="", style_suffix="")
        assert q.startswith("BMW M3")
        assert "-watermark" in q

    def test_strips_leading_trailing_whitespace(self) -> None:
        q = build_search_query("  BMW M3  ", collection_scope="  cars  ")
        assert not q.startswith(" ")
        assert not q.endswith(" ")
