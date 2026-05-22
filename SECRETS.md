# Secrets handling (`.env.shared` via git-crypt)

Team-shared secrets live in `.env.shared`, tracked in the repo and encrypted at rest with [git-crypt](https://github.com/AGWA/git-crypt). Trusted teammates decrypt transparently on checkout; everyone else (and GitHub) only ever sees ciphertext.

**Stop sharing `.env` over chat.** The flow below replaces it.

## How the files relate

| File | Tracked? | Encrypted? | Purpose |
|---|---|---|---|
| `.env.shared` | yes | **yes** | Team-shared values (API keys, ServiceNow creds, Slack webhook). Edit this when a shared secret changes. |
| `.env` | **no** (gitignored) | n/a | Your local working copy. Code reads this. Start with `cp .env.shared .env` after first unlock; add personal overrides here. |
| `.env.example` | yes | no (plaintext) | Public template — the list of variable names and how they're used. |
| `.env.local`, `.env.*.local` | **no** (gitignored) | n/a | Per-machine overrides if you want them. |

The split exists because `KUBECONFIG`, `OLLAMA_HOST`, sometimes `AIOPS_LLM_PROVIDER` differ per developer — `.env.shared` keeps the team-common values, `.env` lets you override locally without committing those overrides.

---

## TL;DR

| Scenario | Command |
|---|---|
| Fresh clone, you're a trusted user | `git-crypt unlock`, then `cp .env.shared .env` |
| Joining the team | Generate a GPG key, send the pubkey to Chinmay, then `git-crypt unlock` after he merges his add-user commit |
| Updating a shared secret | Edit `.env.shared`, `git commit`, `git push`. git-crypt encrypts on the way in. Tell teammates to pull and refresh their `.env`. |
| Check what's encrypted | `git-crypt status` |
| Lock the working copy (re-encrypt locally) | `git-crypt lock` |

---

## One-time setup

### Tools you need

| Tool | Windows install |
|---|---|
| GnuPG 2.x | `winget install GnuPG.GnuPG` (needs admin once) |
| git-crypt 0.7.0 | Drop the binary from <https://github.com/AGWA/git-crypt/releases> into `%LOCALAPPDATA%\Programs\git-crypt\git-crypt.exe` and add that dir to your user PATH |

Verify:

```powershell
gpg --version       # 2.4+ is fine
git-crypt --version # 0.7.0
```

### Generate your GPG key

Pick a passphrase you'll remember — losing it is the same as losing access to every encrypted secret you can currently read.

```powershell
gpg --full-generate-key
```

Answer the prompts:

- Kind: **(1) RSA and RSA**
- Size: **4096**
- Expiry: **2y** (a number ending in `y` — not `0`; expiring keys force healthy rotation)
- Real name: your name
- Email: your **Zensar work email**
- Passphrase: pick a strong one (a password manager is fine to remember it)

Confirm it's there:

```powershell
gpg --list-secret-keys --keyid-format=long
```

You should see one entry; copy the `sec   rsa4096/<KEY_ID>` value — that's your key ID.

---

## Onboarding workflow

### Joining the team (you're the new teammate)

1. Generate your GPG key (above).
2. Export your **public** key and send it to Chinmay through any channel:

   ```powershell
   gpg --armor --export your.email@zensar.com > my-pubkey.asc
   ```

   Send `my-pubkey.asc` (it's safe to share — it's the public half).

3. Wait for Chinmay's "you're added, pull" message.
4. Pull, then unlock, then copy:

   ```powershell
   git pull
   git-crypt unlock
   Copy-Item .env.shared .env
   ```

5. `.env` is now your local working copy. Edit it for any personal overrides.

### Adding a new teammate (Chinmay / repo owner)

1. Import their pubkey (they sent you `their-pubkey.asc`):

   ```powershell
   gpg --import their-pubkey.asc
   gpg --list-keys their.email@zensar.com
   ```

2. **Trust the key locally** so `git-crypt add-gpg-user` doesn't refuse it. The easiest path is the helper script:

   ```powershell
   .\scripts\secrets\add-teammate.ps1 -PubkeyFile .\their-pubkey.asc
   ```

   It imports, sets ultimate trust, runs `git-crypt add-gpg-user`, and creates the commit. All you have left is `git push`.

   Manual equivalent:

   ```powershell
   gpg --edit-key their.email@zensar.com
   # at the gpg> prompt: trust, then 5 (ultimate), y, quit
   git-crypt add-gpg-user their.email@zensar.com
   git push
   ```

3. Tell them to `git pull && git-crypt unlock`, then `cp .env.shared .env`.

> **Note:** A new teammate can't unlock anything committed **before** the add-user commit lands — old commits remain encrypted with the old key set. In practice this doesn't matter because the *current* `.env.shared` is decryptable from the moment they unlock; they don't need to read historical secrets.

---

## Updating a shared secret

Edit `.env.shared` (not `.env` — that's your local copy):

```powershell
notepad .env.shared
git add .env.shared
git commit -m "rotate ANTHROPIC_API_KEY"
git push
```

git-crypt's smudge/clean filters encrypt on the way in. Sanity-check by inspecting the staged blob — `git cat-file -p :.env.shared | head -c 9` should print `\0GITCRYPT` (the encrypted-file magic).

Tell teammates to:

```powershell
git pull
# they decide: blow away their local .env and re-copy, or merge by hand
Copy-Item .env.shared .env -Force
```

---

## Common commands

```powershell
git-crypt status                      # what's encrypted vs plaintext
git-crypt status -e                   # only encrypted files
git-crypt lock                        # re-encrypt locally (e.g. before sharing a laptop)
git-crypt unlock                      # decrypt (uses your GPG key automatically)
git-crypt export-key .\team.key       # export the symmetric key (BACKUP — see below)
git-crypt unlock .\team.key           # unlock with the symmetric key instead of GPG
```

---

## Backups and break-glass

If every trusted GPG key is lost — wiped laptops, forgotten passphrases — the encrypted `.env.shared` is unrecoverable from git. Mitigations:

1. **Export the symmetric key once** and keep it somewhere recoverable but private:

   ```powershell
   git-crypt export-key C:\path\that\is\not\this\repo\aiops-poc-team.key
   ```

   Treat that file like the password to a vault. Acceptable storage: 1Password / Bitwarden vault, Zensar-issued personal OneDrive (not Teams / shared channels). Unacceptable: this repo, email, chat.

2. The exported key is a fallback for `git-crypt unlock <keyfile>` — anyone who has the file can decrypt the repo, regardless of GPG state.

---

## What about the existing chat-shared `.env`?

If a teammate previously received `.env` through chat:

- Their copy is still on disk — git-crypt doesn't change that.
- After their GPG key is added and they `git pull && git-crypt unlock`, `.env.shared` becomes the canonical source. They run `cp .env.shared .env` (or keep their personal `.env` and use `.env.shared` as a diff target).
- **Rotate any secret that lived in a chat message** that's now in someone's mailbox / Slack scrollback / Teams history. Encrypted-in-repo doesn't fix encrypted-in-chat — chat history is the leak surface, not the repo.

---

## Alternative we considered: SOPS + age

For larger teams or stricter compliance, [SOPS](https://github.com/getsops/sops) + [age](https://age-encryption.org/) is the modern equivalent — encrypts per-key (you can re-key without rewriting history) and integrates with cloud KMS. git-crypt is simpler for a 4-person POC and was the explicit ask; revisit SOPS post-POC if the secret count grows or auditors get involved.
