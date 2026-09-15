"""Checks for the Decoinks Prompt Management wiring. No network, no browser.

Run from the project root:  python tests/test_prompt_registry.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import requests  # noqa: F401
except ImportError:  # the registry only needs it for real fetches
    sys.modules["requests"] = types.ModuleType("requests")

from config import workflows as W  # noqa: E402
from config.job_options import JOB_OPTIONS, PARAMETERISED_OPTIONS  # noqa: E402
from src import prompt_builder  # noqa: E402
from src.prompt_registry import (  # noqa: E402
    PROMPT_KEYS, PromptRegistry, _builtin, from_python_template, option_template_name,
    prompt_names_for_job, template_fields, to_python_template,
)

FAILS: list[str] = []


def check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  [{extra}]"))
    if not cond:
        FAILS.append(label)


def fake_registry(texts: dict[str, str]) -> PromptRegistry:
    """A registry whose "Decoinks" answer is `texts` (key -> {{}} text), never refetched."""
    reg = PromptRegistry()
    reg._data = {"revision": "t", "prompts": {
        k: {"prompt_key": k, "text": t, "version": {"id": f"v-{i}", "number": 1}}
        for i, (k, t) in enumerate(texts.items())}}
    reg._fetched_at = float("inf")
    return reg


# 1. Every prompt text in the code is mapped to a key in the library.
code_prompts = {n for n, v in vars(W).items() if n.isupper() and isinstance(v, str) and len(v) > 60}
code_prompts |= {"BASE_INSTRUCTION"} | {option_template_name(k) for k in JOB_OPTIONS}
check("every prompt in the code has a Prompt Management key", code_prompts <= set(PROMPT_KEYS),
      sorted(code_prompts - set(PROMPT_KEYS)))
check("27 prompts mapped, keys unique", len(PROMPT_KEYS) == 27 and len(set(PROMPT_KEYS.values())) == 27, len(PROMPT_KEYS))

# 2. Conversion to Decoinks {{}} text and back is exact for every one of them.
bad = [n for n in PROMPT_KEYS if to_python_template(from_python_template(_builtin(n))) != _builtin(n)]
check("round trip {x} <-> {{x}} is byte-identical for all 27", not bad, bad)

# 3. Published text identical to the code -> the automation runs the code's text, from Decoinks.
same = {PROMPT_KEYS[n]: from_python_template(_builtin(n)) for n in PROMPT_KEYS}
reg = fake_registry(same)
served = {n: reg.template(n) for n in PROMPT_KEYS}
check("all 27 served as managed and identical to the built-in text",
      all(t == _builtin(n) and m["source"] == "managed" for n, (t, m) in served.items()))

# 4. A version that adds or drops a placeholder is refused.
changed = dict(same)
changed["AIS.EDIT.RECOLOUR"] = "Recolour it"            # drops {{value}}
changed["AIS.EXTRACT.BOXES"] = same["AIS.EXTRACT.BOXES"] + " {{dpi}}"  # adds one
reg2 = fake_registry(changed)
t, m = reg2.template("JOB_OPTION_RECOLOUR")
check("dropping {{value}} is refused", m["source"] == "built_in" and t == JOB_OPTIONS["recolour"], m)
t, m = reg2.template("EXTRACT_BOXES")
check("adding {{dpi}} is refused", m["source"] == "built_in" and t == W.EXTRACT_BOXES, m)

# 5. Which prompts each job uses.
check("text job", prompt_names_for_job("text") == ["TEXT_TURN_0", "TEXT_TURN_1", "TEXT_TURN_2", "TEXT_TURN_3"])
check("custom job", prompt_names_for_job("custom", ["reconstruct", "aspect_ratio"]) ==
      ["CUSTOM_RECONSTRUCT", "CUSTOM_ASPECT_ADVICE", "CUSTOM_ASPECT_BASELINE", "CUSTOM_ASPECT_REGENERATE"])
check("edit-options job", prompt_names_for_job("", options=["recolour", "text_only"]) ==
      ["BASE_INSTRUCTION", "JOB_OPTION_RECOLOUR", "JOB_OPTION_TEXT_ONLY"])

# 6. The edit-options prompt is built from the managed blocks.
opts, params = ["remove_background", "recolour"], {"recolour": "navy"}
check("build_prompt with no managed text is unchanged",
      prompt_builder.build_prompt(opts, params, "note") == prompt_builder.build_prompt(opts, params, "note", templates={}))
managed = {"BASE_INSTRUCTION": "BASE v2", "JOB_OPTION_RECOLOUR": "Recolour to {value} v2"}
built = prompt_builder.build_prompt(opts, params, "note", templates=managed)
check("build_prompt uses the managed base and option blocks",
      built == "BASE v2\n\n" + JOB_OPTIONS["remove_background"] + "\n\nRecolour to navy v2\n\nnote", built)
check("parameterised options still need {value}",
      all("{value}" in JOB_OPTIONS[k] and template_fields(JOB_OPTIONS[k]) == {"value"} for k in PARAMETERISED_OPTIONS))

print("\nRESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
