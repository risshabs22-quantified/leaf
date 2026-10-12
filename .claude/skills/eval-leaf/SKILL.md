---
name: eval-leaf
description: Measure Leaf's diagnosis accuracy on labelled photos and improve prompts based on the failures. Use after changing prompts, models or the pipeline. Spends about 1-3 cents per case.
---
Evaluation-driven prompt tuning (Anthropic's pattern: measure, read failures, change one thing, measure again).

1. Check `evals/cases.jsonl` has at least 5 real `tune` cases (lines not starting with `//`) and the images exist. If not, ask the user for photos they own the rights to, with the true diagnosis.
2. Run `.venv/bin/python scripts/eval.py --split tune` and keep only the summary lines and the ✗ lines. Never tune on the `test` split; run it only for final reported numbers.
3. Read 2-3 failures in `evals/results.jsonl`. Classify each: vision missed a symptom / reasoning picked wrong cause / confidence badly calibrated / search found nothing useful.
4. Change ONE thing (usually a prompt in `backend/prompts.py`), run `make check`, then rerun the same cases.
5. Keep the change only if top-3 hit rate rose or escalation got better calibrated. Record before/after in `docs/PROGRESS.md` under "Eval log".
6. Reply in at most 8 lines: before → after, what changed, what to try next.
