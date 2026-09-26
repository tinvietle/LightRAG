# Duplicate Entities: Evidence From the Data and From Retrieval

**What this document is:** direct evidence that the knowledge graph contains
duplicate/near-duplicate entities, and that this duplication actually shows up
in what the retrieval pipeline returns for real queries — not a design doc for
the fix. Everything below comes from the current, production-relevant graph
(`data_rag_workstation`, 45,340 entities) and the real saved evaluation run
(`baseline_contexts.json`, 416 clinical case queries, hybrid retrieval mode,
`top_k=40` / `chunk_top_k=10`). No data was modified to produce this analysis.

---

## 1. Duplication in the underlying data

LightRAG stores an entity by its exact extracted name string. Two documents
written by different authors that describe the same finding with different
capitalization or wording currently produce **two separate graph nodes** with
no link between them.

Scanning all 45,340 entities for exact-CUI matches (QuickUMLS) and near-identical
embeddings (SapBERT, cosine similarity ≥ 0.99, screened against modifier and
type conflicts):

| | |
|---|---|
| Total entities | 45,340 |
| Entities identified as duplicates of another entity already in the graph | **3,704 (8.17%)** |
| Distinct duplicate clusters found | 2,981 |

This is not a marginal artifact — 1 in every ~12 entities in the graph is a
duplicate of something already there.

**Example, pulled directly from the graph:**

```
"Pyogenic liver abscess"   (Title Case, from one document)
"pyogenic liver abscess"   (lowercase, from a different document)
```

Same diagnosis, same wording, different capitalization — two nodes.

---

## 2. Duplication as it actually appears in retrieval

This is the more important part: duplication isn't just a graph-structure
statistic, it's visibly wasting ranking capacity on real, live queries.

For every one of the 416 saved evaluation queries, we looked at the actual
ranked list of entities the retrieval pipeline returned for that query
(`retrieval.entities`, the true candidate set the query pipeline generates) and
checked whether the same underlying entity appears more than once in that list.

### 2.1 How common is it

| Metric | Value |
|---|---|
| Queries where the returned entity list contains at least one duplicate | **339 / 416 = 81.5%** |
| Total ranking slots wasted on duplicates, across all queries | 795 |
| Average wasted slots per affected query | 2.3 |

**Over 4 out of 5 queries retrieve at least one duplicate entity right now.**

### 2.1a Full distribution, not just the average

Averages hide how bad the worst cases get. Here is the full distribution of
wasted slots per query, both as a raw count and as a percentage of that
query's own returned entity list (list sizes vary, typically 20–90 entities):

| Statistic | Wasted slots (count), all 416 queries | Wasted slots (count), the 339 affected queries only | Wasted slots as % of that query's list, all 416 | Wasted slots as % of that query's list, affected only |
|---|---|---|---|---|
| Min | 0 | 1 | 0.0% | 1.3% |
| Mean | 1.91 | 2.35 | 4.0% | 4.9% |
| Median (p50) | 2 | 2 | 3.2% | 3.9% |
| p75 | 3 | 3 | 5.7% | 6.4% |
| p90 | 4 | 5 | 8.2% | 9.1% |
| p95 | 5 | 5 | 10.5% | 10.9% |
| p99 | 7 | 7 | 15.2% | 15.9% |
| Max | 9 | 9 | 21.7% | 21.7% |

Histogram (all 416 queries, including the 77 with zero duplicates):

```
0 wasted slots:  77 queries  ███████████████
1 wasted slot:  115 queries  ███████████████████████
2 wasted slots: 111 queries  ██████████████████████
3 wasted slots:  53 queries  ███████████
4 wasted slots:  26 queries  █████
5 wasted slots:  19 queries  ████
6 wasted slots:   8 queries  ██
7 wasted slots:   5 queries  █
8 wasted slots:   1 query
9 wasted slots:   1 query
```

