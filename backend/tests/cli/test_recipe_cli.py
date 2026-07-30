from __future__ import annotations

import json

import pytest

from app.cli.main import main


def test_recipe_list_json_is_stable_and_sorted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["recipe", "list", "--json"]) == 0
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)

    assert envelope["command"] == "recipe list"
    assert envelope["data"]["recipes"] == [
        {
            "display_name": "Video course note",
            "input_kinds": ["source"],
            "output_kinds": ["knowledge-note"],
            "recipe_id": "alltonote.video-course-note",
            "recipe_version": 1,
        },
        {
            "display_name": "Video producer",
            "input_kinds": ["source"],
            "output_kinds": ["knowledge-note", "faithful-edition"],
            "recipe_id": "alltonote.video-producer",
            "recipe_version": 2,
        },
    ]
    assert captured.err == ""
    assert captured.out.count("\n") == 1


def test_recipe_describe_json_is_stable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["recipe", "describe", "alltonote.video-producer@2", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["command"] == "recipe describe"
    assert envelope["data"] == {
        "display_name": "Video producer",
        "input_kinds": ["source"],
        "output_kinds": ["knowledge-note", "faithful-edition"],
        "recipe_id": "alltonote.video-producer",
        "recipe_version": 2,
    }


@pytest.mark.parametrize(
    ("selector", "code"),
    (
        ("invalid", "recipe_selector_invalid"),
        ("unknown.recipe@1", "recipe_not_found"),
        ("alltonote.video-producer@1", "recipe_version_not_found"),
    ),
)
def test_recipe_describe_errors_are_stable(
    selector: str,
    code: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["recipe", "describe", selector, "--json"]) == 2
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["error"]["code"] == code
