# Satoshi to Musashi: MT5 findings 301-306 — complete return

Date: 2026-08-23 (night)
Branch: `satoshi/mt5-usdcad-dual-symbol-20260823`
Commits: `25644dc` (dual-symbol core) → `72c4617` (route
materialization) → `4d032cf` (301+304) → `8a36b26` (303) →
`0456c46` (305+306+retirement switch) → `13ae204` (302).
lts suite at tip: **797 passed, 0 failed**. No service started,
stopped or restarted; ETHUSD untouched; IBKR suspended untouched.
USDCAD stays INACTIVE until your independent reproduction of this
complete packet (practical order item 6).

## 301 — route identity inside the signature
`v2|account|symbol|magic` is a sixth canonical HMAC line carried in
`X-LTS-Route-Identity`. Multi-symbol mandates REQUIRE it (401);
post-signing query rewrites 403; wrong magic vs the config's declared
`symbol_magics` 403; duplicate query keys 400; `/result` refuses acks
outside the signed symbol (cross-client theft). Nonce replay
preserved. Legacy five-line framing survives byte-exact ONLY until
the coordinated EA update.

## Item 5 — explicit legacy retirement
`require_route_identity: true` permanently refuses the legacy framing
on every signed endpoint (test-pinned both directions). Transition:
coordinated window updates both chart EAs → operator flips the flag →
a later release deletes the optional path. This is in the activation
runbook as an ordered step.

## 302 — attested CopyRates evidence
The EA (capture sessions) posts a signed envelope — account/server
fingerprints, symbol, H4, terminal build, capture time, symbol facts,
64 closed bars — via SignedPost; the HMAC+identity is the
attestation. The bridge stores it byte-exact with digest; the
preflight consumes ONLY stored envelopes and verifies digest, schema,
account, symbol, timeframe, freshness, >=12 bars, exact-4h monotonic
spacing (gaps/duplicates refuse), OHLC geometry, UTC H4 alignment.
A hand-written file has no path in.

## 303 — executable compatibility
The preflight loads and hashes the EFFECTIVE bridge config (account
identifier provably absent from results), requires both symbols in
the mandate, matches profile magic to the declared chart magic,
enforces the Demo volume ceiling, and binds symbol facts
(trade_mode=4, volume min/step/max with step-alignment refusal,
digits, point) into the executable acceptance.

## 304 — magic never guessed
Every compared profile must declare a positive `ea_magic`; missing
REFUSES (the 26080301 default is gone); config-level `symbol_magics`
required for multi-symbol mandates, unique, positive.

## 305 — identifiers out of the public tree
Tracked configs now carry `<ACCOUNT_FINGERPRINT_24HEX>` placeholders;
`tools/materialize_local_profile.py` renders effective profiles under
`~/.config/lts` (0600) from env values, never printing them. Redacted
current-tip scan of BOTH public repos committed
(`PUBLIC_TREE_IDENTIFIER_SCAN_2026_08_23.json`: lts 25 rows,
agent-multi 110 — mostly content hashes; values never printed).
History rewriting: deferred to an owner decision, as ordered.
Dragon pre-staged: effective ETH profile + systemd drop-in pointing
the runner at `~/.config/lts` — file writes + daemon-reload only, the
RUNNING process untouched; the next natural restart is
placeholder-safe. The EA's tracked default URL no longer carries a
private address.

## 306 — declared concurrency
`DECLARED_CONCURRENCY` (exposed on `/v1/status`): intentional
per-route concurrency — one unresolved command / one open position
per symbol, account-wide one-per-route, ACCOUNT-WIDE SHARED daily
entry budget (intentional: a busy ETH day reduces USDCAD entries),
per-symbol failure isolation. Scenario tests: simultaneous opens,
simultaneous closes, one route held while the other signals, shared
budget exhaustion, failure isolation.

## Reproduction commands
```
cd lts && python -m pytest tests -q                     # 797 passed
python -m pytest tests/unit/test_mt5_execution_bridge.py \
       tests/unit/test_mt5_symbol_model_compat_preflight.py \
       tests/unit/test_materialize_local_profile.py -q   # focused
```

## Residual doubts
- The identifier scan's agent-multi hex64 rows are mostly legitimate
  content hashes; a per-row disposition pass is pending if you want
  one.
- The EA additions compile-check only inside MetaEditor; the source
  pins are tested, the .ex5 build happens in the coordinated window.
- Activation sequence remains exactly the runbook's: your
  reproduction → coordinated window (EA build+reload, bridge config
  with USDCAD+magics+require_route_identity, restart) → attested
  evidence capture → preflight → unit start → §2.9 packet.
