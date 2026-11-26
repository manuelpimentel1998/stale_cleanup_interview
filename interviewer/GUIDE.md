# Interviewer guide — do not share with the candidate

## What this exercise evaluates

The three explicit objectives are:

1. Explain why the current stale-data total does not reveal the team's cleanup effort.
2. Propose different KPIs, their acquisition/retention requirements, and assumptions, taking **thousands of disks with millions of files each** into account.
3. Implement one chosen KPI and demonstrate its result and a verification check.

The important distinction is between a **stock** (how much stale data exists now) and **activity over an interval** (how much left that stock, and why). The candidate should ask useful questions, navigate the starter, and choose a proportionate approach.

Do not require the aggregate reference algorithm. File-level comparison, a defensible estimate, or recording cleanup actions can all be good approaches. Historical file comparison is **not possible with the current retained data**; proposing new acquisition is allowed. The candidate may use clearly labeled assumed inputs for a prototype, but must not present them as measured facts.

Coding is secondary. AI-generated code is acceptable; unsupported claims or an inability to explain the calculation are not.

## Run the interview

1. Give the candidate only the `candidate/` folder and its README.
2. Let them run the existing report and choose where to look.
3. Answer questions consistently using the facts below. Do not lead with the cleanup formula.
4. Ask them to explain at least two KPI options and choose one. Within the README's 20-minute time box, leave approximately five minutes to implement the smallest useful slice.
5. Ask: **"What would you tell leadership, and how confident are you in that statement?"**

For the demonstration, the candidate can run `python3 -m storage_metrics.plotting --output-dir graphs` from their folder. It saves stale history and any implemented KPI results; the neutral starter produces only the stale graph. Different metric names or units are plotted separately, and missing results are not treated as zero.

If they stall, one neutral prompt is: **"What could make the over-one-year total change between scans?"** Record that a prompt was needed. Avoid rescuing them by naming the preferred algorithm.

Do not use the project test suite as a candidate autograder. Some tests establish that the distributed starter is intentionally unfinished, and other tests concern the private reference implementations.

## Dataset ground truth

There are 53 weekly scans per disk, from 2025-11-23 through 2026-11-22: 52 seven-day intervals. The merged `scans.csv` contains 106 rows with total/stale GiB and one column per age bin. `files.csv` contains only the final scan for each disk, with integer days since last access rather than access dates. Values below are GiB.

| Disk | Stale-data history | Data aging into stale per interval | Previously stale data removed per interval |
| --- | --- | --- | --- |
| `/projects/atlas` | 10,368 → 30,336 over the year | 896 | 512 |
| `/projects/birch` | 4,160 throughout the year | 448 | 448 |

Across the 52 intervals:

- The synthetic cleanup schedule removes **49,920 GiB** of previously stale data.
- **69,888 GiB** ages into the stale category.
- The fleet stale total therefore **grows by 19,968 GiB**, from 14,528 to 34,496 GiB.

These are facts of the private generator and its event schedule. They are **not evidence available from an actual production snapshot about who performed a deletion**. The candidate data contains no actor, reason, or cleanup-event labels.

Atlas total usage also grows (57,088 → 77,056 GiB). Birch total usage stays at 27,520 GiB. Both disks receive new active data between scans. Looking only at total disk usage does not solve the problem either.

The private generator derives the historical aggregates from transient synthetic inventories and writes only the latest file details. See `ground_truth.json` for interval records. It does not make historical inventories available to the candidate. The checks verify these relationships rather than trusting hand-entered totals.

## Consistent answers to candidate questions

Offer these when relevant; the exercise is not a guessing game.

| Question | Answer |
| --- | --- |
| What does leadership want? | Visibility into cleanup activity despite incoming stale data. Ultimately they want creditable team activity, not merely a change in the stale total. A clearly labeled estimate is a useful first step. |
| Are scans complete and comparable? | Yes, in the supplied main dataset. The same disks and coverage are used throughout. In production, partial or failed scans must be handled explicitly. |
| Can the last-access ages be trusted? | Treat `days_since_last_access` as accurate at the scan date in this synthetic dataset. In production, collection semantics and access-time updates need confirmation. |
| Are files reaccessed, resized, or moved in this dataset? | Not in the main fixture. Production can have all of those effects; the candidate should explain how they affect the chosen metric. |
| Are detailed file records available? | Only the latest scan per disk, refreshed weekly. Historical file detail is not retained. A file-level historical KPI needs new acquisition/retention or an action journal. |
| What is the production scale? | Thousands of disks, each with millions of files: potentially billions of entries per fleet scan. Persisting and joining full inventories repeatedly is a material storage, processing, and acquisition decision. |
| Is a missing file proof that the team deleted it? | No. Missing files in comparable complete scans establish disappearance from the observed inventory. Attribution needs cleanup-action evidence. |
| Does logical file size equal physically reclaimed disk space? | Not necessarily. Snapshots, deduplication, links, compression, and storage accounting can matter. This dataset uses logical file sizes in GiB. |
| How much history exists? | One year of aggregated weekly disk scans. Only the latest weekly file-detail scan is retained; older file-detail requests explicitly fail. |

