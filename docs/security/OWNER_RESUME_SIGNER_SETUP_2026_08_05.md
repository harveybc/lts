# Owner Resume-Signer Setup Packet (findings AUD-F2-20260804-094, AUD-F2-20260811-227)

The `resume_after_reconciliation` operation is DISABLED until this
one-time setup is completed by the owner, by hand. A PTY is not proof of
a human, and neither is typing a public confirmation phrase — those are
ergonomic guards against accidental invocation only, never
authentication. The proof of the owner is a detached OpenSSH Ed25519
signature whose public key is pinned in a **root-owned** file no agent
account can write, and whose private key opens only with the owner's
passphrase — the human-authenticated boundary.

Nothing in this packet is secret. The private key and its passphrase
must never enter Git, Hermes, chat, logs, or any environment file an
agent can read.

## 1. One-time setup (owner, on Omega)

Step 1 — create the owner signing key with a REAL passphrase (the
passphrase prompt is the human boundary; do not leave it empty):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/lts_owner_resume -C "lts-owner-resume"
chmod 600 ~/.ssh/lts_owner_resume
```

Step 2 — pin the PUBLIC key root-owned (requires sudo; this is the
manual/root boundary of the packet):

```bash
sudo mkdir -p /etc/lts
printf 'owner namespaces="lts-ibkr-resume" %s\n' "$(cut -d' ' -f1-2 ~/.ssh/lts_owner_resume.pub)" | sudo tee /etc/lts/resume_allowed_signers
sudo chown root:root /etc/lts/resume_allowed_signers
sudo chmod 644 /etc/lts/resume_allowed_signers
```

Step 3 — verify the pin (as the normal user):

```bash
ls -l /etc/lts/resume_allowed_signers     # must show root root, 0644
```

## 2. Per-resume flow (only when a hold must be cleared)

1. `python tools/mint_resume_capability.py …` (interactive; prints the
   exact sign command).
2. Sign the minted capability file — you will be asked for the key
   passphrase, which no agent knows:

   ```bash
   ssh-keygen -Y sign -f ~/.ssh/lts_owner_resume -n lts-ibkr-resume \
       ~/.config/lts/ibkr-resume-capabilities/resume_XXXXXXXX.json
   ```

3. `python tools/ibkr_resume_after_reconciliation.py --config … \
   --capability ~/.config/lts/ibkr-resume-capabilities/resume_XXXXXXXX.json`
   — name the file you just signed (preferred, finding 227). The CLI
   verifies the signature against the root pin BEFORE anything else;
   then the serialized, fail-closed core (finding 093) runs.

   `--capability` may be omitted: the CLI then classifies every store
   file and uses the single VALID one. Unsigned, malformed, expired or
   already-consumed side files are logged and ignored — they can never
   deny your signed capability (finding 227). Two valid signed current
   capabilities still refuse; keep exactly one, or name one.

## 3. What the verifier enforces (implemented, tested)

- pin file must exist (absent pin ⇒ resume disabled), be root-owned and
  not group/other-writable;
- signature must verify over the EXACT capability bytes, principal
  `owner`, namespace `lts-ibkr-resume`;
- forged payloads, copied signatures on altered payloads, wrong signers
  and wrong namespaces refuse;
- an explicit `--capability` must live inside the protected store and be
  signed, current, profile-bound and unconsumed — anything else is a
  typed refusal (finding 227);
- everything the capability already enforced remains: single-use nonce
  burn, ≤15-minute validity, exact venue/account/instrument/effect
  binding, in-transaction precondition re-reads.

## 3b. Store hygiene (optional, finding 227)

Leftover files never block you and are never deleted automatically. To
tidy the store, run the separate explicit archival operation:

```bash
python tools/ibkr_resume_after_reconciliation.py --config … --archive-invalid
```

It moves ONLY files typed expired or consumed (plus their `.sig`) into
`~/.config/lts/ibkr-resume-capabilities/archive/`, then exits without
resuming. Valid capabilities are never moved; unsigned/malformed files
are also left in place as potential tamper evidence for you to inspect.
The default resume flow moves nothing.

## 4. Rotation / revocation

Replace the line in `/etc/lts/resume_allowed_signers` (sudo) with a new
public key to rotate; an empty or removed file disables resume entirely.
