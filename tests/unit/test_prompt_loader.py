"""Tests for the prompt loader (B2-03), using throw-away prompt directories."""

import re
from decimal import Decimal
from pathlib import Path

import pytest
from app.agents.llm import Message
from app.agents.prompts import PromptError, list_prompts, load_prompt
from app.agents.prompts.loader import FEEDBACK_FOOTER, FEEDBACK_HEADING
from pydantic import BaseModel

VALID = """\
---
prompt_version: demo.v1
---
# Role
You are a demo agent.

# Task
Do the demo for {{topic}}.

# Inputs
- Topic: {{topic}}
- Language: {{language}}

# Output format
Reply with JSON:

```json
{"a": {"b": 1}}
```

# Rules
- Be brief.
"""


def write(directory: Path, name: str, text: str) -> None:
    (directory / f"{name}.md").write_text(text, encoding="utf-8")


def add_fragment(
    directory: Path, name: str = "safety_rules", text: str = "SAFE"
) -> None:
    fragments = directory / "fragments"
    fragments.mkdir(exist_ok=True)
    (fragments / f"{name}.md").write_text(text, encoding="utf-8")


@pytest.fixture
def prompts(tmp_path: Path) -> Path:
    return tmp_path


def load(prompts: Path, name: str = "demo"):  # type: ignore[no-untyped-def]
    return load_prompt(name, directory=prompts)


def expect_error(prompts: Path, text: str, code: str = "prompt_invalid") -> PromptError:
    write(prompts, "demo", text)
    with pytest.raises(PromptError) as err:
        load(prompts)
    assert err.value.code == code
    return err.value


# --------------------------------------------------------------------------
# Loading a valid prompt
# --------------------------------------------------------------------------


def test_valid_prompt_loads(prompts: Path) -> None:
    write(prompts, "demo", VALID)
    template = load(prompts)
    assert template.name == "demo"
    assert template.version == "demo.v1"
    assert [title for title, _ in template.sections] == [
        "Role",
        "Task",
        "Inputs",
        "Output format",
        "Rules",
    ]
    assert template.variables == {"topic", "language"}
    assert template.fragments == {}


def test_json_braces_in_the_output_format_are_not_placeholders(prompts: Path) -> None:
    write(prompts, "demo", VALID)
    text = load(prompts).render(topic="t", language="en")
    assert '{"a": {"b": 1}}' in text


def test_examples_section_is_optional_and_last(prompts: Path) -> None:
    write(prompts, "demo", VALID + "\n# Examples\nExample one.\n")
    template = load(prompts)
    assert template.sections[-1][0] == "Examples"


def test_loading_is_cached_per_file_location(prompts: Path, tmp_path: Path) -> None:
    write(prompts, "demo", VALID)
    other = tmp_path / "other"
    other.mkdir()
    write(other, "demo", VALID.replace("Be brief.", "Be long."))
    assert load(prompts) is load(prompts)
    assert load(other) is not load(prompts)
    assert "Be long." in load(other).render(topic="t", language="en")


def test_windows_line_endings_are_accepted(prompts: Path) -> None:
    (prompts / "demo.md").write_bytes(VALID.replace("\n", "\r\n").encode())
    template = load(prompts)
    assert template.variables == {"topic", "language"}
    assert "\r" not in template.render(topic="t", language="en")


def test_list_prompts_lists_prompt_files_only(prompts: Path) -> None:
    write(prompts, "b_prompt", VALID)
    write(prompts, "a_prompt", VALID)
    add_fragment(prompts)
    (prompts / "notes.txt").write_text("x")
    assert list_prompts(directory=prompts) == ["a_prompt", "b_prompt"]


# --------------------------------------------------------------------------
# Names and missing files
# --------------------------------------------------------------------------


def test_missing_prompt_is_not_found(prompts: Path) -> None:
    with pytest.raises(PromptError) as err:
        load(prompts, "nope")
    assert err.value.code == "prompt_not_found"


@pytest.mark.parametrize("name", ["", "../secret", "Bad", "a/b", "a.b", "1abc", "a b"])
def test_invalid_names_are_rejected_before_touching_the_disk(
    prompts: Path, name: str
) -> None:
    with pytest.raises(PromptError) as err:
        load(prompts, name)
    assert err.value.code == "prompt_not_found"


