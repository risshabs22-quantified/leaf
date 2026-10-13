# Leaf eval set

`cases.jsonl` lists one case per line (lines starting with `//` are comments). `scripts/eval.py` runs Leaf on them and scores the result.

## Splits

- **test**: the held-out split we report. These are the maintainers' own labelled phone photos (added to `evals/images/`). Never tune prompts or weights on them.
- **tune**: used while building and tuning Leaf. The images below come from Wikimedia Commons under licences that allow reuse.

The tune photos are archival, so their weather is unknown: their cases carry no region, and the weather tool reports that no location was given. Lab-style and curated photos flatter any model compared with real phone photos, which is why the test split is our own.

## Licences (tune split)

**The images in `evals/images/` are not covered by Leaf's MIT licence.** Each file keeps its own licence, listed below with its author, source link and the changes made. Only CC0, public domain, CC BY and CC BY-SA files are used; anything NonCommercial, NoDerivatives or without a clear licence is excluded. If you reuse a file, follow its licence: credit the author, link the licence, say it was changed, and share CC BY-SA files (including our resized copies) under CC BY-SA.

All images were retrieved from Wikimedia Commons on 2026-10-12 and 2026-10-13. Licence and author were read from the Commons API metadata (each licence was checked again at download time); each label is stated by the uploader in the file title or description. Changes made: downscaled to at most 1600 px on the long side where larger (the second batch was fetched as Commons' 1280 px thumbnails, as Wikimedia asks of bulk downloads), and re-encoded as JPEG (quality 85).

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
| `iron_chlorosis_1.jpg` | iron chlorosis | rose | Natalka Ukraine | CC BY 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Iron_chlorosis_on_young_rose_leaves_in_a_garden,_by_baby-bear.org.jpg) | re-encoded as JPEG q85 |
| `magnesium_deficiency_3.jpg` | magnesium deficiency | alder buckthorn | Karduelis | Public domain | [Commons](https://commons.wikimedia.org/wiki/File:Frangula_alnus_with_magnesium_deficiency.jpg) | resized 3648x2736 to 1600x1200, re-encoded as JPEG q85 |
| `sunscald_1.jpg` | sunscald | bell pepper (fruit) | Don Ferrin, Louisiana State University Agricultural Center, Bugwood.org | CC BY 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:5411486-PPT-Suncald_on_exposed_bell_pepper_fruit.jpg) | resized 3072x2304 to 1600x1200, re-encoded as JPEG q85 |
| `herbicide_injury_1.jpg` | herbicide injury | soybean | United Soybean Board | CC BY 2.0 | [Commons](https://commons.wikimedia.org/wiki/File:Herbicide_Injury_to_Soybeans_(9620825285).jpg) | resized 3456x4608 to 1200x1600, re-encoded as JPEG q85 |
| `aphids_1.jpg` | aphids | cherry | ML5 | CC0 | [Commons](https://commons.wikimedia.org/wiki/File:Black_Cherry_Aphid.jpg) | resized 4032x1960 to 1600x778, re-encoded as JPEG q85 |
| `leaf_miner_1.jpg` | leaf miner | unspecified vegetable | David Riley, University of Georgia, Bugwood.org | CC BY 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:2510028-PPT-Leafminer_fly_(Genus_Liriomyza).jpg) | resized 3072x2048 to 1280x853, re-encoded as JPEG q85 |
| `flea_beetle_1.jpg` | flea beetle | Chinese cabbage | Whitney Cranshaw, Colorado State University, Bugwood.org | CC BY 3.0 US | [Commons](https://commons.wikimedia.org/wiki/File:Phyllotreta_cruciferae_(02).jpg) | resized 3072x2048 to 1280x853, re-encoded as JPEG q85 |
| `scale_insects_1.jpg` | soft scale | ficus | Whitney Cranshaw, Colorado State University, Bugwood.org | CC BY 3.0 US | [Commons](https://commons.wikimedia.org/wiki/File:Coccus_hesperidum01.jpg) | resized 3072x2048 to 1280x853, re-encoded as JPEG q85 |
| `late_blight_4.jpg` | late blight | potato | Mary Ann Hansen, Virginia Polytechnic Institute and State University, Bugwood.org | CC BY 3.0 US | [Commons](https://commons.wikimedia.org/wiki/File:Phytophthora_infestans_on_potato_leaf.jpg) | resized 3072x2046 to 1280x852, re-encoded as JPEG q85 |
| `downy_mildew_3.jpg` | downy mildew | basil | Forest and Kim Starr | CC BY 3.0 US | [Commons](https://commons.wikimedia.org/wiki/File:Starr-110822-8248-Ocimum_basilicum-leaves_with_basil_downy_mildew_Peronospora_belbahrii-Hawea_Pl_Olinda-Maui_(24476536533).jpg) | resized 3648x2736 to 1280x960, re-encoded as JPEG q85 |
| `anthracnose_1.jpg` | anthracnose | lima bean (pods) | David B. Langston, University of Georgia, Bugwood.org | CC BY 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Colletotrichum_lindemuthianum.jpg) | re-encoded as JPEG q85 |
| `rust_3.jpg` | cedar-apple rust | crabapple | Littleinfo | Public domain | [Commons](https://commons.wikimedia.org/wiki/File:Cedar_apple_rust_on_crab_apple_leaf_upper_surface.JPG) | resized 3008x2000 to 1280x851, re-encoded as JPEG q85 |
| `fire_blight_1.jpg` | fire blight | pear | Scot Nelson | CC0 | [Commons](https://commons.wikimedia.org/wiki/File:Pear_Fire_blight_(Pathogen_Erwinia_amylovora)_(12226126143).jpg) | re-encoded as JPEG q85 |
| `iron_chlorosis_2.jpg` | iron chlorosis | hydrangea | Pierre.hamelin | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Chlorose_hortensia.jpg) | resized 4608x3456 to 1280x960, re-encoded as JPEG q85 |
| `nitrogen_deficiency_1.jpg` | nitrogen deficiency | common bean | Rasbak | CC BY-SA 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Phaseolus_vulgaris_nitrogen_deficiency.jpg) | resized 2448x3264 to 1200x1600, re-encoded as JPEG q85 |
| `potassium_deficiency_1.jpg` | potassium deficiency | tomato | Goldlocki | CC BY-SA 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Tomate_Blatt_Kalimangel.jpg) | resized 3264x2448 to 1280x960, re-encoded as JPEG q85 |
| `potassium_deficiency_2.jpg` | potassium deficiency | soybean | Alandmanson | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:K-deficient_soyabean_Cedara_2013_02_08_08_24_24_7488.jpg) | re-encoded as JPEG q85 |
| `manganese_deficiency_1.jpg` | manganese deficiency | rose | Sten Porse | CC BY-SA 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Manganese-deficiency-rose.jpg) | re-encoded as JPEG q85 |
| `sunscald_2.jpg` | sunburn | pepper (leaves) | Schlaghecken Josef | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Paprika_Sonnenbrand-1-Schlaghecken.jpg) | resized 3029x2362 to 1280x998, re-encoded as JPEG q85 |
| `blossom_end_rot_1.jpg` | blossom end rot | tomato | A13ean | CC BY-SA 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Blossom_end_rot.JPG) | resized 4272x2848 to 1280x853, re-encoded as JPEG q85 |
| `frost_injury_2.jpg` | frost damage | tomato | Famartin | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:2021-11-05_11_52_19_Tomato_plants_killed_by_frost_along_Indale_Court_in_the_Franklin_Farm_section_of_Oak_Hill,_Fairfax_County,_Virginia.jpg) | resized 3024x4032 to 1200x1600, re-encoded as JPEG q85 |
| `drought_stress_1.jpg` | drought stress | forsythia | Famartin | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:2023-06-04_16_12_41_A_forsythia_bush_wilting_during_a_dry_spell_along_Barbara_Lane_in_the_Mountainview_section_of_Ewing_Township,_Mercer_County,_New_Jersey.jpg) | resized 4032x3024 to 1280x960, re-encoded as JPEG q85 |
| `spider_mites_1.jpg` | spider mites | common bean | Rasbak | CC BY-SA 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Stokboon_bonenspintmijt_Phaseolus_vulgaris_with_Tetranychus_urticae.jpg) | resized 1535x1680 to 1280x1401, re-encoded as JPEG q85 |
| `japanese_beetle_1.jpg` | Japanese beetle | crabapple | Fred613 | CC BY-SA 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Feuilles_de_pommetier_attaqu%C3%A9es_par_le_Scarab%C3%A9e_japonais.JPG) | resized 4000x3000 to 1280x960, re-encoded as JPEG q85 |
| `potato_early_blight_1.jpg` | early blight | potato | Linnea Stridh | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Alternaria_solani_IMG_1661.jpg) | resized 4000x2664 to 1280x852, re-encoded as JPEG q85 |
| `tomato_septoria_leaf_spot_3.jpg` | septoria leaf spot | tomato | MerielGJones | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Septoria_leaf_spot_symptoms_on_tomato_leaf_(Septoria_lycopersici_on_Solanum_lycopersicum_leaf).jpg) | resized 2219x2728 to 1280x1574, re-encoded as JPEG q85 |
| `powdery_mildew_3.jpg` | powdery mildew | pumpkin | Schlaghecken Josef | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:K%C3%BCrbis-Cucurbita-Echter_Mehltau-Golovinomyces-1-JS.jpg) | resized 4624x3468 to 1280x960, re-encoded as JPEG q85 |
| `rust_4.jpg` | rose rust | rose | pinonbistro (via iNaturalist) | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Phragmidium_mucronatum_24565885.jpg) | resized 2048x1536 to 1280x960, re-encoded as JPEG q85 |
| `gray_mold_1.jpg` | gray mold | tomato | Rasbak at Dutch Wikipedia | CC BY-SA 3.0 | [Commons](https://commons.wikimedia.org/wiki/File:Botrytis_tomaat.jpg) | re-encoded as JPEG q85 |
| `mosaic_virus_1.jpg` | tomato mosaic virus | tomato | RosieScienceGal | CC BY-SA 4.0 | [Commons](https://commons.wikimedia.org/wiki/File:Leaf_with_ToMV.jpg) | re-encoded as JPEG q85 |

Licence texts: [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/), [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/), [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/), [CC BY 3.0 US](https://creativecommons.org/licenses/by/3.0/us/), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/), [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). "Public domain" files carry no licence conditions; the author is still credited.

Known gaps: no clearly labelled tomato bacterial spot (all bacterial spot images are pepper), no edema, no root rot or overwatering, and no uploader-confirmed healthy controls. Several photos are not of the leaf a gardener would photograph: `sunscald_1` and `blossom_end_rot_1` are fruit, `anthracnose_1` shows bean pods, `fire_blight_1` is a studio photo of a cut shoot, and `leaf_miner_1` names no crop. `mosaic_virus_1` is described as tobacco mosaic but its file name says ToMV; both resolve to mosaic virus. Rejected after a look: a field-wide frost photo, a microscope slide of Cercospora spores, a pepper field with no visible spots, and a fennel bulb with barely visible tipburn.

Candidates for growing the set later (strictly CC0, public domain or CC BY, found on 2026-10-13, not yet downloaded or checked by eye): Starr's *Agave attenuata* oedema (CC BY 2.0, the only edema example), frost on onion seedlings (Schlaghecken, CC BY 4.0), drought-stressed *Dubautia* (Starr, CC BY 3.0 US), two blossom end rot close-ups (CC0, public domain), spider mite webbing on tomato (Clemson/Bugwood, CC BY 3.0 US) and on barberry (CC0), powdery mildew on melon and rose (Bugwood, CC BY 3.0), two rose black spot (iNaturalist, CC0 and CC BY 4.0), apple scab (iNaturalist, CC BY 4.0), Botrytis on endive (Schlaghecken, CC BY 4.0), turnip and squash mosaic (CC BY 3.0 US, CC0).

## Adding test photos

Put each photo in `evals/images/test/` and add a line such as:

```json
{"image": "evals/images/test/tomato_1.jpg", "expected": ["early blight"], "condition": "early_blight", "crop": "tomato", "notes": "", "region": "Ohio", "split": "test", "tag": "clear"}
```

`condition` is the canonical id from `backend/diseases.py` (synonyms such as "Alternaria solani" count; look-alikes such as bacterial speck for bacterial spot do not). `expected` lists readable names and is used only for cases without a `condition`. `tag` groups cases in the report, e.g. `clear`, `subtle`, `poor_photo`, `abiotic`.
