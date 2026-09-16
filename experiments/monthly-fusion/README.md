# Monthly fusion evaluation

This directory contains the reusable DRACO evaluation harness for comparing
OpenSquilla multi-model fusion lineups. Campaign data and generated reports do
not belong here; write them beneath the repository's ignored `reports/`
directory or another explicit output directory.

The harness has four boundaries:

1. `profiles.py` turns a data-only lineup specification into frozen OpenSquilla
   profiles. It rejects literal credentials and refuses to overwrite a changed
   profile.
2. `generate.py` prepares the complete DRACO task set. It starts model calls
   only when `--execute` is present, records physical provider calls, and never
   retries a terminal task as a new generation.
3. `judge_and_aggregate.py` freezes an independent judge protocol, resumes at
   the criterion/repeat level, and persists every attempt. Only its `judge`
   command makes judge calls. `--max-attempts` includes the first request.
4. `full100/` extracts credential-free records, computes full-set quality and
   cache-aware costs, verifies the result, and renders Markdown.

The scripts require paths on the command line. They contain no server-specific
project path, dated campaign name, or fixed experiment-group list.

## Offline preparation

```bash
python experiments/monthly-fusion/profiles.py \
  --lineups experiments/monthly-fusion/lineups.example.json \
  --output-dir reports/my-campaign/raw-run/profiles \
  --source-revision "$(git rev-parse HEAD)"

python experiments/monthly-fusion/generate.py \
  --run-root reports/my-campaign/raw-run \
  --aef-root /path/to/AutoEval-Factory \
  --opensquilla-root "$PWD" \
  --prepare-only
```

Review `profiles-manifest.json`, `generation-campaign-lock.json`, and the
prepared task count before authorizing generation. Paid generation is explicit:

```bash
python experiments/monthly-fusion/generate.py \
  --run-root reports/my-campaign/raw-run \
  --aef-root /path/to/AutoEval-Factory \
  --opensquilla-root "$PWD" \
  --execute
```

`OPENROUTER_API_KEY` and `BRAVE_API_KEY` are read from the process environment
only for `--execute`. Use a new run root when a frozen profile, dataset, subject
revision, or campaign setting changes.

## Judge protocol and resumable judging

First inspect the offline plan, then freeze the reviewed protocol:

```bash
python experiments/monthly-fusion/judge_and_aggregate.py prepare \
  --run-dir reports/my-campaign/raw-run \
  --aef-root /path/to/AutoEval-Factory \
  --judge-model provider/model

python experiments/monthly-fusion/judge_and_aggregate.py freeze \
  --run-dir reports/my-campaign/raw-run \
  --aef-root /path/to/AutoEval-Factory \
  --judge-model provider/model \
  --judge-model-record reports/my-campaign/judge-model.json \
  --selection-revision monthly-independent-judge-v1
```

Copy the example judge record and replace its ID and reviewed per-token prices
before freezing. The record, source hash, provider, endpoint, reasoning mode,
and native rubric revision become part of the immutable judge selection.

The paid command requires a mode-0600 key file and the same frozen arguments:

```bash
python experiments/monthly-fusion/judge_and_aggregate.py judge \
  --run-dir reports/my-campaign/raw-run \
  --aef-root /path/to/AutoEval-Factory \
  --judge-model provider/model \
  --key-file /secure/path/judge-key \
  --concurrency 16 --requests-per-second 4 --max-attempts 10
```

A criterion/repeat unit receives at most ten total attempts in this example:
one initial request and at most nine retries. Successful checkpoints are never
reissued. HTTP 429 responses share a global cooldown across workers.

## Full-set accounting and report

The accounting phase makes no network or model calls. A generation catalog and
a judge model record with pricing must already be frozen in the campaign.

```bash
python experiments/monthly-fusion/full100/extract_inputs.py \
  --run-dir reports/my-campaign/raw-run \
  --output reports/my-campaign/full100/existing-records.json.gz

python experiments/monthly-fusion/full100/recompute.py \
  --snapshot reports/my-campaign/full100/existing-records.json.gz \
  --comparison reports/my-campaign/raw-run/comparison.json \
  --output-dir reports/my-campaign/full100 \
  --groups baseline,candidate --baseline-group baseline --expected-tasks 100

python experiments/monthly-fusion/full100/verify_results.py \
  --report reports/my-campaign/full100/full100-report.json

python experiments/monthly-fusion/full100/render_report.py \
  --report reports/my-campaign/full100/full100-report.json \
  --profiles-manifest reports/my-campaign/raw-run/profiles/profiles-manifest.json \
  --output reports/my-campaign/REPORT.md
```

For a historical baseline, pass `--baseline-report` to `recompute.py`. The
generated report labels that comparison as cross-run. Full-set AvgQ and AvgPass
use every planned task; an unscored task contributes operational zero. The
report retains the scored-task count so that quality and reliability are not
confused.

Run the offline tests with:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover \
  -s experiments/monthly-fusion/tests -p 'test_*.py'
```
