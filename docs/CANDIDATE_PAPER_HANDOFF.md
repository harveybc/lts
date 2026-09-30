# Candidate-to-paper preflight (read-only)

This is a local custody and runner-interface check, not model promotion, an order
authorization, or a profitability claim. It opens only the files named in a
candidate handoff. It does not open a broker socket, ledger, or credential file.
Predictor's general Keras persistence interface saves a model file (commonly
`.keras`); that is not by itself LTS's live-linear policy artifact or manifest.

The active Alpaca SPY 1d runner loads only
`prediction_provider.live_linear_manifest.v1` through `SelectedLinearPolicy`.
Its `demo_research_canary` tier requires `research_validated`; the separate
`promoted_paper` tier requires both `live_inference_eligible` and
`live_execution_eligible`. It uses the IEX closed-bar features and an L0 risk
service before L1's bounded native bracket execution. Its profile limits SPY
to one share and four orders per day. The active MT5 runner likewise selects
linear/SAC policies, but needs a Demo bridge mandate, chart magic, volume and
symbol facts, and attested UTC-aligned bars; an unknown command effect cannot
be treated as flat. Neither runner has a modular-model inference adapter.

## Handoff contract

`lts.paper_candidate_handoff.v1` is a JSON object with `model_id`, `family`,
`asset_id`, `timeframe`, and `weights`, `metrics`, `provenance` objects. Each
file object has an absolute `path` and lowercase `sha256`. Metrics must be a
JSON object with a nonempty `validation` object of finite numeric values.
Provenance must be `lts.candidate_provenance.v1`, `status: verified`, and bind
the same model ID plus weights and metrics digests. A linear candidate also
needs `selection_manifest`, the existing live-linear manifest path.

The `verified` provenance status is a supplied claim. This tool verifies its
local bytes and bindings, not the issuer, evaluation design, data lineage,
temporal validity, or independent scientific review. A modular candidate with
complete local evidence returns `locally_hash_consistent` and
`unsupported_model_family`; missing weights or metrics always refuse.

## Exact next smoke (owner-gated)

From this worktree, with a future candidate handoff staged by the model owner:

```bash
cd /home/harveybc/Documents/GitHub/lts-worktrees/candidate-preflight-20260930
/home/harveybc/anaconda3/envs/trading-stack/bin/python -m pytest -q tests/unit/test_candidate_paper_preflight.py tests/unit/test_live_model_selection.py tests/unit/test_alpaca_model_runner.py tests/unit/test_mt5_symbol_model_compat_preflight.py
/home/harveybc/anaconda3/envs/trading-stack/bin/python tools/candidate_paper_preflight.py --candidate /absolute/path/to/candidate.json --route alpaca_spy
```

Exit 2/refused is the expected result for a modular model today. Stop there.
Only if a separately accepted **linear** candidate returns
`interface_compatible_only`, and the owner confirms a Paper account and local
credential file, may the owner run the existing read-only broker probe:

```bash
cd /home/harveybc/Documents/GitHub/lts-worktrees/candidate-preflight-20260930
ALPACA_PAPER_ENV_FILE="$HOME/.config/lts/alpaca-paper.env" bash examples/scripts/run_alpaca_paper_preflight.sh
```

That probe reads Alpaca Paper facts and writes its local lab ledger. It does not
submit/cancel orders. Do not run the model runner, systemd units, or a canary as
part of this smoke. MT5 needs the separate attested symbol/model preflight and
owner-approved Demo evidence before even an interface-compatibility conclusion.

## Owner facts still missing

- Model owner: actual modular weights and metrics files, immutable digests,
  provenance issuer/acceptance receipt, data and evaluation lineage, inference
  feature/normalization contract, action semantics, target asset/timeframe.
- LTS owner: approved model adapter and golden parity evidence for modular
  inference; explicit route and tier decision. No such adapter is wired now.
- Broker owner: current Paper account/credential-file custody and permission to
  run the read-only Alpaca probe. No credentials are needed for the offline CLI.
- MT5 owner, if that route is chosen: effective Demo bridge mandate hash,
  symbol facts, attested CopyRates/bar evidence, chart magic, account binding,
  and unresolved command-effect disposition. None are inferred here.

## Requirement-to-evidence trace

| ID | Requirement | Evidence |
|---|---|---|
| H1 | Missing/tampered weights or metrics refuse | `test_missing_weights_or_metrics_refuses_before_compatibility`, `test_tamper_and_unverified_provenance_refuse` |
| H2 | Provenance binds model and local artifacts | `test_tamper_and_unverified_provenance_refuse`, `_check_file` and provenance check |
| H3 | Route and model interface fail closed | `test_route_and_metric_semantics_refuse`, `test_verified_modular_candidate_is_not_runner_compatible`, `test_linear_fixture_reaches_interface_compatible_without_promotion` |
| H4 | CLI is read-only; no promotion/order claim | `test_cli_is_read_only_and_nonzero_on_refusal`; `promotion_authorized: false` and zero order counters |
