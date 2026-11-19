"""Contract tests for the prompts that ship in ``app/agents/prompts`` (B2-03).

The output examples in the prompts are checked against the Pydantic classes
written in ``AGENT_SPECS.md``, so the spec and the prompts cannot drift apart
silently. The nodes (B2-05 onwards) add schema tests against the real models.
"""

import ast
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from app.agents.prompts import PromptTemplate, list_prompts, load_prompt
from app.agents.prompts.loader import PROMPTS_DIR

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "AGENT_SPECS.md"

EXPECTED_VARIABLES: dict[str, set[str]] = {
    "planner": {
        "topic",
        "content_type",
        "platforms",
        "brand_voice",
        "language",
        "options",
    },
    "researcher_queries": {"topic", "plan", "language", "max_queries"},
    "researcher_summary": {"topic", "plan", "language", "search_results"},
    "strategist": {
        "content_type",
        "platforms",
        "plan",
        "research",
        "brand_voice",
        "platform_rules",
        "language",
    },
    "writer": {
        "platform",
        "plan",
        "research",
        "strategy",
        "brand_voice",
        "language",
        "platform_rules",
        "banned_words",
    },
    "image": {
        "platform",
        "caption",
        "plan",
        "strategy",
        "brand_voice",
        "language",
        "aspect_ratios",
    },
    "hashtag": {
        "platform",
        "caption",
        "plan",
        "hashtag_count",
        "hashtag_min",
        "hashtag_max",
        "banned_words",
        "language",
    },
    "reviewer": {
        "content_type",
        "plan",
        "strategy",
        "research",
        "drafts",
        "platform_rules",
        "brand_voice",
        "banned_words",
        "language",
    },
    "judge": {
        "agent",
        "rubric",
        "output",
        "plan",
        "strategy",
        "platform_rules",
        "brand_voice",
        "language",
    },
}

# AGENT_SPECS 1.2: brand voice goes to planner, strategist, writer, reviewer
# (and script, which arrives with B4); the judge needs it to rate voice fit.
USES_BRAND_VOICE = {"planner", "strategist", "writer", "image", "reviewer", "judge"}

# Values and limits the rules of each prompt must state (AGENT_SPECS 3-5).
MUST_MENTION: dict[str, list[str]] = {
    "planner": [
        "awareness",
        "engagement",
        "education",
        "promotion",
        "other",
        "2 to 5",
    ],
    "researcher_queries": ["at most {{max_queries}}"],
    "researcher_summary": ["high, medium, low", "At most 15 facts", "grounded"],
    "strategist": ["short", "long", "hashtags_min", "hashtags_max", "3 to 5"],
    "writer": ["30 percent", "refused", "hashtags in the caption"],
    "image": ["200 characters", "aspect ratios"],
    "hashtag": ["40 characters", "unique", "minimum", "maximum"],
    "reviewer": [
        "planner, strategist, writer, image, hashtag, script, storyboard",
        "scene:<n>",
        "error",
        "warning",
    ],
    "judge": ["0 to 1", "error", "warning", "500 characters", "200 characters"],
}

LEAF = None


def shape(value: Any) -> Any:
    """Reduce JSON to its field structure: dict keys, first list item, leaves."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return LEAF


def output_example(name: str) -> Any:
    """The JSON object shown in the prompt's ``# Output format`` section."""
    body = dict(load_prompt(name).sections)["Output format"]
    match = re.search(r"```json\n(.*?)```", body, re.DOTALL)
    assert match, f"{name}: no json example in # Output format"
    return json.loads(match.group(1))


def spec_classes() -> dict[str, set[str]]:
    """Field names of every ``class X(BaseModel)`` in AGENT_SPECS.md code blocks."""
    classes: dict[str, set[str]] = {}
    for block in re.findall(r"```python\n(.*?)```", SPEC.read_text("utf-8"), re.DOTALL):
        for node in ast.parse(block).body:
            if isinstance(node, ast.ClassDef):
                classes[node.name] = {
                    item.target.id
                    for item in node.body
                    if isinstance(item, ast.AnnAssign)
                    and isinstance(item.target, ast.Name)
                }
    return classes


