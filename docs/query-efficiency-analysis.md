# Catalogue query efficiency analysis

This is a read-only planning note. It does not change query cadence, merge
queries, increase the result window, contact Vinted, or claim that any request
rate is safe. The calculations can be reproduced with `query_efficiency.py`.

## Implemented evidence and discovery pipeline

The scraper now records one `catalogue_query_executions` row for every physical
catalogue request. Returned IDs and their per-execution classifications are in
`catalogue_query_execution_items`; durable per-query progress and observations
are in `query_progress` and `query_item_observations`; work awaiting local
filtering/outbox persistence is in `pending_query_items`.

The ongoing new-listing rule is ID/progress based:

- a genuinely uninitialised query uses the historical 20-minute check once as
  a bootstrap flood guard;
- after a successful observation, unseen IDs ahead of the previous newest-first
  anchor are candidates regardless of listing age;
- when the previous anchor has fallen outside the finite response window, an
  unseen listing must be dated at or after the previous successful request;
- failed requests never move the anchor or successful-observation boundary;
- progress and privacy-minimised pending snapshots commit together, so a lost
  process or queue handoff can resume without losing or duplicating an alert;
- editing the query URL resets its progress atomically, while renaming it or
  changing polling/deal preferences does not.

Pending claims are scoped per logical query. This lets an active overlapping
query proceed when another query is paused. Final `items.item` uniqueness keeps
the established global-delivery behaviour: whichever active query persists a
listing first generates the alert, and later overlapping batches classify that
ID as already known instead of sending a duplicate query-specific alert.

Telemetry stores keyed item/query fingerprints for analysis rather than seller
details, descriptions, chat IDs, or raw search URLs. A short sanitised pending
snapshot retains only the listing fields already required by filtering and
delivery. No instrumentation path sends an additional Vinted request.

### Inspecting the evidence

Open **Efficiency** in the Web UI, or visit `/query-efficiency`, then select a
7, 30, or 90-day period. The page is read-only and labels scheduled clean-run
models separately from observed execution data. For direct SQLite inspection,
use the tables listed above; do not treat hashed item keys as listing content.
Execution detail is retained for 90 days and pruned at startup. Query progress
and observations remain because they are correctness state, not disposable
reporting detail.

## Current clean-run estimate

The captured scheduler plan has 36 active catalogue queries: 2 Fast and 34
Normal. The configured base intervals are 90 seconds and 180 seconds, but the
shared request budget stretches the 34 Normal queries to an effective 1,093
seconds while the 2 Fast queries remain at 90 seconds. The result window is 20
items per catalogue request.

| Measure | Fast | Normal | Combined |
| --- | ---: | ---: | ---: |
| Active queries | 2 | 34 | 36 |
| Effective interval | 90s | 1,093s | — |
| Scheduled requests/minute | 1.3333 | 1.8664 | **3.1998** |
| Scheduled requests/hour | 80.0000 | 111.9854 | **191.9854** |
| Scheduled requests/day | 1,920.0000 | 2,687.6487 | **4,607.6487** |

These are nominal clean-run scheduler figures. Quiet hours, query-specific
quiet-hour monitoring, cooldowns, failures, process downtime, retries, session
refreshes, and any non-catalogue calls make observed traffic different.

Most importantly, 3.1998 requests/minute is **not a safe threshold**. The
incident history includes a block after traffic rose from roughly 2.0 to 15.8
requests/minute, followed by re-blocks after only 51 and 85 requests at roughly
3.2 requests/minute. That evidence shows path dependence and uncertainty; it
does not identify a magic request rate.

For an arrival uniformly distributed between successful clean-run polls, the
modelled scheduler-only detection latency is:

| Mode | Mean | 95th percentile | Maximum before next due poll |
| --- | ---: | ---: | ---: |
| Fast | 45s | 85.5s | 90s |
| Normal | 546.5s (9m 6.5s) | 1,038.35s (17m 18.35s) | 1,093s (18m 13s) |

This latency model excludes request duration, queueing, delivery, failures,
cooldowns, quiet hours, and finite-window loss. A short-lived listing is only
catchable if at least one successful catalogue response contains its ID before
the listing disappears.

## Result-window model and right-censoring

With newest-first results limited to `N`, a query can observe at most `N` IDs
from one execution. If more than `N` matching listings enter between successful
observations, some can enter and leave that visible window without ever being
seen. Durable item-ID progress fixes the old listing-age correctness problem;
it cannot recover an ID that the finite server response never returned.

The arrival rate that would arithmetically fill a result window between clean
polls is shown below. This is capacity arithmetic, not a probabilistic miss
forecast; real listings arrive in bursts.

