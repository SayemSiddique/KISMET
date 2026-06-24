"""Unit and integration tests for src.cli."""


from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import src.cli as cli_mod
from src.cli import (
    CategoryEntry,
    ItemEntry,
    SessionConfig,
    _build_filename,
    _build_tree,
    app,
)
from src.llm import BrainstormResult, CategoryItem, OllamaConnectionError

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# _build_filename — new signature: (pattern, cat_slug, item_slug, index)
# ---------------------------------------------------------------------------

class TestBuildFilename:
    def test_item_index_pattern(self) -> None:
        name = _build_filename("[item]_[index]", "food", "samosa", 1)
        assert name == "samosa_01.jpg"

    def test_index_zero_padding(self) -> None:
        name = _build_filename("[item]_[index]", "food", "curry", 9)
        assert name == "curry_09.jpg"

    def test_custom_prefix_pattern(self) -> None:
        name = _build_filename("my_prefix_[index]", "any", "anything", 3)
        assert name == "my_prefix_03.jpg"

    def test_category_item_index_pattern(self) -> None:
        name = _build_filename("[category]_[item]_[index]", "cars", "bmw_m3", 2)
        assert name == "cars_bmw_m3_02.jpg"

    def test_no_literal_tokens_in_result(self) -> None:
        name = _build_filename("[item]_[index]", "cat", "burger", 5)
        assert "[item]" not in name
        assert "[index]" not in name

    def test_double_digit_index(self) -> None:
        name = _build_filename("[item]_[index]", "food", "taco", 10)
        assert name == "taco_10.jpg"


# ---------------------------------------------------------------------------
# _build_tree
# ---------------------------------------------------------------------------

class TestBuildTree:
    def _cfg(self, **overrides) -> SessionConfig:
        cats = overrides.pop("categories", [
            CategoryEntry("Samosa", "samosa", [
                ItemEntry("Crispy Samosa", "crispy_samosa"),
                ItemEntry("Veg Samosa", "veg_samosa"),
            ]),
            CategoryEntry("Curry", "curry", [ItemEntry("Butter Chicken", "butter_chicken")]),
        ])
        defaults: dict = dict(
            project_name="Indian street food",
            project_slug="indian_street_food",
            collection_scope="",
            visual_style="none",
            exclude_keywords="",
            categories=cats,
            image_count=3,
            save_dir=Path("/tmp/test_harvest/kismet_indian_street_food"),
            naming_pattern="[item]_[index]",
            custom_prefix=None,
        )
        defaults.update(overrides)
        return SessionConfig(**defaults)

    def test_returns_tree_object(self) -> None:
        from rich.tree import Tree
        assert isinstance(_build_tree(self._cfg()), Tree)

    def test_tree_label_contains_parent(self) -> None:
        cfg = self._cfg()
        assert str(cfg.save_dir.parent) in str(_build_tree(cfg).label)

    def test_tree_has_harvest_node(self) -> None:
        cfg = self._cfg()
        tree = _build_tree(cfg)
        assert len(tree.children) == 1
        assert cfg.save_dir.name in str(tree.children[0].label)

    def test_single_category_single_image(self) -> None:
        cfg = self._cfg(
            categories=[CategoryEntry("Cars", "cars", [ItemEntry("BMW M3", "bmw_m3")])],
            image_count=1,
        )
        tree = _build_tree(cfg)
        assert len(tree.children[0].children) >= 1

    def test_item_overflow_ellipsis(self) -> None:
        # A category with 5 items should show "more items" ellipsis (only 3 shown)
        items = [ItemEntry(f"Item {i}", f"item_{i}") for i in range(5)]
        cfg = self._cfg(categories=[CategoryEntry("Big", "big", items)], image_count=1)
        tree = _build_tree(cfg)
        cat_node = tree.children[0].children[0]
        labels = [str(c.label) for c in cat_node.children]
        assert any("more item" in lbl for lbl in labels)

    def test_no_ellipsis_within_item_limit(self) -> None:
        items = [ItemEntry(f"Item {i}", f"item_{i}") for i in range(2)]
        cfg = self._cfg(categories=[CategoryEntry("Small", "small", items)], image_count=1)
        tree = _build_tree(cfg)
        cat_node = tree.children[0].children[0]
        labels = [str(c.label) for c in cat_node.children]
        assert not any("more item" in lbl for lbl in labels)

    def test_image_overflow_ellipsis(self) -> None:
        cfg = self._cfg(
            categories=[CategoryEntry("Cars", "cars", [ItemEntry("BMW M3", "bmw_m3")])],
            image_count=5,
        )
        tree = _build_tree(cfg)
        item_node = tree.children[0].children[0].children[0]
        labels = [str(c.label) for c in item_node.children]
        assert any("more" in lbl for lbl in labels)


# ---------------------------------------------------------------------------
# CLI end-to-end via CliRunner
# New prompt sequence (8 steps + category/item loop):
#   1. Collection name
#   2. Collection scope (blank = skip)
#   3. Visual style (1–5)
#   4. Exclude keywords (blank = skip)
#   5. Save dir
#   6. Images per item
#   7. Naming pattern
#   8. Category loop: name, name, ..., blank
#      Per-category item loop: name, spec, name, spec, ..., blank
#   9. Confirm y/n
# ---------------------------------------------------------------------------

