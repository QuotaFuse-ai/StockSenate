"""Configuration contract tests.

Each test pins one rule that, if broken, would silently produce a run with the
wrong model or an over-sized senate.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from stock_senate.config import schema

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


def copy_config(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(CONFIG, target)
    return target


def append_to(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_repository_config_is_valid() -> None:
    report = schema.validate_dir(CONFIG)
    assert report["ok"] is True, report["errors"]


def test_unknown_role_llm_key_fails(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    path = directory / "llm_config.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[llm.role_llms]\n",
            '[llm.role_llms]\nbogus_role = { provider = "minimax", model = "x" }\n',
            1,
        ),
        encoding="utf-8",
    )
    report = schema.validate_dir(directory)
    assert report["ok"] is False
    assert any("bogus_role" in error["key"] for error in report["errors"])


def test_alias_collision_fails(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    path = directory / "llm_config.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[llm.role_llms]\n",
            '[llm.role_llms]\ntechnical = { provider = "minimax", model = "x" }\n',
            1,
        ),
        encoding="utf-8",
    )
    report = schema.validate_dir(directory)
    assert report["ok"] is False
    assert any("别名" in error["message"] for error in report["errors"])


def analyst_roles(count: int) -> dict:
    return {
        f"custom_{index}": {
            "display_name": f"自定义{index}",
            "kind": "analyst",
            "prompt": f"prompts/analysts/custom_{index}.md",
            "tools": ["news_search"],
            "output_schema": "AnalystReport",
            "enabled": True,
        }
        for index in range(count)
    }


def test_thirteenth_analyst_is_rejected() -> None:
    report = schema.validate_roles(analyst_roles(13))
    assert report["ok"] is False
    assert any("超过上限" in error["message"] for error in report["errors"])


def test_twelve_analysts_are_accepted() -> None:
    report = schema.validate_roles(analyst_roles(12))
    assert report["ok"] is True, report["errors"]


def test_sixth_custom_analyst_is_rejected(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    extra = "".join(
        f"\n[roles.custom_{index}]\n"
        f'display_name = "自定义{index}"\n'
        f'kind = "analyst"\n'
        f'prompt = "prompts/analysts/custom_{index}.md"\n'
        f'tools = ["news_search"]\n'
        f'output_schema = "AnalystReport"\n'
        f"enabled = true\n"
        for index in range(6)
    )
    append_to(directory / "agent_roles.toml", extra)
    report = schema.validate_dir(directory)
    assert report["ok"] is False
    assert any("自定义分析师" in error["message"] for error in report["errors"])


def test_text_doc_must_not_extract_numbers(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    path = directory / "data_source.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "numeric_extraction = false", "numeric_extraction = true"
        ),
        encoding="utf-8",
    )
    report = schema.validate_dir(directory)
    assert report["ok"] is False
    assert any("numeric_extraction" in error["key"] for error in report["errors"])


def test_llm_fill_must_stay_forbidden(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    path = directory / "data_source.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "forbid_llm_fill = true", "forbid_llm_fill = false"
        ),
        encoding="utf-8",
    )
    report = schema.validate_dir(directory)
    assert report["ok"] is False
    assert any("forbid_llm_fill" in error["key"] for error in report["errors"])


def test_output_template_must_forbid_order_fields(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    path = directory / "output_template.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace('"auto_order"', '"note"'),
        encoding="utf-8",
    )
    report = schema.validate_dir(directory)
    assert report["ok"] is False
    assert any("auto_order" in error["message"] for error in report["errors"])


def test_unknown_key_warns_by_default(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    path = directory / "workflow.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "max_debate_rounds = 2",
            "max_debate_rounds = 2\nmax_debate_roundz = 3",
            1,
        ),
        encoding="utf-8",
    )
    report = schema.validate_dir(directory)
    assert report["ok"] is True, report["errors"]
    assert any(warning["key"] == "debate.max_debate_roundz" for warning in report["warnings"])


def test_strict_mode_turns_unknown_key_into_an_error(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    path = directory / "workflow.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "max_debate_rounds = 2",
            "max_debate_rounds = 2\nmax_debate_roundz = 3",
            1,
        ),
        encoding="utf-8",
    )
    report = schema.validate_dir(directory, strict=True)
    assert report["ok"] is False
    assert any(error["key"] == "debate.max_debate_roundz" for error in report["errors"])


def test_unknown_top_level_table_is_reported(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    append_to(directory / "workflow.toml", "\n[experimental]\nflag = true\n")
    report = schema.validate_dir(directory)
    assert report["ok"] is True
    assert any(warning["key"] == "experimental" for warning in report["warnings"])


def test_unknown_key_inside_a_role_entry_is_reported(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    path = directory / "agent_roles.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'output_schema = "AnalystReport"',
            'output_schema = "AnalystReport"\noutput_schem = "AnalystReport"',
            1,
        ),
        encoding="utf-8",
    )
    report = schema.validate_dir(directory)
    assert report["ok"] is True
    assert any(warning["key"] == "roles.market.output_schem" for warning in report["warnings"])


def test_name_keyed_tables_accept_new_names(tmp_path: Path) -> None:
    directory = copy_config(tmp_path)
    append_to(
        directory / "agent_roles.toml",
        '\n[roles.custom_sector]\n'
        'display_name = "行业分析师"\n'
        'kind = "analyst"\n'
        'tools = ["news_search"]\n'
        'output_schema = "AnalystReport"\n'
        "enabled = false\n",
    )
    append_to(
        directory / "data_source.toml",
        '\n[providers.my_archive]\nkind = "json_file"\nroot = "data/mine"\n',
    )
    append_to(directory / "output_template.toml", '\n[sections.appendix]\ntitle = "附录"\n')
    report = schema.validate_dir(directory)
    assert report["ok"] is True, report["errors"]
    assert report["warnings"] == []
