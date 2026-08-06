from build_visual_review import group_id
from build_cleanup_plan import keeper_rank
from build_ai_cleanup import actions_for_groups
from enrich_locations import decimal_degrees
from review_server import ALLOWED_DECISIONS, confirm_review, initialise_database, save_decision


def test_group_id_is_stable_when_component_order_changes() -> None:
    items = [
        {"relative_path": "zebra.jpg"},
        {"relative_path": "apple.jpg"},
    ]
    assert group_id(items, [0, 1]) == group_id(items, [1, 0])


def test_destructive_label_is_only_an_allowed_review_decision() -> None:
    assert "delete_all_but_one" in ALLOWED_DECISIONS
    assert "delete_group" in ALLOWED_DECISIONS


def test_confirmation_requires_saved_choices_for_every_group(tmp_path) -> None:
    database = tmp_path / "catalogue.db"
    initialise_database(database)
    first_group = "0123456789abcdef"
    second_group = "fedcba9876543210"
    save_decision(database, 1, first_group, "keep_all", "")
    try:
        confirm_review(database, 1, [first_group, second_group])
    except ValueError as error:
        assert "Every group" in str(error)
    else:
        raise AssertionError("Confirmation should require every group decision.")
    save_decision(database, 1, second_group, "review_later", "")
    assert confirm_review(database, 1, [first_group, second_group])["group_count"] == 2


def test_keeper_rank_prefers_resolution_then_size() -> None:
    small = {"relative_path": "small.jpg", "width": 100, "height": 100, "size_bytes": 1_000_000}
    large = {"relative_path": "large.jpg", "width": 200, "height": 100, "size_bytes": 100_000}
    same_resolution_larger = {"relative_path": "same.jpg", "width": 200, "height": 100, "size_bytes": 200_000}
    assert min([small, large], key=keeper_rank) is large
    assert min([large, same_resolution_larger], key=keeper_rank) is same_resolution_larger


def test_adjusted_cleanup_can_keep_all_or_select_another_keeper() -> None:
    group = {
        "id": "visual-test",
        "kind": "high-confidence visual match",
        "recommended_keeper": "/library/first.jpg",
        "members": [
            {"path": "/library/first.jpg", "relative_path": "first.jpg", "size_bytes": 10, "sha256": "a"},
            {"path": "/library/second.jpg", "relative_path": "second.jpg", "size_bytes": 9, "sha256": "b"},
        ],
    }
    assert actions_for_groups([group], {"visual-test": "keep_all"}) == []
    actions = actions_for_groups([group], {"visual-test": "/library/second.jpg"})
    assert [action["source_path"] for action in actions] == ["/library/first.jpg"]
    actions = actions_for_groups([group], {"visual-test": "delete_all"})
    assert [action["source_path"] for action in actions] == ["/library/first.jpg", "/library/second.jpg"]


def test_gps_dms_conversion_handles_hemispheres() -> None:
    assert decimal_degrees((48, 8, 0), "N") == 48 + 8 / 60
    assert decimal_degrees((17, 6, 0), "W") == -(17 + 6 / 60)
