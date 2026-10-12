# Leaf evals

- Put photos in `evals/images/` and one line per photo in `cases.jsonl` (format in its header).
- Aim for 15-20 cases: 5+ plants, 8+ problems, at least 3 abiotic (sunscald, overwatering, nutrient) and 2 poor photos (tag `poor_photo`).
- Mark about 5 cases `"split": "test"` and never tune prompts on them. Report the test split's numbers in the README.
- Run: `python scripts/eval.py --split tune` while tuning, `--split test` for the final numbers.

## Photo licenses

| File | Source | License |
| --- | --- | --- |
| _your photos_ | taken by the team | MIT (this repo) |