Do not reject an approach because it differs from the reference. Challenge its evidence and operational costs instead.

## Reference approach A: 28-day age-cohort estimate (`solutions/cohort.py`)

```text
estimated cleanup = previous stale + previous 330-365 bucket - current stale
```

The month's aging inflow is assumed to be the whole previous 11-12 month bucket
(ages 330-364, the `330-365` edge bin), as if that whole cohort reached a year
during the month. A month is assumed to be exactly 28 days, so weekly scans give
non-overlapping four-scan pairs. Verify with
`python3 -m interviewer.solutions.cohort --verify`; add `--plot DIR` to save the
KPI graph through the candidate plotting helper.

- The previous stock is required. `current stale - inflow` is only the adjusted
  stock left after subtracting inflow, not the interval's cleanup.
- Bins use edges `0, 30, 60, …, 330, 365`: bounded bins are `[lower, upper)`,
  then `365+`. Stale means age 365 or more, so the `365+` bin is exactly the
  stale total and there is no crossing bin to split.
- For a 28-day step only ages 337-364 cross: ages 330-336 stay active, and age
  365 is already stale at the previous scan, so it is not inflow. The whole-bin
  assumption therefore overstates inflow by about 7 age-days. In this uniform
  fixture the estimate is 43.8% high for atlas (2,944 vs 2,048 GiB/month) and
  25.0% high for birch. State the bias; do not clamp or hide it.
- Cheap: aggregate bins only, no new acquisition. Assumes a stable age density
  within the bucket and no reaccess, size change or migration.
- Signed residuals are kept. A negative value means the assumption does not
  explain the observed change; it is not negative cleanup. Even a positive
  value is **not verified team cleanup**.
- `calculate_kpi` implements the candidate hook. Consecutive weekly calls use
  the scan exactly 28 days before `current` as the baseline, so the metric plots
  directly; weeks before a month of history exists are gaps.

## Reference approach B: rolling stale-inventory diff (`solutions/inventory_diff.py`)

Keep the previous month's stale IDs and diff today's inventory against them:

```text
disappeared = previously stale IDs absent today     -> cleanup proxy
reaccessed  = present, now age < 365                -> left stale stock
retained    = stale in both; size delta tracked     -> stays stale
first seen  = stale today with no old stale ID      -> aging-in or new
```

Run `--demo` for a checked four-record example (conservation residual zero) and
the default mode for the acquisition gap. Add `--plot DIR` to call the KPI hook
through the candidate plotting helper; on the supplied fixture that saves only
the stale graph because historical detail is missing.

- `disappeared` is an upper bound on cleanup: migration, lost mounts, identity
  churn and rename-with-new-ID are indistinguishable. Attribution needs action
  evidence, and logical GiB need not equal physical reclaimed space.
- Keeping only stale IDs mixes aging-in with genuinely new files. Retain a
  near-threshold active set or all IDs to split them.
- Historical detail is not retained in the fixture, so `calculate_kpi` returns
  None and the CLI reports the acquisition gap rather than pretending an empty
  scan is a full one. Production also needs stable identities, consistent scan
  scope and a defensible rename/replacement policy.
- Retention scale, computed once in the module docstring: a record with full
  path (~100 B), size (8 B) and age (4 B) is about 112 B. One disk with a
  billion files and 25% stale is 250M records, about 28 GB per retained month.
  Three thousand such disks is about 84 TB per month, or roughly 1 PB for a
  year of monthly copies. Compression or sampling changes the constant, not the
  order of magnitude.

## Other credible proposals

**Cleanup-action journal:** instrument the team's cleanup workflow to record completed actions, actor, time, file/directory identity, pre-action stale size, and verified outcome. Measure actual action results rather than planned directory sizes. This is the strongest direction for team attribution, but misses out-of-band work unless collection is comprehensive.

