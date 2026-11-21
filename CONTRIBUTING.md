# Contributing

## Language: english in the repository

Everything that lives in this repository is written in **english**: pull request titles and
descriptions, commit subjects and bodies, code comments, docstrings, variable names, test names,
file headers and documentation.

German stays where a user reads it: the visible interface texts of the pages (labels, buttons,
messages) and the notes inside the data files that name a stock or a reason in words the user
sees.

Before pushing, read your own diff:

```
git diff master...HEAD -U0 | grep -E "^\+.*(#|//|/\*)"
```

A german comment in the diff is a defect, like a red test - even when the code is correct.

## Tests

```
python -m pytest -q
python -m ruff check .
```

Both have to be clean. The suite also covers the page's own JavaScript: `analytics/test_page_script.py`
and `analytics/test_page_scores.py` lift the shipped functions out of `index.html` and run them in
node, so a change to the scoring is exercised, not assumed.
