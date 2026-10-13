# Leaf eval set

`cases.jsonl` lists one case per line (lines starting with `//` are comments). `scripts/eval.py` runs Leaf on them and scores the result.

## Splits

- **test**: the held-out split we report. These are the maintainers' own labelled phone photos (added to `evals/images/`). Never tune prompts or weights on them.
- **tune**: used while building and tuning Leaf. The images below come from Wikimedia Commons under licences that allow reuse.

The tune photos are archival, so their weather is unknown: their cases carry no region, and the weather tool reports that no location was given. Lab-style and curated photos flatter any model compared with real phone photos, which is why the test split is our own.

## Licences (tune split)

All images were retrieved on 2026-10-12. Licence and author were read from the Commons API metadata; each label is stated by the uploader in the file description. Changes made: downscaled to at most 1600 px on the long side where larger, and re-encoded as JPEG (quality 85). Each modified file keeps its original licence; CC BY-SA files are shared under the same licence.

| File | Condition | Crop | Author | Licence | Source | Changes |
| --- | --- | --- | --- | --- | --- | --- |
| `tomato_early_blight_1.jpg` | tomato early blight | tomato | Clemson University - USDA Cooperative Extension Slide Series, Bugwood.org (image 1233052) | CC BY 3.0 US | [Commons](https://commons.wikimedia.org/wiki/File:Alternaria_solani_-_leaf_lesions.jpg) | resized 3072x3072 to 1600x1600, re-encoded as JPEG q85 |
| `tomato_early_blight_2.jpg` | tomato early blight | tomato | Jerzy Opioła | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Alternaria_solani_a1_(2).jpg) | resized 3792x2844 to 1600x1200, re-encoded as JPEG q85 |
| `tomato_early_blight_3.jpg` | tomato early blight | tomato | Dwight Sipler | CC BY 2.0 | [Commons](https://commons.wikimedia.org/wiki/File:Early_blight_on_tomato_leaves_(7871930010).jpg) | resized 3872x2592 to 1600x1071, re-encoded as JPEG q85 |
| `tomato_septoria_leaf_spot_1.jpg` | tomato septoria leaf spot | tomato | Dr Parthasarathy Seethapathy, Tamil Nadu Agricultural University, Bugwood.org | CC BY 3.0 US | [Commons](https://commons.wikimedia.org/wiki/File:Septoria_lycopersici_malagutii_leaf_spot_on_tomato_leaf.jpg) | resized 3072x1607 to 1600x837, re-encoded as JPEG q85 |
| `tomato_septoria_leaf_spot_2.jpg` | tomato septoria leaf spot | tomato | Wolan268 | CC0 1.0 | [Commons](https://commons.wikimedia.org/wiki/File:Tomato_septoria_leaf_spot_3006.jpg) | resized 3840x2160 to 1600x900, re-encoded as JPEG q85 |
| `late_blight_1.jpg` | late blight | tomato | Dwight Sipler | CC BY 2.0 | [Commons](https://commons.wikimedia.org/wiki/File:Late_blight_on_tomato_leaf_(7871756748).jpg) | resized 3872x2592 to 1600x1071, re-encoded as JPEG q85 |
| `late_blight_2.jpg` | late blight | tomato | Scot Nelson | CC0 1.0 | [Commons](https://commons.wikimedia.org/wiki/File:Tomato_late_blight_leaf_sporulating_lesions_1_(5816170621).jpg) | resized 2784x1856 to 1600x1067, re-encoded as JPEG q85 |
| `late_blight_3.jpg` | late blight | potato | Björn Andersson (SLU) | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Potato_Late_Blight.JPG) | resized 2048x1536 to 1600x1200, re-encoded as JPEG q85 |
| `powdery_mildew_1.jpg` | powdery mildew | squash | Dmitry Brant | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Powdery_mildew_9.jpg) | resized 6000x4000 to 1600x1067, re-encoded as JPEG q85 |
| `powdery_mildew_2.jpg` | powdery mildew | zucchini | Schlaghecken Josef | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Zucchini_Echter-Mehltau_Schlaghecken.jpg) | resized 4608x3456 to 1600x1200, re-encoded as JPEG q85 |
| `downy_mildew_1.jpg` | downy mildew | cucumber | Christian Hummert (Ixitixel) | CC BY-SA 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Falscher_Mehltau.jpg) | re-encoded as JPEG q85 |
| `downy_mildew_2.jpg` | downy mildew | grape | Bruce Cook (via iNaturalist) | CC0 1.0 | [Commons](https://commons.wikimedia.org/wiki/File:Plasmopara_viticola_305150885.jpg) | resized 1536x2048 to 1200x1600, re-encoded as JPEG q85 |
| `rust_1.jpg` | rust | rose | Cec-clp | CC0 1.0 | [Commons](https://commons.wikimedia.org/wiki/File:Rose-Rust-2.jpg) | resized 2384x2384 to 1600x1600, re-encoded as JPEG q85 |
| `rust_2.jpg` | rust | common bean | Jerzy Opioła | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Uromyces_appendiculatus_a1_(1).jpg) | resized 3668x2751 to 1600x1200, re-encoded as JPEG q85 |
| `rose_black_spot_1.jpg` | rose black spot | rose | Stephen James McWilliam (via iNaturalist) | CC BY 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Diplocarpon_rosae_102374407.jpg) | resized 2048x1365 to 1600x1066, re-encoded as JPEG q85 |
| `rose_black_spot_2.jpg` | rose black spot | rose | Stephen James McWilliam (via iNaturalist) | CC0 1.0 | [Commons](https://commons.wikimedia.org/wiki/File:Diplocarpon_rosae_94746674.jpg) | resized 2048x1365 to 1600x1066, re-encoded as JPEG q85 |
| `bacterial_spot_1.jpg` | bacterial spot | pepper | Scot Nelson (photograph by Chelsea Hardin) | CC0 1.0 | [Commons](https://commons.wikimedia.org/wiki/File:Bacterial_leaf_spot_of_pepper_(14954536360).jpg) | resized 2448x3264 to 1200x1600, re-encoded as JPEG q85 |
| `bacterial_spot_2.jpg` | bacterial spot | pepper | Scot Nelson | CC0 1.0 | [Commons](https://commons.wikimedia.org/wiki/File:Bacterial_leaf_spot_of_pepper_(Capsicum_sp.)_(43614805831).jpg) | resized 3886x3888 to 1599x1600, re-encoded as JPEG q85 |
| `apple_scab_1.jpg` | apple scab | apple | AfroBrazilian | CC BY-SA 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Venturia_inaequalis_01.JPG) | re-encoded as JPEG q85 |
| `apple_scab_2.jpg` | apple scab | apple | Lin linao | CC BY-SA 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Camuesta_con_sarna.JPG) | resized 2048x1202 to 1600x939, re-encoded as JPEG q85 |
| `magnesium_deficiency_1.jpg` | magnesium deficiency | maize | Alandmanson | CC BY 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Mg_deficiency_Vulindlela_1995-3-30_2018_01_23_2235.jpg) | re-encoded as JPEG q85 |
| `magnesium_deficiency_2.jpg` | magnesium deficiency | maize | Agronom | CC BY-SA 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Maize_of_magnesium_deficiency,_symptoms_on_leaves.jpg) | re-encoded as JPEG q85 |

Licence texts: [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/), [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/), [CC BY 3.0 US](https://creativecommons.org/licenses/by/3.0/us/), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/), [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).

Known gaps: no clearly labelled tomato bacterial spot (both bacterial spot images are pepper), no nitrogen deficiency, and no uploader-confirmed healthy controls, so none are included.

## Adding test photos

Put each photo in `evals/images/test/` and add a line such as:

```json
{"image": "evals/images/test/tomato_1.jpg", "expected": ["early blight", "alternaria"], "crop": "tomato", "notes": "", "region": "Ohio", "split": "test", "tag": "clear"}
```

`expected` lists accepted names (case-insensitive substring match against Leaf's candidates). `tag` groups cases in the report, e.g. `clear`, `subtle`, `poor_photo`, `abiotic`.