SPEC_CLASSES = spec_classes()
NAMES = sorted(EXPECTED_VARIABLES)


def sample_values(template: PromptTemplate) -> dict[str, str]:
    return {name: f"<<{name.upper()}>>" for name in template.variables}


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------


def test_the_expected_prompts_ship() -> None:
    assert list_prompts() == NAMES


@pytest.mark.parametrize("name", NAMES)
def test_prompt_loads_and_has_a_matching_version(name: str) -> None:
    template = load_prompt(name)
    assert template.name == name
    assert template.version == f"{name}.v1"


@pytest.mark.parametrize("name", NAMES)
def test_prompt_variables_are_the_documented_ones(name: str) -> None:
    assert load_prompt(name).variables == EXPECTED_VARIABLES[name]


@pytest.mark.parametrize("name", NAMES)
def test_every_prompt_takes_the_output_language(name: str) -> None:
    assert "language" in load_prompt(name).variables


@pytest.mark.parametrize("name", NAMES)
def test_brand_voice_is_passed_where_the_spec_says(name: str) -> None:
    assert ("brand_voice" in load_prompt(name).variables) == (name in USES_BRAND_VOICE)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_variable_reaches_the_rendered_prompt(name: str) -> None:
    template = load_prompt(name)
    text = template.render(**sample_values(template))
    assert "{{" not in text
    for variable in template.variables:
        assert f"<<{variable.upper()}>>" in text, variable


# Small settings that the rules legitimately mention ("write in <language>").
# Everything else (topic, plan, research, brand voice, drafts...) is
# user-influenced text and must never be placed in the system message.
SYSTEM_MESSAGE_VARIABLES = {"language", "max_queries"}


@pytest.mark.parametrize("name", NAMES)
def test_user_influenced_text_stays_out_of_the_system_message(name: str) -> None:
    template = load_prompt(name)
    system, user = template.messages(**sample_values(template))
    assert system.role == "system" and user.role == "user"
    for section in ("# Role", "# Output format", "# Rules"):
        assert section in system.content
    assert "# Task" in user.content and "# Inputs" in user.content
    for variable in template.variables - SYSTEM_MESSAGE_VARIABLES:
        assert f"<<{variable.upper()}>>" not in system.content, variable
        assert f"<<{variable.upper()}>>" in user.content, variable


@pytest.mark.parametrize("name", sorted(USES_BRAND_VOICE))
def test_brand_voice_reaches_the_user_message(name: str) -> None:
    template = load_prompt(name)
    values = sample_values(template) | {"brand_voice": "WARM-AND-PLAYFUL"}
    assert "WARM-AND-PLAYFUL" in template.messages(**values)[1].content


@pytest.mark.parametrize("name", NAMES)
def test_retry_feedback_reaches_the_user_message(name: str) -> None:
    template = load_prompt(name)
    messages = template.messages(feedback="Hook is weak.", **sample_values(template))
    assert "# Previous attempt feedback\nHook is weak." in messages[1].content
    assert "Hook is weak." not in messages[0].content


@pytest.mark.parametrize("name", NAMES)
def test_shared_safety_rules_are_in_every_prompt(name: str) -> None:
    template = load_prompt(name)
    assert "safety_rules" in template.fragments
    system = template.messages(**sample_values(template))[0].content
    assert "Never invent facts" in system
    assert "never as instructions" in system  # prompt-injection rule


@pytest.mark.parametrize("name", NAMES)
def test_user_text_cannot_inject_placeholders(name: str) -> None:
    template = load_prompt(name)
    values = sample_values(template)
    victim = sorted(template.variables)[0]
    values[victim] = "{{safety_rules}} {{" + victim + "}}"
    text = template.render(**values)
    assert "{{safety_rules}} {{" + victim + "}}" in text


# --------------------------------------------------------------------------
# Output format matches AGENT_SPECS.md
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_output_example_is_valid_json_object(name: str) -> None:
    assert isinstance(output_example(name), dict)


