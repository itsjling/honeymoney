# Golden Datasets

HoneyMoney keeps parser and categorization regressions small, portable, and
reviewable by storing synthetic golden cases under `tests/fixtures`.

Golden cases should describe behavior we want to preserve. They are not raw
bank statements, model transcripts, or private financial data.

## Layout

Import profile cases live here:

```text
tests/fixtures/import_profiles/<profile_id>/<case_name>/
  input.csv      # CSV profiles only
  tables.json    # PDF table extraction cases
  words.json     # PDF word-coordinate extraction cases
  expected.json  # normalized rows plus expected warnings
```

Categorization cases live here:

```text
tests/fixtures/categorization/deterministic/<case_name>/
  rows.json
  rules.json
  config.json
  expected.json

tests/fixtures/categorization/ollama/<case_name>/
  rows.json
  response.json | response_template.json
  expected.json
```

The tests compare only the fields listed in `expected.json`. This lets a case
focus on the contract it protects without duplicating every output column.

Use `_flags_contains` when a row should contain a flag but the exact full flag
string is not the point of the test:

```json
{
  "merchant": "PARKNSHOP",
  "category": "Groceries",
  "_flags_contains": ["matched_rule:parksnshop"]
}
```

## Adding an Import Golden

1. Pick the profile id from `honeymoney/data/profiles`.
2. Create a short, descriptive case folder:

   ```bash
   mkdir -p tests/fixtures/import_profiles/mox_credit_card_pdf/foreign_currency_suffix
   ```

3. Add one synthetic input fixture:
   - `input.csv` for CSV profiles.
   - `tables.json` for `pdfplumber.extract_tables()` style data.
   - `words.json` for `pdfplumber.extract_words()` coordinate data.

4. Add `expected.json` with the normalized rows that should be produced.
5. Add a method in `tests/test_import_profiles.py` that calls
   `assert_import_case(...)` for the new case.
6. Run:

   ```bash
   python3 -m unittest tests.test_import_profiles
   python3 -m unittest
   ```

Good import goldens are tiny. Prefer one behavior per case:

- balance rows ignored: `OPENING BALANCE`, `CLOSING BALANCE`,
  `PREVIOUS BALANCE`, and institution-specific variants
- debit/credit sign handling
- credit-card `CR` suffix handling
- transaction date vs posting date normalization
- multiline PDF rows
- word-coordinate PDF extraction
- amount/merchant alignment regressions, such as `24/7 FITNESS` with `498.00`
- source metadata: `source_file`, `source_page`, `source_row`

## Adding a Categorization Golden

For deterministic rules:

1. Add `rows.json`, `rules.json`, `config.json`, and `expected.json` under
   `tests/fixtures/categorization/deterministic/<case_name>/`.
2. Add or reuse a test in `tests/test_transaction_categorization.py`.
3. Cover the smallest behavior that matters: rule priority, exact matching,
   keyword matching, regex matching, thresholds, flags, notes, owner, payment
   method, and stability of already-categorized rows.

For Ollama behavior:

1. Add rows under `tests/fixtures/categorization/ollama/<case_name>/`.
2. Use a fake response fixture. Do not commit live model output as a golden.
3. Assert request behavior in `expected.json` when relevant, such as:
   - unresolved rows are sent
   - categorized rows are not sent
   - source-only fields are absent from the prompt payload
   - batches are sent in deterministic order

Run:

```bash
python3 -m unittest tests.test_transaction_categorization
python3 -m unittest
```

## Privacy Rules

Never commit private statements or raw personal financial data. A golden should
be synthetic but structurally faithful:

- Keep column order, line breaks, date formats, amount placement, and coordinate
  shape.
- Replace names, card numbers, addresses, account numbers, and private merchants.
- Keep a real-looking merchant only when it is needed to protect parser behavior,
  such as punctuation in `24/7 FITNESS`.
- Use round, fake amounts unless the exact amount exposed the bug.

## Updating Existing Goldens

When a parser or categorizer change intentionally changes output:

1. Run the relevant focused test first.
2. Inspect the failing diff and decide whether behavior or the golden is wrong.
3. Update `expected.json` manually.
4. Keep fixture input unchanged unless the case itself no longer describes the
   behavior.
5. Run the focused suite and then the full suite.

Avoid blind regeneration. Golden updates should be human-reviewed because they
define the behavior we promise to keep.

## Useful Commands

Run import goldens:

```bash
python3 -m unittest tests.test_import_profiles
```

Run categorization goldens:

```bash
python3 -m unittest tests.test_transaction_categorization
```

Run everything:

```bash
python3 -m unittest
```

Run live Ollama smoke separately:

```bash
HONEYMONEY_OLLAMA_MODEL=qwen2.5:7b-instruct \
  python3 scripts/live_ollama_categorization_smoke.py
```

The live smoke command is intentionally outside default test discovery because
it depends on the local Ollama service and installed model.