def test_names_cannot_escape_the_prompt_directory(prompts: Path) -> None:
    # a perfectly valid prompt exists one level up; it must still not load
    inner = prompts / "prompts"
    inner.mkdir()
    write(prompts, "outside", VALID.replace("demo.v1", "outside.v1"))
    assert load_prompt("outside", directory=prompts).version == "outside.v1"
    with pytest.raises(PromptError) as err:
        load_prompt("../outside", directory=inner)
    assert err.value.code == "prompt_not_found"


# --------------------------------------------------------------------------
# Front matter
# --------------------------------------------------------------------------


def test_front_matter_is_required(prompts: Path) -> None:
    expect_error(prompts, VALID.split("---\n", 2)[2])


def test_front_matter_must_be_closed(prompts: Path) -> None:
    expect_error(prompts, "---\nprompt_version: demo.v1\n# Role\nx\n")


def test_prompt_version_is_required(prompts: Path) -> None:
    error = expect_error(prompts, VALID.replace("prompt_version: demo.v1", ""))
    assert "must set prompt_version" in error.message


def test_other_front_matter_keys_do_not_replace_prompt_version(prompts: Path) -> None:
    error = expect_error(prompts, VALID.replace("prompt_version: demo.v1", "other: 1"))
    assert "unknown front matter keys: other" in error.message


def test_blank_lines_in_front_matter_are_ignored(prompts: Path) -> None:
    write(
        prompts,
        "demo",
        VALID.replace("prompt_version: demo.v1", "\nprompt_version: demo.v1\n"),
    )
    assert load(prompts).version == "demo.v1"


def test_unknown_front_matter_keys_are_rejected(prompts: Path) -> None:
    expect_error(
        prompts,
        VALID.replace(
            "prompt_version: demo.v1", "prompt_version: demo.v1\nprompt_verison: x"
        ),
    )


@pytest.mark.parametrize("line", ["prompt_version", "prompt_version:", ": demo.v1"])
def test_malformed_front_matter_lines_are_rejected(prompts: Path, line: str) -> None:
    expect_error(prompts, VALID.replace("prompt_version: demo.v1", line))


@pytest.mark.parametrize(
    "version", ["demo.v0", "demo", "demo.1", "other.v1", "demo.v1.1", "Demo.v1"]
)
def test_prompt_version_must_match_the_file_name(prompts: Path, version: str) -> None:
    expect_error(prompts, VALID.replace("demo.v1", version))


def test_higher_versions_are_fine(prompts: Path) -> None:
    write(prompts, "demo", VALID.replace("demo.v1", "demo.v12"))
    assert load(prompts).version == "demo.v12"


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section", ["Role", "Task", "Inputs", "Output format", "Rules"]
)
def test_every_required_section_must_exist(prompts: Path, section: str) -> None:
    text = re.sub(
        rf"# {re.escape(section)}\n.*?(?=\n# |\Z)", "", VALID, flags=re.DOTALL
    )
    assert f"# {section}\n" not in text
    error = expect_error(prompts, text)
    assert f"missing sections: {section}" in error.message


def test_sections_must_be_in_order(prompts: Path) -> None:
    swapped = VALID.replace("# Task", "# TMP").replace("# Inputs", "# Task")
    swapped = swapped.replace("# TMP", "# Inputs")
    expect_error(prompts, swapped)


def test_unknown_sections_are_rejected(prompts: Path) -> None:
    expect_error(prompts, VALID + "\n# Extras\nmore\n")


def test_the_feedback_heading_is_reserved_for_code(prompts: Path) -> None:
    expect_error(prompts, VALID + f"\n# {FEEDBACK_HEADING}\nmore\n")


def test_duplicate_sections_are_rejected(prompts: Path) -> None:
    expect_error(prompts, VALID + "\n# Rules\nagain\n")


def test_empty_sections_are_rejected(prompts: Path) -> None:
    expect_error(prompts, VALID.replace("- Be brief.\n", ""))


def test_text_before_the_first_heading_is_rejected(prompts: Path) -> None:
    expect_error(prompts, VALID.replace("# Role", "stray text\n\n# Role", 1))