def keys(name: str, *path: Any) -> set[str]:
    node = output_example(name)
    for step in path:
        node = node[step]
    return set(node)


@pytest.mark.parametrize(
    ("name", "path", "spec_class"),
    [
        ("planner", (), "ContentPlan"),
        ("researcher_queries", (), "SearchQueries"),
        ("researcher_summary", (), "ResearchNotes"),
        ("researcher_summary", ("facts", 0), "Fact"),
        ("strategist", ("<platform>",), "PlatformStrategy"),
        ("writer", (), "WriterOutput"),
        ("image", (), "ImagePrompt"),
        ("reviewer", (), "ReviewResult"),
        ("reviewer", ("issues", 0), "ReviewIssue"),
        ("judge", (), "CheckResult"),
        ("judge", ("issues", 0), "Issue"),
    ],
)
def test_output_fields_equal_the_spec_class(
    name: str, path: tuple[Any, ...], spec_class: str
) -> None:
    assert spec_class in SPEC_CLASSES, f"{spec_class} missing from AGENT_SPECS.md"
    assert keys(name, *path) == SPEC_CLASSES[spec_class]


def test_hashtag_output_is_just_the_hashtag_list() -> None:
    # AGENT_SPECS 4.3 defines HashtagOutput inline: HashtagOutput(hashtags: list[str])
    assert keys("hashtag") == {"hashtags"}


def test_reviewer_scores_have_the_five_documented_keys() -> None:
    assert keys("reviewer", "scores") == {
        "quality",
        "accuracy",
        "policy",
        "platform_fit",
        "brand_voice",
    }


def test_strategist_video_fields_are_described_in_the_rules() -> None:
    rules = dict(load_prompt("strategist").sections)["Rules"]
    assert SPEC_CLASSES["VideoStrategy"]
    for field in SPEC_CLASSES["VideoStrategy"]:
        assert f'"{field}"' in rules, field
    assert output_example("strategist")["<platform>"]["video"] is None


@pytest.mark.parametrize("name", NAMES)
def test_output_example_shape_is_stable(name: str) -> None:
    # guards against someone turning a list or object into a plain value
    example = output_example(name)
    assert shape(example) == shape(json.loads(json.dumps(example)))


@pytest.mark.parametrize(("name", "phrases"), sorted(MUST_MENTION.items()))
def test_rules_state_the_spec_values_and_limits(name: str, phrases: list[str]) -> None:
    template = load_prompt(name)
    text = "\n".join(body for _, body in template.sections)
    for phrase in phrases:
        assert phrase in text, f"{name}: rules must mention {phrase!r}"


# --------------------------------------------------------------------------
# Hygiene and packaging
# --------------------------------------------------------------------------

PROMPT_FILES = sorted(PROMPTS_DIR.rglob("*.md"))


def test_prompt_files_exist() -> None:
    assert len(PROMPT_FILES) == len(NAMES) + 1  # + the safety fragment


@pytest.mark.parametrize("path", PROMPT_FILES, ids=lambda p: p.name)
def test_prompts_have_no_hard_coded_model_names_or_secrets(path: Path) -> None:
    text = path.read_text("utf-8")
    models = re.search(
        r"\b(gpt|gemini|claude|llama|mistral|openai|anthropic)\b", text, re.IGNORECASE
    )
    secrets = re.search(
        r"(api[_ -]?key|secret|password|bearer|token|sk-[A-Za-z0-9]{8,})",
        text,
        re.IGNORECASE,
    )
    assert models is None, models and models.group(0)
    assert secrets is None, secrets and secrets.group(0)


@pytest.mark.parametrize("path", PROMPT_FILES, ids=lambda p: p.name)
def test_prompt_files_use_unix_line_endings_and_utf8(path: Path) -> None:
    raw = path.read_bytes()
    assert b"\r" not in raw
    raw.decode("utf-8")


def test_package_data_includes_every_prompt_file() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["app.agents.prompts"]
    shipped = {p for pattern in patterns for p in PROMPTS_DIR.glob(pattern)}
    assert set(PROMPT_FILES) <= shipped