Reading this together: the typical query loses 2 ranking slots to duplicates
(the median), but the tail is real — 1 in 20 queries (p95) loses 5, and the
single worst query loses 9 out of its 60 returned entities (15% of that
query's entire candidate list) to redundant copies of concepts already
present.

**Worst cases by raw count:**

| Query | Outcome | Wasted / list size |
|---|---|---|
| tn=160 | disease not retrieved (failure) | 9 / 60 |
| tn=385 | disease not retrieved (failure) | 8 / 77 |
| tn=5, 37, 150, 165 | succeeded | 7 / 44–64 |
| tn=233 | disease not retrieved (failure) | 7 / 58 |

**Worst cases by share of the list wasted:**

| Query | Outcome | Wasted / list size |
|---|---|---|
| tn=255 | succeeded | 5 / 23 = **21.7%** |
| tn=264 | succeeded | 5 / 24 = **20.8%** |
| tn=324 | succeeded | 5 / 25 = **20.0%** |
| tn=179 | wrong chunk selected (failure) | 5 / 33 = 15.2% |
| tn=160 | disease not retrieved (failure) | 9 / 60 = 15.0% |

Interesting pattern: the queries with the *highest percentage* of their list
wasted tend to have *smaller* total candidate lists (23–33 entities) — when a
query only surfaces a narrow set of candidates to begin with, every duplicate
inside it costs proportionally more.

### 2.2 It happens whether the query succeeds or fails today

| Outcome bucket | # queries | % with duplicate entities in their own results |
|---|---|---|
| Retrieval succeeds | 193 | 85.0% |
| Retrieval fails — correct disease never retrieved | 172 | 80.2% |
| Retrieval fails — correct entity found, wrong chunk chosen | 46 | 73.9% |
| Retrieval fails — disease not in graph at all | 5 | 60.0% |

Duplication is present at essentially the same rate everywhere. It isn't a
niche problem hiding in the failures — it's baked into how the pipeline
retrieves for almost every query, successful or not.

### 2.3 What kind of duplication is it: mostly the simplest kind

We split every duplicate group into two types:

- **Pure capitalization/whitespace duplicates** — literally the same string,
  different case (e.g. `"Penicillin"` / `"penicillin"`).
- **True synonym duplicates** — different wording for the same concept (e.g.
  `"liver abscess"` / `"hepatic abscess"`).

| Type | Duplicate groups found | Wasted slots |
|---|---|---|
| Pure case/whitespace | 547 | 673 |
| True cross-lexical synonym | 157 | 122 |
| **Total** | **704** | **795** |

**~85% of all wasted ranking slots (673 of 795) come from the simplest possible
form of duplication — the same exact entity name in different letter case.**
The remaining ~15% require recognizing genuine medical synonyms.

### 2.4 Concrete examples pulled directly from real query results

Pure case duplicates (same entity, retrieved twice):

```
tn=1  -> ['Penicillin', 'penicillin']
tn=3  -> ['Painless palpable mass', 'painless palpable mass']
tn=3  -> ['Follicular Lymphoma', 'follicular lymphoma']
tn=4  -> ['Subdural hematoma', 'subdural hematoma']
tn=4  -> ['epidural abscess', 'Epidural abscess']
```

True synonym duplicates (different wording, same concept, retrieved as if they
were unrelated entities):

```
tn=1  -> ['cutaneous abscess', 'Skin abscess']
tn=6  -> ['end-stage renal disease', 'End-stage renal disease', 'end-stage renal failure', 'chronic kidney failure']
tn=8  -> ['CT scan of abdomen and pelvis', 'CT scan of the abdomen and pelvis']
tn=13 -> ['Oral amoxicillin-clavulanate', 'Oral amoxicillin/clavulanate']
tn=17 -> ['acute kidney injury', 'acute kidney failure']
```

### 2.5 A single query where this compounds

Test query #10 — ground truth diagnosis "Pyogenic liver abscess," a disease
with only **2** supporting documents in the entire 1,674-document training
corpus (i.e. very little redundancy to begin with). Its own retrieved entity
list contains:

```
['liver abscess', 'hepatic abscess', 'Hepatic abscess', 'Liver abscess']   -> 4 slots, 1 concept
['pyogenic liver abscess', 'Pyogenic liver abscess']                       -> 2 slots, 1 concept
```

**6 of this query's ~48 candidate slots are spent on 2 concepts that should
each occupy exactly 1 slot.** For a rare diagnosis, that is a meaningful share
of the entity list. This query's correct entity was in fact found (rank 10, and
separately rank 42) but its supporting text did not make the final selected
context — this duplication is visible in the exact same query.

---

## 3. Summary

- The graph itself contains duplicate entities: **8.17%** of all 45,340 nodes.
- That duplication is not just theoretical — it shows up in **81.5% of real
  queries'** retrieved results, wasting an average of 2.3 ranking slots per
  affected query.
- It is present at roughly the same rate in both successful and failing
  queries, so it should be understood as a **pervasive ranking-noise problem
  across the whole system**, not a defect isolated to today's failures.
- The large majority of the waste (**85%**) is caused by nothing more
  sophisticated than inconsistent capitalization between documents — the
  simplest possible category of duplicate.
