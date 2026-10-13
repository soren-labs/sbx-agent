import { Check, ChevronDown, FilePen, FolderGit2, GitCompare, Plus, Search, Server, SquareTerminal } from "lucide-react";

/**
 * A static, decorative rendering of the workbench for the landing hero. It is built
 * from the same visual language as the Console instead of a screenshot, so it never
 * shows real account data and stays sharp at any size.
 */
export function ProductPreview({ label }: { label: string }) {
  return (
    <figure className="pv" aria-label={label}>
      <div className="pv__chrome" aria-hidden="true">
        <span className="pv__dot" />
        <span className="pv__dot" />
        <span className="pv__dot" />
        <span className="pv__url">sbx-agent.com/sessions/fix-flaky-retry-test</span>
      </div>
      <div className="pv__body" aria-hidden="true">
        <aside className="pv__side">
          <div className="pv__ws">
            <span className="pv__ws-mark">A</span>
            Acme
            <ChevronDown size={12} />
          </div>
          <div className="pv__nav pv__nav--active">
            <Plus size={13} /> New session
          </div>
          <div className="pv__nav">
            <FolderGit2 size={13} /> Projects
          </div>
          <div className="pv__nav">
            <Server size={13} /> Machines
          </div>
          <div className="pv__label">Sessions</div>
          <div className="pv__item pv__item--active">
            <span>Fix flaky retry test</span>
            <small>Claude Code · running</small>
            <i className="pv__status pv__status--run" />
          </div>
          <div className="pv__item">
            <span>Add rate limiter to API</span>
            <small>Codex · waiting for you</small>
            <i className="pv__status pv__status--warn" />
          </div>
          <div className="pv__item">
            <span>Upgrade to React 19</span>
            <small>OpenCode · 2h ago</small>
          </div>
          <div className="pv__item">
            <span>Document webhook retries</span>
            <small>Grok Build · yesterday</small>
          </div>
        </aside>

        <section className="pv__conv">
          <div className="pv__head">
            <strong>Fix flaky retry test</strong>
            <span className="pv__pill">
              <i className="pv__status pv__status--run" /> Running
            </span>
            <span className="pv__meta">Claude Code · acme/payments@main</span>
          </div>
          <div className="pv__msgs">
            <div className="pv__user">
              The retry test in payments fails about 1 in 10 runs on CI. Find the cause and fix it.
            </div>
            <div className="pv__agent">
              <span className="pv__avatar">S</span> Claude Code
            </div>
            <div className="pv__work">
              <div className="pv__work-head">
                <ChevronDown size={12} /> Worked for 1m 42s <span>· 6 steps</span>
              </div>
              <div className="pv__step">
                <Search size={12} /> Searched <code>backoff</code>
                <Check size={12} className="pv__ok" />
              </div>
              <div className="pv__step">
                <SquareTerminal size={12} /> Ran <code>pytest tests/test_retry.py -x --count 50</code>
                <Check size={12} className="pv__ok" />
              </div>
              <div className="pv__step">
                <FilePen size={12} /> Edited <code>payments/retry.py</code>
                <Check size={12} className="pv__ok" />
              </div>
            </div>
            <p className="pv__text">
              The jitter used <code>random.random()</code> without a seed, so the test&apos;s timing window was
              occasionally exceeded. I injected the clock and RNG, and the test now passes 50/50 runs.
            </p>
            <div className="pv__live">
              <i className="pv__status pv__status--run" /> Running <code>pytest -q</code>
              <span>0:12</span>
            </div>
          </div>
          <div className="pv__composer">Queue a follow-up…</div>
        </section>

        <section className="pv__panel">
          <div className="pv__tabs">
            <span className="pv__tab pv__tab--active">
              <GitCompare size={12} /> Changes <b>2</b>
            </span>
            <span className="pv__tab">Files</span>
            <span className="pv__tab">Terminal</span>
          </div>
          <div className="pv__file">
            <span className="pv__badge">M</span> payments/<b>retry.py</b>
            <span className="pv__counts">
              <em>+6</em> <s>−3</s>
            </span>
          </div>
          <pre className="pv__diff">
            <span className="pv__hunk">@@ -18,9 +18,12 @@ def backoff(</span>
            <span>{"  def backoff(attempt, *,"}</span>
            <span className="pv__del">{"-              base=0.2):"}</span>
            <span className="pv__add">{"+              base=0.2, rng=random,"}</span>
            <span className="pv__add">{"+              clock=time.monotonic):"}</span>
            <span>{"      cap = min(30, base * 2 ** attempt)"}</span>
            <span className="pv__del">{"-     return cap * random.random()"}</span>
            <span className="pv__add">{"+     return cap * rng.random()"}</span>
          </pre>
          <div className="pv__deliver">
            <span>ChangeSet sealed · sha256:9f2c…e41a</span>
            <b>Open pull request</b>
          </div>
        </section>
      </div>
    </figure>
  );
}
