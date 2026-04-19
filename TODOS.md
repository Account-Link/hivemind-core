# TODOS

## ~~1. Build query agent eval harness~~ DONE
Built 7 scenarios (aggregation, filtering, joins, empty_tables, time_series, parameterized, schema_discovery) with 3-component scoring (SQL safety 30%, structure 40%, answer 30%). First 3 scenarios scored 100%.

## ~~2. Conditional recall explainer document~~ DONE
Written at `docs/conditional-recall.md`. Covers binary access problem, privacy-quality frontier, CLI examples, honest caveats.

## ~~3. Full eval coverage~~ DONE

### Query eval (7 scenarios, 25 test cases): 99.3%
| Scenario | Score |
|---|---|
| aggregation | 100% |
| filtering | 100% |
| joins | 100% |
| empty_tables | 100% |
| time_series | 95.6% |
| parameterized | 100% |
| schema_discovery | 100% |

### Scope eval (8 realistic scenarios): 91%
| Scenario | Score |
|---|---|
| pii_redaction | 100% |
| write_blocking | 100% |
| aggregation_only | 100% |
| column_allowlist | 100% |
| row_level_security | 100% |
| chat_history_privacy | 63% (over-redacted message column) |
| health_records | 67% (over-restricted small result sets) |
| social_media_public | 100% |

### Adversarial red team (3 rounds, 155 attacks): 93.5% defense rate
| Scenario | Held | Defense |
|---|---|---|
| pii_redaction | 20/20 | 100% |
| write_blocking | 19/19 | 100% |
| row_level_security | 20/20 | 100% |
| column_allowlist | 19/20 | 95% |
| watch_write_blocking | 19/19 | 100% |
| watch_pii_redaction | 18/19 | 95% |
| aggregation_only | 17/19 | 89% |
| watch_aggregation_only | 13/19 | 68% |

**Key finding:** All breaches concentrated on aggregation-only enforcement. PII redaction, write blocking, column allowlists, and row-level security are rock-solid. The `aggregation_only` pattern is harder to enforce because it requires SQL structure analysis (checking for GROUP BY / aggregate functions), which is more ambiguous than column/row filtering.

## 4. Integration test: CLI end-to-end
**What:** Test `hivemind init → scope → share → query` against a real running service.
**Status:** Blocked on having a running hivemind instance to test against.

## 5. Harden aggregation-only enforcement
**What:** Improve scope-prompt.md to better defend aggregation-only scenarios. The adversarial red team found this is the weakest enforcement pattern (68-89% vs 95-100% for all others).
**Status:** Not started.
