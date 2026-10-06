# examples/

`unified_mvp.py` — SDK walkthrough against the unified `/api`: create a Session on a
repository, wait for the Turn's ChangeSet, run a review Delegation, then request a Delivery
(GitHub pull request).

```bash
SBX_BASE_URL=http://127.0.0.1:8800 SBX_API_KEY=... python examples/unified_mvp.py owner/repo
```

The API key comes from `sbx auth login` (stored by the CLI) or **Settings → API keys** in the
Console. Modal, OpenCode Zen and GitHub Connections must already be configured for the account.
