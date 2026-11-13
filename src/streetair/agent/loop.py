"""The agent loop: send the goal, run the tool calls the model asks for, return the results,
until the model stops calling tools or a step limit is reached."""

from __future__ import annotations

from typing import Any

from .tools import FEATURE_GROUPS, Toolbox

SYSTEM = """You are an ML experiment assistant for a model that corrects ERA5 2 m air temperature to street \
level in Phoenix. The model is LightGBM on site features (land cover, vegetation, elevation), time of day \
and season, and ERA5 weather. It is evaluated leave-one-station-out over the weather stations, so every \
result is for stations the model never saw.

Work like a careful ML engineer:
1. Call check_data first and note any issues.
2. Run a baseline with default settings before changing anything.
3. Change one thing at a time so each result is interpretable (one hyperparameter, or one feature group \
dropped). State the rationale for each run. Every comparison must be against an experiment that differs \
ONLY in that one change: if you adopt new hyperparameters, run ablations with those same hyperparameters \
and compare them with the matching run, not with the original baseline. Quote deltas against that run.
4. Differences under about 0.01 C in MAE are within noise for this setup; do not over-interpret them. \
Look at night and day separately, and at the per-network breakdown, not only overall MAE.
5. If a change gives exactly the same result as the baseline, find out why before moving on: it may \
equal the default already, or another setting may be the binding constraint (for example \
min_child_samples limits how many leaves a tree can grow, whatever num_leaves allows). Check the \
effective settings ("effective_params") and "train_rows_per_fold" returned by run_experiment.
6. Stay within the experiment budget. When it is used up or the picture is clear, call write_report once.

The report is for the ML team: what was tested and why, what the results show (with numbers), which \
configuration to adopt and why, and caveats (few stations, subsampled hours, possible overfitting to \
station types). A results table and the full tool log are appended automatically.

Feature groups available for ablation: """ + ", ".join(f"{g} ({len(c)} features)" for g, c in FEATURE_GROUPS.items())

DEFAULT_GOAL = ("Run a small hyperparameter and feature study for the LightGBM correction: establish a "
                "baseline, test the effect of tree complexity and regularisation, and check which feature "
                "groups matter by leaving each important group out. Recommend a configuration and write the report.")


REMINDER = ("You ended without calling write_report. Call write_report now with your report as the markdown "
            "argument; do not run more experiments.")


def run_agent(provider, toolbox: Toolbox, goal: str = DEFAULT_GOAL, max_steps: int = 30,
              verbose: bool = True) -> dict[str, Any]:
    turn = provider.start(SYSTEM, Toolbox.SPECS, goal)
    steps, reminded = 0, False
    while steps < max_steps:
        if not turn.calls:
            if toolbox.reports or reminded:
                break
            # Some models write the report as their final message instead of calling the tool: remind once
            reminded = True
            if verbose:
                print("[loop] no report written yet; reminding the agent", flush=True)
            turn = provider.remind(REMINDER)
            continue
        steps += 1
        if verbose and turn.text.strip():
            print(f"[agent] {turn.text.strip()}", flush=True)
        results = []
        for c in turn.calls:
            if verbose:
                print(f"  -> {c.name} {_brief(c.args)}", flush=True)
            text, err = toolbox.call(c.name, c.args)
            if verbose:
                print(f"     {toolbox.log[-1]['summary']}", flush=True)
            results.append((c, text, err))
        if any(c.name == "write_report" and not err for c, _, err in results):
            break                     # the report is the last step: no further request (saves tokens on free tiers)
        try:
            turn = provider.reply(results)
        except Exception as e:  # noqa: BLE001 - keep everything already done, end the session cleanly
            if verbose:
                print(f"[loop] provider error, stopping: {type(e).__name__}: {e}", flush=True)
            turn = type(turn)(text=f"Session ended by a provider error: {type(e).__name__}: {e}")
            break
    if verbose and turn.text.strip():
        print(f"[agent] {turn.text.strip()}", flush=True)
    if not toolbox.reports:           # always leave a record, even if the model never wrote one
        toolbox.call("write_report", {"title": "Experiment log (no report written by the agent)",
                                      "markdown": "The agent stopped without writing a report. "
                                                  f"Stopped after {steps} steps. Final message:\n\n{turn.text}"})
    return {"steps": steps, "experiments": toolbox.experiments_run, "reports": [str(p) for p in toolbox.reports],
            "final_text": turn.text}


def _brief(args: dict[str, Any]) -> str:
    a = {k: v for k, v in (args or {}).items() if k != "markdown"}
    s = str(a)
    return s if len(s) < 160 else s[:157] + "..."