| Window | Fast at 90s | Normal at 1,093s |
| ---: | ---: | ---: |
| 20 | 13.333 matching items/min | 1.098 matching items/min |
| 40 | 26.667 matching items/min | 2.196 matching items/min |
| 60 | 40.000 matching items/min | 3.294 matching items/min |

A response returning fewer than the requested window is uncensored for that
response. A response returning exactly the requested limit is right-censored:
it proves only that at least that many rows were available. It does not prove
the catalogue had exactly that many, nor reveal how many relevant IDs existed
beyond the limit.

Consequences for analysis:

- treat every full-window execution as right-censored;
- report observed item totals as lower bounds when any execution is censored;
- use successive returned-ID sets to measure what was observed, never to claim
  an exact unseen-tail count;
- stratify acceptance and notification yield by censoring status, because a
  saturated newest-first window may not represent the full matching catalogue;
- include the elapsed time since the previous successful observation. Failed
  requests, blocks, quiet hours, and pauses lengthen the exposure window;
- do not infer that `fresh/new = 0` means the query had no unseen matching
  listings outside the returned window.

Changing 20 to 40 or 60 does not alter the nominal requests/minute if the API
still returns the window in one request. It does increase response and local
processing volume, and its server-side risk is unknown. Therefore the live
default should remain 20 until clean telemetry supports an isolated,
reversible test. The model can compare candidate sizes without sending traffic:

```python
from query_efficiency import result_window_scenario

result_window_scenario(
    items_per_request=40,
    matching_arrivals_per_minute=1.5,
    poll_interval_seconds=1093,
)
```

## Overlap and exact-merge constraints

Returned-ID Jaccard similarity and directional containment can rank pairs for
manual review. High overlap is evidence of redundant returned work, but it is
not proof that two Vinted searches can be merged safely.

A row in `queries` is a logical user search. Today each scheduled execution is
also one physical catalogue request. A future consolidation would have to map
one physical response back to every affected logical query without changing
subscriber routing, filtering, priority, quiet-hours, or deal-evaluator
semantics. Overlap alone is not that mapping proof.

An exact merge is permissible only when one catalogue query can express the
exact union of both result sets while preserving all relevant semantics:

1. The market/domain, currency, ordering, catalogue/category, size, brand,
   condition, price, colour, and search-text semantics must be compatible.
2. Combining list filters must not create an unintended cross-product. For
   example, `(brand A, size X) OR (brand B, size Y)` is not equivalent to
   `brand in {A,B} AND size in {X,Y}`, which also admits A/Y and B/X.
3. Distinct text searches need a server-supported exact OR. Broadening or
   dropping search text is not an exact merge.
4. Query-specific banwords, allowlists, poll mode, quiet-hour override, price
   ceiling/AI evaluation, subscribers, and notification attribution must still
   behave identically.
5. The merged query must not create result-window competition that pushes a
   unique result from either source query out of the newest-first window.
6. A comparison needs aligned successful executions over enough clean days;
   timestamps separated by cooldowns or failures should not be treated as the
   same snapshot.

Because condition 5 can fail even when URL filters form an exact logical union,
no automatic merge is recommended. Use `returned_id_overlap()` to shortlist
pairs, then inspect URL semantics and simulate the union against stored
observations. Do not send extra catalogue requests for this analysis.

## Read-only recommendation

Keep the current scheduler, shared request gate, Fast/Normal allocation, result
window of 20, breaker, and quiet-hours behavior unchanged while the new
execution telemetry accumulates. A useful evidence window is at least 7–14
clean operating days and should include, rather than discard, blocked and
cooldown periods.

For each query, review:

- successful and failed execution counts and elapsed successful-observation
  gaps;
- full-window/right-censored fraction;
- returned, first-observed, already-known, locally rejected, accepted, and
  notification counts per request;
- request duration and notification yield per 100 catalogue requests;
- aligned returned-ID overlap and directional containment with other queries;
- whether Fast priority materially improves delivered-alert latency for the
  small number of genuinely time-sensitive searches.

Then consider changes in this conservative order:

1. Ask the user before pausing a demonstrably obsolete, persistently zero-yield
   query.
2. Review high-overlap pairs, but merge only when every exactness constraint
   above passes and stored-window simulation shows no unique-tail loss.
3. For a repeatedly censored high-value query, consider a single isolated,
   reversible result-window change; do not raise the global default first.
4. Reallocate cadence only after comparing delivered value against catalogue
   traffic and block/cooldown history. Preserve hard shared pacing and Normal
   starvation protection.

No major consolidation or scheduler change is justified before that evidence
exists. Reliable notification delivery and low block risk take priority over
maximizing scrape frequency.
