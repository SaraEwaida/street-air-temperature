"""Agent: tools, guardrails, report, and both provider adapters (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from streetair.agent import ScriptedProvider, Toolbox, run_agent
from streetair.agent.providers import AnthropicProvider, OpenAIProvider, ToolCall
from test_model import CFG, make_data


@pytest.fixture
def toolbox(tmp_path):
    return Toolbox(CFG, make_data(n_st=6, days=6), tmp_path / "agent", max_experiments=2, hour_stride=2)


def test_full_session_with_guardrails(toolbox):
    plan = [
        [("check_data", {})],
        [("run_experiment", {"name": "baseline", "rationale": "defaults"}),
         ("run_experiment", {"name": "huge", "rationale": "too big", "params": {"num_leaves": 5000}})],
        [("run_experiment", {"name": "no-weather", "rationale": "ablation", "drop_feature_groups": ["weather"]})],
        [("run_experiment", {"name": "third", "rationale": "over budget"})],
        [("compare_experiments", {"names": ["baseline", "no-weather"]}),
         ("write_report", {"title": "Study 1", "markdown": "Weather features matter at night."})],
    ]
    prov = ScriptedProvider(plan)
    res = run_agent(prov, toolbox, verbose=False)
    assert res["experiments"] == 2 and len(res["reports"]) == 1

    errors = [(name, out["error"]) for step in prov.received for name, out, err in step if err]
    assert any("num_leaves=5000 outside" in e for _, e in errors)        # range check
    assert any("budget" in e for _, e in errors)                         # budget check
    check = prov.received[0][0][1]
    assert check["stations"] == 6 and check["issues"] == ["none found"]

    report = open(res["reports"][0], encoding="utf-8").read()
    assert "Weather features matter at night." in report
    assert "| baseline | defaults |" in report and "| no-weather | no weather |" in report
    assert report.count("\n| ") >= 9                                     # results rows + tool log rows
    rec = json.loads((toolbox.exp_dir / "no-weather.json").read_text(encoding="utf-8"))
    assert rec["dropped_groups"] == ["weather"] and rec["n_features"] < json.loads(
        (toolbox.exp_dir / "baseline.json").read_text(encoding="utf-8"))["n_features"]
    assert rec["lightgbm"]["mae_c"] > 0 and rec["era5_raw"]["mae_c"] > 0


def test_bad_inputs_are_errors_not_crashes(toolbox):
    for name, args, msg in [
        ("run_experiment", {"name": "bad name!", "rationale": "x"}, "name must"),
        ("run_experiment", {"name": "x", "rationale": "x", "params": {"max_depth": 3}}, "not allowed"),
        ("run_experiment", {"name": "x", "rationale": "x", "drop_feature_groups": ["colour"]}, "unknown feature"),
        ("compare_experiments", {"names": ["nope"]}, "unknown experiments"),
        ("delete_everything", {}, "Unknown tool"),
        ("run_experiment", {"rationale": "missing name"}, "Bad arguments"),
    ]:
        text, err = toolbox.call(name, args)
        assert err and msg in json.loads(text)["error"], (name, text)
    assert toolbox.experiments_run == 0


def test_report_is_written_even_if_agent_forgets(toolbox):
    prov = ScriptedProvider([[("check_data", {})]], final_text="I am done.")
    res = run_agent(prov, toolbox, verbose=False)
    assert len(prov.reminders) == 1                                     # reminded once, then gave up
    assert len(res["reports"]) == 1 and "no report written" in open(res["reports"][0], encoding="utf-8").read()


def test_reminder_gets_the_report_written(toolbox):
    """A model that answers with text first and calls write_report only after the reminder."""
    prov = ScriptedProvider([[("check_data", {})], [], [("write_report", {"title": "T", "markdown": "Body"})]])
    res = run_agent(prov, toolbox, verbose=False)
    assert len(prov.reminders) == 1 and len(res["reports"]) == 1
    assert "no report written" not in open(res["reports"][0], encoding="utf-8").read()


# ---------------------------------------------------------------- provider adapters with fake clients

class _Block(SimpleNamespace):
    def model_dump(self):
        return dict(vars(self))


def test_anthropic_adapter_round_trip(monkeypatch):
    sent = []
    responses = [SimpleNamespace(content=[_Block(type="text", text="Checking."),
                                          _Block(type="tool_use", id="tu1", name="check_data", input={})]),
                 SimpleNamespace(content=[_Block(type="text", text="All good.")])]
    p = AnthropicProvider.__new__(AnthropicProvider)
    p.model, p.max_tokens, p.messages = "m", 100, []
    import copy

    p.client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: (sent.append(copy.deepcopy(kw)), responses[len(sent) - 1])[1]))
    t = p.start("sys", Toolbox.SPECS, "goal")
    assert t.text == "Checking." and t.calls[0].name == "check_data" and sent[0]["tools"][0]["name"] == "check_data"
    t2 = p.reply([(t.calls[0], '{"ok": 1}', False)])
    tool_result = sent[1]["messages"][-1]["content"][0]
    assert tool_result == {"type": "tool_result", "tool_use_id": "tu1", "content": '{"ok": 1}', "is_error": False}
    assert t2.text == "All good." and not t2.calls


def test_openai_adapter_round_trip():
    sent = []
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="run_experiment", arguments='{"name": "a", "rationale": "b"}'),
                         model_dump=lambda: {"id": "c1", "type": "function"})
    responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tc]))]),
                 SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Done", tool_calls=None))])]
    p = OpenAIProvider.__new__(OpenAIProvider)
    p.model, p.messages = "m", []
    import copy

    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: (sent.append(copy.deepcopy(kw)), responses[len(sent) - 1])[1])))
    t = p.start("sys", Toolbox.SPECS, "goal")
    assert sent[0]["tools"][1]["function"]["parameters"]["required"] == ["name", "rationale"]
    assert t.calls == [ToolCall("c1", "run_experiment", {"name": "a", "rationale": "b"})]
    t2 = p.reply([(t.calls[0], "{}", False)])
    assert sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "{}"}
    assert sent[1]["messages"][-2]["tool_calls"] == [{"id": "c1", "type": "function"}]
    assert t2.text == "Done" and not t2.calls


def test_pick_groq_model():
    from streetair.agent.providers import pick_model

    assert pick_model(["whisper-large-v3", "llama-3.1-8b-instant", "openai/gpt-oss-120b", "qwen/qwen3-32b"]) \
        == "openai/gpt-oss-120b"
    assert pick_model(["whisper-large-v3", "meta-llama/llama-guard-4-12b", "qwen/qwen3-32b"]) == "qwen/qwen3-32b"
    assert pick_model(["some-new-model", "whisper-large-v3"]) == "some-new-model"
    with pytest.raises(RuntimeError):
        pick_model(["whisper-large-v3"])


def test_unicode_report_and_unexpected_errors(toolbox, monkeypatch):
    """Titles with non-ASCII characters are written as UTF-8 (Windows default encodings cannot), and an
    unexpected exception inside a tool comes back as an error instead of ending the session."""
    text, err = toolbox.call("write_report", {"title": "ERA5 → street level, 0.5 °C", "markdown": "Δ ≈ 0"})
    assert not err
    path = json.loads(text)["path"]
    assert "ERA5 → street level" in open(path, encoding="utf-8").read()
    monkeypatch.setattr(toolbox, "tool_check_data", lambda: 1 / 0)
    text, err = toolbox.call("check_data", {})
    assert err and "ZeroDivisionError" in json.loads(text)["error"]
    out = json.loads(toolbox.call("run_experiment", {"name": "a", "rationale": "x"})[0])
    assert out["effective_params"]["num_leaves"] == CFG["model"]["lightgbm"]["num_leaves"] and out["train_rows_per_fold"] > 0
