-- Saved queries listed in the SQL console sidebar (PLAN.md §8).
-- Each query starts with a "-- name:" line and ends with a semicolon.

-- name: Top 20 banks by first-lien balances repricing or maturing within 12 months (latest quarter)
SELECT name, state,
       within_12m / 1e6        AS within_12m_mm,
       within_12m_pct_first_lien,
       within_12m_pct_assets
FROM v_cdr_bank_latest
ORDER BY within_12m DESC
LIMIT 20;

-- name: Industry repricing wall over time
SELECT report_date,
       (b_le_3m + b_3_12m) / 1e9 AS within_12m_bn,
       (b_1_3y + b_3_5y)   / 1e9 AS y1_5_bn,
       first_lien_total    / 1e9 AS first_lien_bn
FROM v_cdr_industry
ORDER BY report_date;

-- name: First resets by year, retained jumbo ARMs, base scenario
SELECT reset_year,
       SUM(bal_at_reset) / 1e9 AS bal_bn,
       SUM(w_loans)            AS weighted_loans
FROM v_reset_calendar
WHERE scenario = 'base' AND holder_segment = 'retained' AND conforming = 'NC'
GROUP BY 1 ORDER BY 1;
