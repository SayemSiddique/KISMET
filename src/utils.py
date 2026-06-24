"""Security utilities: slug sanitization, safe path resolution, MIME validation."""

from __future__ import annotations

import re
from pathlib import Path

_ALLOWED_MIME_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})

# Paths that must never be used as a download base directory.
# Note: Path("/") is checked explicitly so that every absolute sub-path
# (which is technically relative to /) doesn't cause false positives.
_FORBIDDEN_ROOTS: tuple[Path, ...] = (
    Path("/usr"),
    Path("/etc"),
    Path("/bin"),
    Path("/sbin"),
    Path("/System"),
    Path("/Library"),
    Path("/private/etc"),
    Path("/private/var/root"),
)


def sanitize_slug(raw_text: str) -> str:
    """Return a safe snake_case slug from arbitrary user-supplied text.

    Steps: lowercase → collapse whitespace to underscores →
    strip non-alphanumeric/underscore/hyphen → collapse repeated
    separators → strip leading/trailing separators.
    """
    lowered = raw_text.lower()
    underscored = re.sub(r"\s+", "_", lowered)
    cleaned = re.sub(r"[^a-z0-9_-]", "", underscored)
    collapsed = re.sub(r"[_-]{2,}", "_", cleaned)
    return collapsed.strip("_-")


def resolve_safe_path(base_dir: str | Path, relative_dir: str | Path) -> Path:
    """Resolve *relative_dir* inside *base_dir*, blocking directory traversal.

    Raises:
        PermissionError: if the resolved target escapes *base_dir* or
            lands inside a protected system root.
    """
    base = Path(base_dir).resolve()
    target = (base / relative_dir).resolve()

    if not target.is_relative_to(base):
        raise PermissionError(
            f"Directory traversal blocked: '{target}' escapes base '{base}'."
        )

    if base == Path("/"):
        raise PermissionError("Writing to the filesystem root '/' is not permitted.")

    for forbidden in _FORBIDDEN_ROOTS:
        if base.is_relative_to(forbidden):
            raise PermissionError(
                f"Base directory '{base}' is inside protected system location '{forbidden}'."
            )

    return target


STYLE_MAP: dict[str, str] = {
    "product":      "product photography white background",
    "lifestyle":    "lifestyle photography",
    "editorial":    "editorial photo",
    "illustration": "illustration vector art",
    "none":         "",
}

DDG_TYPE_MAP: dict[str, str] = {
    "product":      "itp:photo",
    "lifestyle":    "itp:photo",
    "editorial":    "itp:photo",
    "illustration": "itp:clipart",
    "none":         "",
}


def build_search_query(
    item_display: str,
    collection_scope: str = "",
    item_spec: str = "",
    style_suffix: str = "",
    exclude_keywords: str = "",
) -> str:
    """Compose the final DuckDuckGo query from layered user inputs.

    Order: [scope] [item] [spec] [style] [-exclude -terms]
    Empty parts are omitted so the query stays clean.
    """
    parts = [p.strip() for p in [collection_scope, item_display, item_spec, style_suffix] if p.strip()]
    query = " ".join(parts)
    if exclude_keywords:
        neg = " ".join(f"-{kw.strip()}" for kw in exclude_keywords.split(",") if kw.strip())
        if neg:
            query = f"{query} {neg}"
    return query.strip()


def build_stem(pattern: str, cat_slug: str, item_slug: str, index: int) -> str:
    """Expand a naming pattern into a file stem (no extension)."""
    name = pattern
    name = name.replace("[category]", cat_slug)
    name = name.replace("[item]", item_slug)
    name = name.replace("[index]", f"{index:02d}")
    return name


def validate_mime_type(content_type_header: str) -> bool:
    """Return True only when *content_type_header* declares an approved image MIME type.

    Parameters are stripped (e.g. 'image/jpeg; charset=utf-8' → 'image/jpeg')
    and comparison is case-insensitive.
    """
    primary = content_type_header.split(";")[0].strip().lower()
    return primary in _ALLOWED_MIME_TYPES
