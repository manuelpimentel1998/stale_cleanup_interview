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

There are 53 weekly scans per disk, from 2025-11-23 through 2026-11-22: 52 seven-day intervals. The merged `scans.csv` contains 106 rows with total/stale GiB and one column per monthly age bin. `files.csv` contains only the final scan for each disk, with integer days since last access rather than access dates. Values below are GiB.

| Disk | Stale-data history | Data aging into stale per interval | Previously stale data removed per interval |
| --- | --- | --- | --- |
| `/projects/atlas` | 10,240 → 30,208 over the year | 896 | 512 |
| `/projects/birch` | 4,096 throughout the year | 448 | 448 |

Across the 52 intervals:

- The synthetic cleanup schedule removes **49,920 GiB** of previously stale data.
- **69,888 GiB** ages into the stale category.
- The fleet stale total therefore **grows by 19,968 GiB**, from 14,336 to 34,304 GiB.

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

## Reference approach A: age-cohort estimate

For an interval:

```text
estimated stale exits GiB = previous stale GiB
                          + estimated GiB aging into stale
                          - current stale GiB
```

Simply subtracting inbound from the current stale total gives an adjusted stock, not the interval's cleanup amount. The previous stock is needed to calculate the change.

Monthly bins use 30-day approximations, covering 0–30, 31–60, …, 511–540 days, plus >540 days. The stale threshold remains >365, not >360. Therefore the exact stale total cannot be obtained by simply summing whole bins: the 361–390-day bin contains both active and stale files.

With a seven-day interval, prior ages 359–365 will cross the threshold if the files remain present and untouched. Those ages span parts of the 331–360 and 361–390 bins; neither bin isolates the incoming cohort.

The reference first uses the independent exact `stale_gib` total to split the crossing bin: subtract the wholly-stale bins to obtain the crossing bin's stale portion, then subtract that portion from the bin total to obtain its active portion. This is an inference from available aggregates, not an extra acquired bucket.

It then estimates overlap uniformly within the relevant active ages. Do not blindly prorate the whole 361–390-day bin: the first synthetic scan has data at active ages 361–365 but none at stale ages 366–390. The fixture is uniform across active age-days, not necessarily across the entire crossing bin.

The remaining uniform-age assumption must be labeled and defended. It agrees with this fixture's ground truth, not necessarily with production distributions. Finer retained age cohorts or new boundary-cohort acquisition reduce uncertainty without retaining every file.

The reference keeps signed residuals. A negative value is not "negative cleanup" to publish blindly: it indicates that the model's assumptions do not explain the observed change. Silently clamping it to zero hides that signal.

Even a positive residual is **not verified team cleanup**. Reaccess, size changes, scope changes, migration, or missing observations can produce similar effects. The supplied main fixture excludes those effects, but a production metric cannot assume that without evidence.

## Reference approach B: file-level comparison

Match stable identities across complete comparable inventories. Distinguish:

- Previously stale files missing from the next scan.
- Previously stale files present but accessed again.
- Existing files newly entering the stale category.
- Files first observed already stale.
- Size changes among files that remain stale.

This supports a more explicit reconciliation than aggregate snapshots. It still does not prove the actor or cause of disappearance, or physical space reclaimed.

**This is a future-acquisition alternative, not a KPI recoverable from the current dataset.** The reference comparison is tested on constructed historical inventories, but the reference CLI must report that history is unavailable for the distributed fixture. Treating unavailable detail as an empty scan would falsely count every old file as removed.

Trade-offs include identity semantics, rename/replacement handling, scan consistency, retention volume, joins, and processing cost. The production FSMON aggregation path uses basenames in its intermediate iteration; those are not sufficient stable identifiers for this comparison. The synthetic fixture deliberately supplies stable IDs.

## Other credible proposals

**Cleanup-action journal:** instrument the team's cleanup workflow to record completed actions, actor, time, file/directory identity, pre-action stale size, and verified outcome. Measure actual action results rather than planned directory sizes. This is the strongest direction for team attribution, but misses out-of-band work unless collection is comprehensive.

**Retained aggregate cohorts:** persist small daily age distributions or near-threshold cohorts over the reporting horizon. Estimate incoming stale volume cheaply and report adjusted progress alongside the raw stale-stock trend. Choose precision, cadence, and retention deliberately.

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
PYTHONDONTWRITEBYTECODE=1 python3 -m interviewer.reference_solution --method aggregate
PYTHONDONTWRITEBYTECODE=1 python3 -m interviewer.reference_solution --method files
```

The aggregate reference should recover 512 GiB per week for atlas and 448 GiB for birch under the main fixture's uniform-cohort assumptions. The file reference should explain that older detail is unavailable and new data collection is needed. Neither reference fills in the candidate's unfinished function.
