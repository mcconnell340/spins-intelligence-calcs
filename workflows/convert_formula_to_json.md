# Workflow: Convert Formula to Whiz JSON

## Objective
Convert a human-readable SPINS measure formula (e.g., `SUM(DOLLARS) / MAX(HOUSEHOLD)`) into the structured JSON step array that Whiz uses to execute calculations.

## When to Use
- A measure in `SPINS LLM Descriptions.xlsx` (Measures tab) is flagged `To be added to Whiz` in column E and has `Calc` or `Script` in column G
- A new measure formula needs to be expressed in Whiz JSON format
- Validating that an existing clean formula matches its JSON representation

## Inputs Required
- **Measure name** — the human-readable name (e.g., "Non-Promo ARP")
- **Clean formula** — the readable formula string (e.g., `SUM(NONPROMODOLLARS) / SUM(NONPROMOUNITS)`)
  - For new measures: derive this from the description in column R and the column naming conventions below
  - For variant measures (PP, YAGO, 2YAGO): apply the suffix pattern from an existing base measure

## Column Name Conventions
| Period | Suffix | Example |
|--------|--------|---------|
| Current | (none) | `DOLLARS` |
| Prior Period | `_PP` | `DOLLARS_PP` |
| Year Ago (YAGO) | `_STLY` | `DOLLARS_STLY` |
| 2 Years Ago | `_2YAGO` | `DOLLARS_2YAGO` |

## Tool
**Script:** `tools/formula_to_json.py`
**Requires:** `ANTHROPIC_API_KEY` in `.env`

### Single measure
```bash
python tools/formula_to_json.py \
  --name "Non-Promo ARP" \
  --formula "SUM(NONPROMODOLLARS) / SUM(NONPROMOUNITS)"
```

### Batch (CSV with `name` and `formula` columns)
```bash
python tools/formula_to_json.py --batch measures.csv --output measures_with_json.csv
```
Output CSV will have an added `json` column.

## How the Tool Works
1. Loads all 222 examples from `beep bop.xlsx` as few-shot context
2. Sends the formula + examples to Claude with a structured prompt explaining all 7 JSON step types
3. Returns a validated JSON array ready to paste into Whiz

## JSON Step Types Reference
| Type | Use Case | Key Fields |
|------|----------|------------|
| `sum` | Sum an additive column | `column` |
| `max` | Max of a non-additive column (ACV, stores, household) | `column` |
| `division` | Numerator / denominator | `value`, `denominator`, `divByZeroResponse` |
| `multiplication` | Product of two refs | `values: [ref1, ref2]` |
| `subtraction` | from - value | `from`, `value` |
| `percent` | value / outOf (ratio) | `value`, `outOf`, `divByZeroResponse` |
| `percentChange` | (value / base) - 1 | `value`, `base`, `divByZeroResponse` |

## Handling Measure Families
Most measures come in a family: base + PP + YAGO + 2YAGO + % Change variants. Workflow:
1. Confirm the base measure formula (e.g., `SUM(NONPROMODOLLARS) / SUM(NONPROMOUNITS)`)
2. Generate JSON for the base
3. For PP/YAGO/2YAGO: substitute column suffixes and regenerate
4. For % Change: add a `percentChange` or `subtraction` step on top of two period variants

## Known Constraints / Gotchas
- Script-type measures (column J in Measures sheet) use named Whiz scripts like `WhizCalDivSumOfMax` — these may require a different conversion path and aren't always expressible as simple step arrays
- The tool uses `claude-sonnet-4-6` — if accuracy is low on complex formulas, review the output steps carefully and update `beep bop.xlsx` with corrected examples to improve future runs
- `divByZeroResponse` defaults to `0.0` for all division/percent/percentChange steps

## Output Quality Check
After generating JSON, verify:
1. Every `column` reference is a real SPINS column name
2. Every step `name` reference exists in a prior step
3. Steps are in correct dependency order
4. The final step name clearly identifies the measure outcome

## Improving the Tool
When a generated JSON is incorrect:
1. Fix it manually
2. Add the corrected example to `beep bop.xlsx` (columns: Measure Name, Measure Calculation, Measure Calculation (Clean))
3. The tool will pick it up automatically on next run — no code changes needed