def _make_input(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _standard_inputs(dest: str, *, confirm: str = "y") -> str:
    return _make_input(
        "My Collection",   # Step 1: collection name
        "",                # Step 2: scope (skip)
        "1",               # Step 3: visual style (no preference)
        "",                # Step 4: exclude keywords (skip)
        dest,              # Step 5: save dir
        "2",               # Step 6: images per item
        "1",               # Step 7: naming ([item]_[index])
        "Appetizers",      # Step 8: category 1
        "",                # Step 8: blank = done with categories
        "Samosa",          # Items for Appetizers: item 1
        "",                # Item 1 spec (skip)
        "",                # Items: blank = done
        confirm,           # Confirm
    )


class TestCLIHappyPath:
    def test_happy_path_shows_harvest_report(self, tmp_path: Path) -> None:
        # After confirm, download runs against live internet (or fails gracefully).
        # Either way the Harvest Report table must appear.
        dest = str(tmp_path / "harvest")
        result = runner.invoke(app, input=_standard_inputs(dest, confirm="y"))
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Harvest Report" in result.output

    def test_happy_path_shows_tree_preview(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "harvest")
        result = runner.invoke(app, input=_standard_inputs(dest, confirm="n"))
        assert "Proposed Directory Structure" in result.output

    def test_happy_path_shows_summary_table(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "harvest")
        result = runner.invoke(app, input=_standard_inputs(dest, confirm="n"))
        assert "Session Summary" in result.output

    def test_happy_path_shows_query_preview(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "harvest")
        result = runner.invoke(app, input=_standard_inputs(dest, confirm="n"))
        assert "Effective Search Queries" in result.output

    def test_abort_at_confirmation(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "harvest")
        result = runner.invoke(app, input=_standard_inputs(dest, confirm="n"))
        assert result.exit_code == 0
        assert "Aborted" in result.output


class TestCLIValidation:
    def test_empty_goal_exits_with_error(self) -> None:
        result = runner.invoke(app, input=_make_input("", ""))
        assert result.exit_code == 1

    def test_naming_option_custom_prefix(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "harvest")
        user_input = _make_input(
            "My Collection", "", "1", "", dest, "1",
            "3",          # naming: custom prefix
            "myprefix",   # prefix value
            "Cars", "",   # 1 category, done
            "BMW M3", "", "",  # 1 item, no spec, done
            "n",
        )
        result = runner.invoke(app, input=user_input)
        assert result.exit_code == 0
        assert "Aborted" in result.output

    def test_invalid_image_count_loops(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "harvest")
        user_input = _make_input(
            "My Collection", "", "1", "", dest,
            "51",  # out of range
            "3",   # valid on retry
            "1",
            "Cars", "",
            "BMW M3", "", "",
            "n",
        )
        result = runner.invoke(app, input=user_input)
        assert result.exit_code == 0
        assert "between 1 and 50" in result.output

    def test_header_panel_displayed(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "harvest")
        result = runner.invoke(app, input=_standard_inputs(dest, confirm="n"))
        assert "KISMET" in result.output

    def test_step_labels_displayed(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "harvest")
        result = runner.invoke(app, input=_standard_inputs(dest, confirm="n"))
        for step in ("Step 1", "Step 2", "Step 3", "Step 4", "Step 5"):
            assert step in result.output

    def test_scope_included_in_query_preview(self, tmp_path: Path) -> None:
        dest = str(tmp_path / "harvest")
        user_input = _make_input(
            "Baby Clothes",          # name
            "baby girl 6-12 months", # scope
            "1", "",                 # style, exclude
            dest, "1", "1",
            "Froks", "",
            "Frok", "", "",
            "n",
        )
        result = runner.invoke(app, input=user_input)
        # The query preview should embed the scope
        assert "baby girl 6-12 months" in result.output


# ---------------------------------------------------------------------------
# AI assist (Step 1.5)
# ---------------------------------------------------------------------------

def _make_brainstorm_result() -> BrainstormResult:
    return BrainstormResult(
        total_expected_categories=2,
        items=[
            CategoryItem(display_name="Cars", search_query="modern cars", folder_slug="cars"),
            CategoryItem(display_name="Bikes", search_query="sport bikes", folder_slug="bikes"),
        ],
    )


class TestAIAssist:
    def test_ai_categories_used_when_accepted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_mod, "brainstorm_categories", lambda **_: _make_brainstorm_result())
        dest = str(tmp_path / "harvest")
        user_input = _make_input(
            "My Collection",  # Step 1: name
            "y",              # Step 1.5: accept AI suggestion
            "",               # Step 2: scope
            "1", "",          # Step 3, 4
            dest, "1", "1",   # Steps 5, 6, 7
            # Step 8 skipped because AI categories used; but items still needed per category
            # Cars items:
            "BMW", "", "",
            # Bikes items:
            "Yamaha", "", "",
            "n",              # confirm
        )
        result = runner.invoke(app, input=user_input)
        assert result.exit_code == 0
        assert "AI Category Suggest" in result.output
        assert "Cars" in result.output

    def test_ollama_unavailable_falls_through(self, tmp_path: Path) -> None:
        # conftest autouse fixture already patches brainstorm_categories to raise OllamaConnectionError
        dest = str(tmp_path / "harvest")
        result = runner.invoke(app, input=_standard_inputs(dest, confirm="n"))
        assert result.exit_code == 0
        assert "Connection to Ollama failed" in result.output
        # Manual categories step still shown
        assert "Step 8" in result.output

    def test_ai_suggestion_declined_falls_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_mod, "brainstorm_categories", lambda **_: _make_brainstorm_result())
        dest = str(tmp_path / "harvest")
        user_input = _make_input(
            "My Collection",  # name
            "n",              # Step 1.5: decline AI
            "",               # scope
            "1", "",
            dest, "1", "1",
            "Appetizers", "",
            "Samosa", "", "",
            "n",
        )
        result = runner.invoke(app, input=user_input)
        assert result.exit_code == 0
        assert "Step 8" in result.output
