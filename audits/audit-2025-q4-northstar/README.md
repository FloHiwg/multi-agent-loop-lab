# Northstar Q4 2025 audit fixture

This is a fictional, synthetic OfficeQA-style packet for testing Proofbench. It contains one master report with ten numeric claims and three basis documents that distribute direct, tabular, and computed evidence.

## Expected outcome

- 8 `supported` claims, including two that require arithmetic.
- 1 `contradicted` claim (net revenue retention).
- 1 `missing_evidence` claim (gross logo churn).
- Exact provenance and tolerance rules are recorded in `gold.yaml`.

The documents are synthetic and must not be treated as real company records.
