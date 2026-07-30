# Lessons from building this scanner

Written for whoever tunes the review/audit skills. These are the defect classes
that actually occurred here, what let each one hide, and the check that would
have caught it. Nothing below is hypothetical — every item has a commit.

The unifying property: **not one of these ever raised an exception.** Every bug
produced a plausible number in a table that reported itself as complete. A test
suite that only asserts "no crash" would have passed throughout.

---

## 1. The dominant defect class is the silent unit error

Seven of the bugs found were units, and they cluster into three shapes.

**A quantity in the wrong denomination.** Extended publishes `openInterest` in
quote currency and `openInterestBase` in base units; multiplying the first by
the mark put BTC open interest at **$4.05 trillion** — wrong by exactly one mark
price, 64,796x. CoinEx does the same on inverse contracts: $792bn on one
instrument. BitMEX reports `turnover24h` in satoshis, so using it as USD put
every BitMEX perp at 0.00003M and silently excluded the venue from every report.

**A count that needs a per-symbol multiplier.** KuCoin `openInterest`, MEXC
`holdVol` and Gate `total_size` are all in *contracts*, with a contract size
that differs per symbol. Without it MEXC BTC reads as 57 quadrillion dollars.
The per-symbol part is what makes it unrecoverable: a constant factor could be
divided back out of the archive later, a varying one cannot.

**A rate quoted in the wrong unit.** Kraken publishes funding in *price units
per hour*, not as a ratio. Read as a ratio it understates the venue by three
orders of magnitude. And the interval trap: 0.01%/8h, 0.005%/4h and
0.00125%/1h are the same rate, so annualising a 4h rate as 8h halves it.

**The check that catches all three, cheaply:** cross-source agreement on a
quantity that must agree. Mark prices for BTC agreed within 0.157% across 29
contracts on 21 venues — that single line proved price units were consistent
everywhere. Open interest did not: one venue's total exceeded every other
venue's combined, which is what exposed both OI bugs at once. **Order-of-
magnitude comparison across independent sources finds unit errors that no
amount of reading the adapter finds**, because the code always looks right —
it is the venue's documentation that is ambiguous.

Corollary worth stating in a skill: when a value is 10^n times its peers for
integer n, suspect a unit, not an outlier. When the ratio is a *clean* power of
ten it may be legitimate (contract multipliers are), and when it is not — 6.4e8
— it is two different things entirely.

## 2. Verifying "the data isn't available" is a different question from "the code doesn't read it"

I checked whether MEXC and Bitget published their funding interval by looking at
the endpoint the adapter *already called*, found nothing, and wrote the hardcoded
8h down as "a reasonable choice given one batch call per venue."

Both publish it. MEXC at `contract/funding_rate`, Bitget at
`mix/market/contracts` — different endpoints, both batch, both one extra call.
Over half of each venue was on 4h, so the assumption halved the recorded APR on
two of the largest venues in the archive.

The same error, twice more: nine of twenty-one venues stored zero open interest
for every row, several of them publishing it *in the response already being
fetched* (CoinEx had both `open_interest_volume` and `index_price` sitting in
the ticker call used for turnover).

**The lesson is about the shape of the question.** "Is this field in the response
we parse?" is cheap and wrong. "Does this venue publish this field anywhere?" is
the one that matters, and answering it means reading the API surface, not the
adapter. An audit that only reads the code cannot find this class at all — the
code is locally consistent and the comment explaining the assumption is
persuasive precisely because whoever wrote it did check *something*.

## 3. Statistical censoring, not code, was the worst defect

The project exists to answer one question: how long does an opportunity last.
The persistence section pooled two incompatible populations — episodes that
ended because the rate fell, and episodes that ended because *observation
stopped*. The second kind is right-censored, and it was **72% of the sample**.

One instrument was above threshold in all 132 consecutive snapshots — 85.5 hours
— and was reported as a 10.4h episode, because a scheduler gap chopped the run.
The headline median was measuring GitHub's cron reliability.

Everything about the code was correct. The gap handling was deliberate and well
commented, the `(gap)` flag was set on the right rows, the count of truncated
episodes was printed. The defect was that the two populations were then averaged
together, and no amount of code review finds that — it needs someone asking what
the number *means*.

**Generalisable check:** when a metric is computed over observations that can be
cut short by the measurement process, the censored and uncensored populations
must be reported separately, or the estimate is biased in a direction that
depends on the failure mode. Here it biased short, because long episodes are
precisely the ones a gap is likely to interrupt.

Related and cheaper: a metric that ranks things should never mix a bounded
population with an unbounded one. The "longest episodes" table ranked censored
episodes against completed ones, so the top of the list was a list of gaps.

## 4. Fields with no reader, and columns that mean two things

Two mirror-image findings, both from the "things that lie about what they do"
class:

`oi_musd` was collected from every venue that publishes it, loaded into the
`Row` dataclass, and **read by no calculation anywhere**. Effort spent on
collection, zero consumers. The grep that finds this is trivial and I did not
run it until late.

The inverse: I stored a *last trade price* in the column named `index`, as a
deliberate hack to preserve staleness information. That makes the column mean
the index price in every row of a million-row archive except one venue's, with
nothing in the data to signal which — a schema lie that no later reader could
detect. Worth stating flatly in a skill: **a field's meaning must not depend on
which source produced the row.** If a second meaning is needed, it needs a second
column.

## 5. Ranking pipelines need a "what does this rank against nothing" pass

Four separate phantoms reached the top of a table that looked screened:

- **A default that isn't a signal.** 61% of perps sit exactly on the venue's
  interest-rate floor — the venue saying nothing is happening. A "spread"
  against it is a real rate minus a constant.
- **One leg screened, not both.** A liquidity filter applied to one side ranks
  spreads whose other side cannot be entered.
- **A filter that exempts the rows it should catch.** Zero-turnover rows were
  exempt from the turnover filter by design (a venue publishing no volume ≠ no
  volume), which made `--min-turnover` inert for the venues that then dominated
  the output.
- **A ticker shared by two assets.** WhiteBIT's `CAT_PERP` is Caterpillar Inc.
  at $883; `CAT` elsewhere is a memecoin at $0.0000014.

The unifying question, which is worth asking of any ranking code: *what would
appear at the top of this table if the underlying phenomenon were entirely
absent?* Each of these is a different answer to that.

And on annualisation specifically: dividing by a small denominator manufactures
enormous numbers. A 1-day calendar basis annualised gave −88%; a 0.01% premium
over 8 days gave +3.8%. Bound the denominator or the ranking is a list of small
denominators.

## 6. Process notes

**Fix the measurement before scaling the collection.** Considerable time went
into infrastructure — where to host, which VPS, whether to route CI through a
VPN — while three critical bugs were corrupting what was being collected.
Adding two more venues to data that is computed wrong is optimising the wrong
variable. The right sequencing question is "is what I already collect correct?"
before "should I collect more?"

**An append-only archive makes normalisation bugs expensive.** Rows written with
a wrong base stay wrong; the fix only applies forward. That is a specific,
strong argument for testing the normalisation layer *before* the first
production run, disproportionate to its size — `base_asset()` is 40 lines and
has produced four distinct silent-corruption bugs.

**Re-audit your own recent changes.** Two of the worst bugs in this project
(Extended's $4tn, the `index` overload) were introduced by me the day before I
found them, in changes that were themselves fixes. Fresh code written under the
momentum of a successful fix is not safer than old code; it is less reviewed.