def test_headings_inside_code_fences_are_not_sections(prompts: Path) -> None:
    text = VALID.replace('{"a": {"b": 1}}', "# Rules\n# not a heading")
    write(prompts, "demo", text)
    template = load(prompts)
    assert [t for t, _ in template.sections].count("Rules") == 1
    assert "# not a heading" in dict(template.sections)["Output format"]


def test_unclosed_code_fences_are_rejected(prompts: Path) -> None:
    expect_error(prompts, VALID + "\n```json\n{")


def test_subheadings_are_allowed(prompts: Path) -> None:
    write(prompts, "demo", VALID.replace("- Be brief.", "## Style\n- Be brief."))
    assert "## Style" in dict(load(prompts).sections)["Rules"]


# --------------------------------------------------------------------------
# Placeholders and built-ins
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["{{ topic }}", "{{Topic}}", "{{1x}}", "{{topic", "{{}}"]
)
def test_malformed_placeholders_are_rejected(prompts: Path, bad: str) -> None:
    expect_error(prompts, VALID.replace("{{language}}", bad))


def test_reserved_variable_names_are_rejected(prompts: Path) -> None:
    expect_error(prompts, VALID.replace("{{language}}", "{{feedback}}"))


def test_safety_rules_are_a_built_in_variable(prompts: Path) -> None:
    write(
        prompts, "demo", VALID.replace("- Be brief.", "- Be brief.\n{{safety_rules}}")
    )
    add_fragment(prompts, text="  NEVER LIE\n- be kind  ")
    template = load(prompts)
    assert template.variables == {"topic", "language"}  # not asked from the caller
    assert template.fragments == {"safety_rules": "NEVER LIE\n- be kind"}
    system = template.messages(topic="t", language="en")[0].content
    assert "NEVER LIE\n- be kind" in system
    assert "{{" not in system


def test_built_ins_cannot_be_overridden_by_the_caller(prompts: Path) -> None:
    write(
        prompts, "demo", VALID.replace("- Be brief.", "- Be brief.\n{{safety_rules}}")
    )
    add_fragment(prompts)
    with pytest.raises(PromptError) as err:
        load(prompts).render(topic="t", language="en", safety_rules="none")
    assert err.value.code == "prompt_variables"
    assert err.value.details["unknown"] == ["safety_rules"]  # type: ignore[index]


def test_missing_fragment_file_is_an_error(prompts: Path) -> None:
    expect_error(prompts, VALID.replace("- Be brief.", "{{safety_rules}}"))


@pytest.mark.parametrize("content", ["", "   \n", "uses {{topic}}"])
def test_bad_fragments_are_rejected(prompts: Path, content: str) -> None:
    write(prompts, "demo", VALID.replace("- Be brief.", "{{safety_rules}}"))
    add_fragment(prompts, text=content)
    with pytest.raises(PromptError) as err:
        load(prompts)
    assert err.value.code == "prompt_invalid"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


@pytest.fixture
def demo(prompts: Path):  # type: ignore[no-untyped-def]
    write(prompts, "demo", VALID)
    return load(prompts)


def test_render_fills_every_placeholder_in_file_order(demo) -> None:  # type: ignore[no-untyped-def]
    text = demo.render(topic="Habits", language="en")
    assert "{{" not in text
    assert "Do the demo for Habits." in text
    assert "- Language: en" in text
    positions = [text.index(f"# {t}") for t in ("Role", "Task", "Inputs", "Rules")]
    assert positions == sorted(positions)


def test_missing_variables_are_listed(demo) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PromptError) as err:
        demo.render(topic="x")
    assert err.value.code == "prompt_variables"
    assert err.value.details == {
        "prompt": "demo",
        "missing": ["language"],
        "unknown": [],
    }
    assert "language" in err.value.message


def test_unknown_variables_are_listed(demo) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PromptError) as err:
        demo.render(topic="x", language="en", tone="loud", extra=1)
    assert err.value.details["unknown"] == ["extra", "tone"]  # type: ignore[index]


