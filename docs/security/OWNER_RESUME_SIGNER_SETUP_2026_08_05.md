# Owner Resume-Signer Setup Packet (finding AUD-F2-20260804-094)

The `resume_after_reconciliation` operation is DISABLED until this
one-time setup is completed by the owner, by hand. A PTY is not proof of
a human; from now on the proof is a detached OpenSSH Ed25519 signature
whose public key is pinned in a **root-owned** file no agent account can
write, and whose private key opens only with the owner's passphrase —
a separate human-authenticated boundary.

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

3. `python tools/ibkr_resume_after_reconciliation.py --config …` —
   the CLI verifies the signature against the root pin BEFORE anything
   else; then the serialized, fail-closed core (finding 093) runs.

## 3. What the verifier enforces (implemented, tested)

- pin file must exist (absent pin ⇒ resume disabled), be root-owned and
  not group/other-writable;
- signature must verify over the EXACT capability bytes, principal
  `owner`, namespace `lts-ibkr-resume`;
- forged payloads, copied signatures on altered payloads, wrong signers
  and wrong namespaces refuse;
- everything the capability already enforced remains: single-use nonce
  burn, ≤15-minute validity, exact venue/account/instrument/effect
  binding, in-transaction precondition re-reads.

## 4. Rotation / revocation

Replace the line in `/etc/lts/resume_allowed_signers` (sudo) with a new
public key to rotate; an empty or removed file disables resume entirely.
