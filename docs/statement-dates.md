# Statement dates in parser output

`honeymoney.importers.parse_statement(profile, profile_id, input_path, config)`
returns a JSON-ready result with `transactions`, `warnings`, `statement_date`,
and `statement_date_source_pages`. Its date fields look like this:

```json
{
  "statement_date": "2026-05-31",
  "statement_date_source_pages": [1, 2]
}
```

`statement_date` is the complete printed statement date in `YYYY-MM-DD` form.
The page list uses one-based page numbers, sorted without repeats. Repeated
copies of the same date agree; different dates do not.

A missing, invalid, ambiguous, or partial date returns JSON `null`, with an
empty page list. CSV sources also return `null`.
Transaction dates, payment due dates, filenames, and the default month
or day used to resolve transaction years cannot supply this field.

The Mox PDF profiles use the printed statement period to resolve yearless
transaction and posting dates. They accept a transaction date up to 14 days
before its posting date for the supported layouts. The statement period end is
not the statement date and does not fill `statement_date`.

PDF profiles define extraction in `pdf.statement_date`. The `regex` must
capture a named `date` group, and `date_formats` lists the accepted complete
date formats, with a day, month, and four-digit year (`%Y`). Match the printed
statement-date label so other dates on the
page cannot supply the value. All matching dates must resolve to one date.

The bundled HSBC One, HSBC HK credit-card, Mox bank, and Mox credit-card PDF
profiles define these rules. Custom profiles without a rule return `null`.

These fields belong to the parser's source report. They do not add transaction
columns or change transaction year resolution. Consumers such as Myfi can save
the date on their imported document and use the page list to locate its source.
Honeymoney's workspace import service does not save these fields in its durable
import records.

The per-source reports from `_import_transactions` also include both fields.
Its failed and skipped reports use `null` and an empty page list.
`parse_statement` raises on parse failure, as the existing preview does.
`preview_profile_input` keeps its existing `(rows, warnings)` return shape.