**Retained aggregate cohorts:** persist small daily age distributions or near-threshold cohorts over the reporting horizon. Estimate incoming stale volume cheaply and report adjusted progress alongside the raw stale-stock trend. Choose precision, cadence, and retention deliberately.

**Near-threshold aggregate cohort:** retain daily sizes only for ages ~300-395. Inflow is then measured directly instead of assumed, with no file IDs and no uniform-age guess. Cost is roughly a hundred daily values per disk per month, kilobytes rather than the GiB-scale inventories in approach B. This is often the best accuracy-per-byte compromise.

**Sampled inventory diff:** retain a deterministic hash sample (for example 1%) of stale IDs, diff the sample, then scale by 1/p. Retention drops about 100x and statistical confidence can be stated. Good when approximate cleanup trend matters more than an exact total.

**Membership sketch:** store a Bloom or XOR filter of last month's stale IDs (about 1.2 B/file at 1% false positives) and test today's IDs for absence. Cheap disappearance counts; pair with aggregate size classes to turn counts into GiB. False positives cause a small undercount, never false "disappeared".

**Filesystem event / change feed:** use fanotify/inotify, an fsmon journal, filesystem generation numbers, or a deletion audit trail to record removals as events. Exact timing and possible actor attribution, but only if collection is comprehensive; otherwise out-of-band work is invisible. Metadata-only, so logical size still needs accounting.

**Snapshot metadata diff:** if disks or backups already have monthly snapshots, diff their metadata (`zfs diff`, btrfs subvolume comparison, backup catalog). No new per-file inventory, but snapshot retention, metadata scan cost and snapshot-consistency questions remain.

**First-observed-stale markers:** persist a `stale_since` marker or side row the first month a file is seen stale. Later absence is cleanup, and marker presence separates aging-in from genuinely new files. One small row per stale file and no full path history; still per-ID and needs identity stability.

**Survival / decay model:** estimate per-cohort retention rates from the aggregate age distribution and report expected exits with uncertainty. No new acquisition, but assumption-heavy and unsuitable for attribution.

**Physical reclaim cross-check:** compare filesystem free-space and block-allocation counters. This measures actual reclaimed space but is polluted by concurrent writes, snapshots, deduplication and compression; use it as corroboration, not as the cleanup KPI.

**Hybrid:** use an aggregate fleet-level estimate, action records for credited team cleanup, and selective file-level reconciliation for investigations. Keep those measures separately named instead of pretending they are interchangeable.

For any approach, useful leadership reporting separates stale inventory, estimated incoming stale data, measured/estimated exits, and attributable cleanup where evidence exists.

## Evaluation rubric

Score each thinking dimension 0–3: 0 = absent or misleading, 1 = partial, 2 = sound, 3 = strong and well-supported.

| Dimension | Strong evidence |
| --- | --- |
| Metric reasoning | Explains why the existing total hides effort; proposes multiple KPIs and distinguishes what each would establish. Uses consistent units and threshold semantics. |
| Questions and assumptions | Recognizes latest-only file detail; states any assumed inputs; asks about reaccess, coverage, identity, or attribution because the answer changes the approach. |
| Codebase navigation and scope | Finds the data model, fetching layer, report, and plotting helper; implements a small KPI prototype. Acquisition and retention proposals explicitly account for thousands of disks and millions of files each. |
| Verification and communication | Demonstrates the KPI and a check; labels estimates honestly; does not claim unavailable history or team attribution from snapshots alone. |

Record coding separately as **working / partially working / proposal only**. Prefer strong reasoning with a small imperfect prototype over polished code implementing an unjustified metric.

Warning signs include treating net stale decrease as all cleanup, treating every stale exit as deletion, claiming team attribution without event evidence, summing unrelated disks or intervals, and accepting AI output without checking its meaning.

## Material checks and reference runs

From the project root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m interviewer.generate_dataset --check
PYTHONDONTWRITEBYTECODE=1 python3 -m interviewer.solutions.cohort --verify
PYTHONDONTWRITEBYTECODE=1 python3 -m interviewer.solutions.inventory_diff --demo
```

The 28-day cohort reference estimates 2,944 GiB/month for atlas and 2,240 GiB for birch, which is 43.8% and 25.0% above the private synthetic schedule (2,048 and 1,792). That gap is the stated bucket-boundary bias, not a bug: the whole 330-365 bin is treated as inflow while ages 330-336 stay active and age 365 is already stale at the previous scan. The inventory reference explains that older detail is unavailable and new data collection is needed. Neither solution fills in the candidate's unfinished function; both implement the same `calculate_kpi` signature so the report and plotting helpers can call them.
