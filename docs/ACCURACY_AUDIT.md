# Accuracy audit (tune split, October 2026)

Every wrong top-1 answer on the 52-photo tune split, classified by hand after looking at the photo
and at what the vision step reported. Tune split only: nothing here was fitted on held-out photos.
Runs: `sprint-q0` (Quick, checklist off), `sprint-q1` (Quick, checklist on), `sprint-deep`,
`sprint-auto`, all logged in `evals/results.jsonl` (not committed) and summarised in the README.
Tools: `scripts/audit_misses.py` (lists misses with what the vision step saw),
`scripts/fit_verdicts.py` (verdict thresholds), `scripts/calibrate.py` (Deep blend and confidence).

## Step 1: naming first

Names are resolved by `backend/diseases.py`: one canonical id per condition, with the names US
extension services use, scientific names, and the look-alikes that must not count (bacterial speck
is not bacterial spot, gray leaf spot is not early blight, rust mites are not rust, edema is not
root rot). Rescoring every earlier run with it changed no result, and none of the 25 Quick misses
below is the right condition under another name. **Naming mismatches: 0.** The baseline is honest.

## Quick misses (sprint-q0: 25 of 52 wrong, 12 of them right in the top 3)

| Class | Count | Photos |
| --- | --- | --- |
| Reasoning error: the sign was seen, the ranking went elsewhere | 12 | bacterial_spot_1, bacterial_spot_2, flea_beetle_1, gray_mold_1, herbicide_injury_1, iron_chlorosis_2, late_blight_3, magnesium_deficiency_2, nitrogen_deficiency_1, potassium_deficiency_1, rose_black_spot_1, sunscald_2 |
| Vision missed (or invented) a feature | 6 | downy_mildew_1 (angular patches read as interveinal chlorosis), late_blight_1 and late_blight_2 (a white "powdery coating" reported that is not there; no water-soaked margin), mosaic_virus_1 (no mottling seen), potato_early_blight_1 (no rings seen), rust_3 (orange cedar-apple rust spots read as shot hole) |
| Genuinely ambiguous from this photo | 7 | apple_scab_1 (purplish blotches that fit Alternaria blotch as well), apple_scab_2 (a young shoot with little visible), downy_mildew_2 (holes, no sign of downy mildew from above), downy_mildew_3 (basil canopy that looks healthy), frost_injury_2 (dead plants: frost and drought look alike without the date), manganese_deficiency_1 (Mn vs Mg needs leaf age), potassium_deficiency_2 (interveinal soybean chlorosis, which fits Fe or Mn as well) |
| Naming | 0 | |

What this says: half the misses are reasoning, and 10 of those 12 had the right answer in the
top three. That is what a second, slower look can fix, and it is what Deep did: Deep was right on 8
of Quick's misses (both bacterial spots, apple_scab_1, herbicide, iron, potassium, nitrogen, potato
early blight) and wrong on 6 Quick had right. Late blight (3 of 4 photos missed by both modes) is
the clearest systematic failure: the vision model reports a powdery coating on late blight
lesions, which pulls the diagnosis to powdery mildew or early blight.

## Step 3: the symptom checklist

The vision step can fill a checklist (lesion shape and margin, halos, rings, dark dots, powdery or
fuzzy growth and which side, the yellowing pattern, which leaves first, insects, frass, webbing;
each present / absent / unsure), which Nemotron then reads with the description. On the same 52
photos it fixed 5 Quick cases and broke 7 (top-1 48% vs 52%, top-3 71% vs 75%, Brier 0.251 vs
0.264). It did not earn its place, so it is off by default (`LEAF_CHECKLIST=1` turns it on). Where
it fails is visible in the logs: the vision model marks "powdery growth: present" on late blight
and "fuzzy growth: present" on herbicide-cupped soybean, so a structured form does not stop it
seeing features that are not there; it only makes them look more certain.

## Deep: why so many lab referrals

With the first thresholds, Deep sent 30 of 52 cases to a lab. Breaking that down by reason:

| Reason | Cases | Top-1 wrong |
| --- | --- | --- |
| The second opinion disagreed and gave the leader under 50% | 17 | 10 |
| No care-plan step survived the claim check | 10 | 2 |
| A high-stakes condition (late blight) in the top two | 2 | 1 |
| Causes too spread out | 1 | 1 |

The "no plan" referrals were a bug, not uncertainty: in 9 of the 10 the investigator finished
without searching, so there were no sources to cite and every step was dropped. Deep now searches
for the leading diagnosis itself when the investigator did not (as Quick always does), and a case
whose plan still has no supported step is capped at Likely with "ask your extension office"
instead of being sent to a lab. The second-opinion rule was kept: when the critic disagreed
strongly the top answer was wrong 59% of the time, against 29% when it agreed, a weak but real
signal, and refitting it on one run of 52 would be fitting noise.

## Review fixes after the runs

A review of the sprint found ways a case could end Confident or skip a lab referral, now fixed
and tested: a high-stakes condition named second in one candidate ("Tobamovirus (ToBRFV or
ToMV)", "early blight, possibly late blight") was not recognised; the second opinion's own
high-stakes alternative was never checked; photos no model could read counted as no photo rather
than a poor one; and a Deep plan that failed outright was capped at Likely instead of going to a
lab. Replaying the logged runs through the fixed rules gives the same numbers as the README.
