# Monkeytype Analyzer — plan & methods

## 1. What is extracted (per test)

| Source | Derived features |
|---|---|
| `_id` across CSVs | dedup key; `files` = set of exports containing the test (file filter) |
| `wpm, rawWpm, acc, consistency` | metrics; `correction_cost = rawWpm − wpm` (speed lost to errors) |
| `charStats` (correct;incorrect;extra;missed) | char counts, `err_rate` = (incorrect+extra+missed)/total |
| `timestamp` | local datetime, date, hour, weekday, month |
| ordering of all tests | `practice` = cumulative test count (exposure), `session` (new session after >30 min gap), `pos` in session, `gap_before`, minutes into session, session size |
| `mode, mode2, language, funbox, punctuation, numbers` | `cond` = text condition (only tests in the same condition are directly comparable) |
| `difficulty` | factor (note: master/expert only saves error-free runs → selection bias) |
| `tags` | order-normalised tag combo + individual tag flags, user-nameable |
| `restartCount, incompleteTestSeconds, afkDuration` | effort / cherry-picking indicators |
| user labels | named groups of tests (from auto-detection or date ranges) |

## 2. Analyses and why they are valid

**Structure of the data:** repeated measures of a single person, tests nested in sessions, strong
learning trend, conditions (tags/difficulty) switched in *time blocks*. Naive group comparisons
are therefore confounded with practice and pseudo-replicated. All inference below accounts for this.

1. **Variance decomposition (ICC(1))** — one-way random-effects ANOVA by session. Shows how much
   variance is day-to-day state/setup (between sessions) vs test-to-test noise. Within-session SD
   is the noise floor: differences smaller than that on a few tests are meaningless.
2. **Learning curve** — `y = a + b·ln(practice)`, the log-linear approximation of the power law
   of practice (Newell & Rosenbloom 1981). Reported as gain per doubling of practice, with
   session-clustered standard errors. LOWESS shown as a model-free check.
3. **Group comparisons (tags, labels, difficulty, …)**
   - Descriptive: n, #sessions, median, mean, Cliff's δ vs reference (robust, non-parametric).
   - Adjusted effect: OLS `y ~ group + ln(practice) + ln(pos in session) [+ condition + difficulty]`
     with **cluster-robust SEs by session** (Liang–Zeger), so correlated tests of one session don't
     count as independent evidence. Falls back to HC3 when there are <5 sessions.
   - **Practice-overlap diagnostic**: if a group's practice range barely overlaps the reference's,
     the adjusted effect rests on extrapolating the trend → flagged.
4. **Factor scan** — every exogenous factor (tag combo, each tag, labels, difficulty, condition,
   hour, weekday, month, session position, gap before test, restarts) is tested the same way:
   ΔR² over the base model, cluster-robust Wald F test, **Benjamini–Hochberg FDR** across factors.
   Metric-derived groupings (clusters, regimes) are excluded here because testing them would be circular.
5. **Automatic group detection** (hypothesis generation, not causal proof)
   - *Regimes*: change-point detection (binary segmentation, mean-shift cost) on **session medians**
     (reduces autocorrelation and outlier influence). Penalty = BIC-style `2·σ²·ln(n)·sensitivity`,
     σ estimated robustly from first differences (MAD). Each change point is annotated with what
     changed around it (tags, difficulty, condition, labels, breaks of several days) → likely cause.
   - *Clusters*: Gaussian mixture on standardised (wpm, acc, consistency, correction cost,
     ln duration), k chosen by BIC (k=1 allowed = "no subpopulations"). Each cluster is characterised
     by metadata enrichment (lift, one-sided Fisher exact test, BH-FDR).
   - *Anomalous sessions*: session-mean residual after controlling for everything known
     (practice, position, condition, difficulty, tags); robust z (median/MAD) > 2.5 → something
     unrecorded happened (fatigue, different keyboard, …).
   - Any detected group can be saved as a **named label** and then becomes a factor in 3 and 4.
6. **Session dynamics** — warm-up/fatigue via within-session deviations from the session median
   (removes between-day differences), mean ± 95% CI by position, plus gap-before-test effect.
7. **Speed/accuracy trade-off** — correlation between-sessions vs within-session (Simpson's paradox check).

## 3. Caveats surfaced in the app
- Observational data: adjusted effects are associations; unmeasured time-varying factors can remain.
- Cluster-robust SEs are anti-conservative with few clusters (<~30 sessions) → shown.
- Mixing conditions (e.g. custom texts with words 10) dominates everything → filter to one condition.
- 10-word tests are noisy (few seconds long); aggregate before judging.
- Restarting until a good run (cherry-picking) inflates saved results; see restart factor.