class Plan(BaseModel):
    angle: str
    tags: list[str]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "none"),
        ("", "none"),
        ("   \n", "none"),
        ("plain text", "plain text"),
        (7, "7"),
        (0, "0"),
        (False, "False"),
        (Decimal("1.5"), "1.5"),
        ({"a": 1}, '{\n  "a": 1\n}'),
        (["x", "y"], '[\n  "x",\n  "y"\n]'),
        (("x",), '[\n  "x"\n]'),
        (
            Plan(angle="A", tags=["t"]),
            '{\n  "angle": "A",\n  "tags": [\n    "t"\n  ]\n}',
        ),
    ],
)
def test_values_are_turned_into_text(demo, value, expected) -> None:  # type: ignore[no-untyped-def]
    assert f"- Topic: {expected}" in demo.render(topic=value, language="en")


def test_non_ascii_text_is_kept_readable_in_json(demo) -> None:  # type: ignore[no-untyped-def]
    text = demo.render(topic={"line": "یہ ایک مثال ہے"}, language="ur")
    assert "یہ ایک مثال ہے" in text
    assert "\\u" not in text


def test_inserted_values_are_never_expanded_again(demo) -> None:  # type: ignore[no-untyped-def]
    # a user's topic that looks like a placeholder must stay literal text
    text = demo.render(topic="{{language}} {{safety_rules}} {{topic}}", language="en")
    assert "- Topic: {{language}} {{safety_rules}} {{topic}}" in text
    assert "- Language: en" in text


def test_the_same_value_can_fill_several_placeholders(demo) -> None:  # type: ignore[no-untyped-def]
    assert demo.render(topic="Habits", language="en").count("Habits") == 2


# --------------------------------------------------------------------------
# Retry feedback
# --------------------------------------------------------------------------


def test_feedback_block_is_appended_at_the_end(demo) -> None:  # type: ignore[no-untyped-def]
    text = demo.render(topic="t", language="en", feedback="  Caption too long.  ")
    assert text.endswith(
        f"# {FEEDBACK_HEADING}\nCaption too long.\n\n{FEEDBACK_FOOTER}"
    )
    assert text.index("# Rules") < text.index(f"# {FEEDBACK_HEADING}")


@pytest.mark.parametrize("feedback", [None, "", "   ", "\n"])
def test_no_feedback_block_without_feedback(demo, feedback) -> None:  # type: ignore[no-untyped-def]
    text = demo.render(topic="t", language="en", feedback=feedback)
    assert FEEDBACK_HEADING not in text
    assert text == demo.render(topic="t", language="en")


def test_feedback_is_not_treated_as_a_template(demo) -> None:  # type: ignore[no-untyped-def]
    feedback = "Remove {{brand}} and {{topic}} from the caption."
    text = demo.render(topic="REAL", language="en", feedback=feedback)
    assert feedback in text
    assert text.count("REAL") == 2  # topic appears only where the template put it


# --------------------------------------------------------------------------
# Messages for the LLM layer
# --------------------------------------------------------------------------


def test_messages_split_system_and_user_parts(demo) -> None:  # type: ignore[no-untyped-def]
    system, user = demo.messages(topic="Habits", language="en")
    assert isinstance(system, Message) and isinstance(user, Message)
    assert (system.role, user.role) == ("system", "user")
    assert "# Role" in system.content
    assert "# Output format" in system.content
    assert "# Rules" in system.content
    assert "# Task" not in system.content and "# Inputs" not in system.content
    assert "# Task" in user.content and "# Inputs" in user.content
    assert "# Role" not in user.content and "# Rules" not in user.content
    assert "- Topic: Habits" in user.content


def test_messages_put_feedback_in_the_user_message(demo) -> None:  # type: ignore[no-untyped-def]
    system, user = demo.messages(topic="t", language="en", feedback="Shorten it.")
    assert FEEDBACK_HEADING not in system.content
    assert user.content.endswith(FEEDBACK_FOOTER)
    assert "Shorten it." in user.content


def test_messages_check_variables_too(demo) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PromptError):
        demo.messages(topic="t")


def test_examples_go_to_the_system_message(prompts: Path) -> None:
    write(prompts, "demo", VALID + "\n# Examples\nExample one.\n")
    system, user = load(prompts).messages(topic="t", language="en")
    assert "# Examples\nExample one." in system.content
    assert "Example one." not in user.content
