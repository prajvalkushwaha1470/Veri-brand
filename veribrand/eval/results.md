Test set: 24 labelled drafts (29 expected findings, 7 clean drafts)

| System | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|
| Keyword-only baseline (detection) | 29 | 6 | 0 | 0.829 | 1.0 | 0.906 |
| VeriBrand engine (detection) | 29 | 0 | 0 | 1.0 | 1.0 | 1.0 |
| VeriBrand engine (detection + correct label) | 29 | 0 | 0 | 1.0 | 1.0 | 1.0 |

Stress set (paraphrases / spelled-out numbers / slang the rules do not cover): recall 0/5. These are KNOWN LIMITATIONS of the rule+ledger layer; the optional grounded-AI pass is meant to cover them.
