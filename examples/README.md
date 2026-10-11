# examples/

`unified_mvp.py` — SDK walkthrough against the unified `/api`: create a Session on a
repository, wait for the Turn's ChangeSet, run a review Delegation, then request a Delivery
(GitHub pull request).

```bash
SBX_BASE_URL=http://127.0.0.1:8800 SBX_API_KEY=... python examples/unified_mvp.py owner/repo
```

The API key comes from `sbx auth login` (stored by the CLI) or **Settings → API keys** in the
Console. Inference API key, Modal and GitHub Connections must already be configured for the account.

## `subscription_machines.py`

Adds a cloud machine (Machine Slot) for one provider, finishes its official sign-in, prints the
models and reasoning efforts the signed-in CLI reports, and optionally starts one Session on it.
Providers are `codex`, `grok` and `antigravity`. A verified Modal Connection is required.

```bash
SBX_BASE_URL=http://127.0.0.1:8800 SBX_API_KEY=... python examples/subscription_machines.py grok
SBX_BASE_URL=http://127.0.0.1:8800 SBX_API_KEY=... python examples/subscription_machines.py \
  antigravity --prompt "Summarize this repository."
```

Device-code providers (`codex`, `grok`) print a sign-in URL and a one-time code. For `antigravity`
the script prints the URL, and you paste the code the browser shows when it asks; the code is read
without echo and sent once. Sign in yourself in your own browser: the script never handles your
provider password.
