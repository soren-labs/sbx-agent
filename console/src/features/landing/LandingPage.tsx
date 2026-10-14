import { useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  BookOpen,
  Boxes,
  GitFork,
  GitPullRequest,
  Github,
  History,
  KeyRound,
  Languages,
  Lock,
  Moon,
  Network,
  ShieldCheck,
  Sun,
  TerminalSquare,
} from "lucide-react";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useTheme } from "../../theme";
import { IconButton } from "../../ui/Button";
import { ProductPreview } from "./ProductPreview";

const DOCS_URL = "https://docs.sbx-agent.com";
const QUICK_START_URL = `${DOCS_URL}/getting-started/quick-start/`;
const GITHUB_URL = "https://github.com/soren-labs/sbx-agent";
const SECURITY_URL = `${GITHUB_URL}/blob/main/SECURITY.md`;

/** The official CLIs SBX supervises (see docs/specs/unified/harnesses). */
const CLIS = ["OpenCode", "Codex", "Claude Code", "Grok Build", "Command Code", "Antigravity"];

const STEPS: { n: string; title: I18nKey; body: I18nKey }[] = [
  { n: "01", title: "landing.how.1.title", body: "landing.how.1.body" },
  { n: "02", title: "landing.how.2.title", body: "landing.how.2.body" },
  { n: "03", title: "landing.how.3.title", body: "landing.how.3.body" },
];

const FEATURES: { icon: typeof TerminalSquare; title: I18nKey; body: I18nKey }[] = [
  { icon: TerminalSquare, title: "landing.f.cli.title", body: "landing.f.cli.body" },
  { icon: History, title: "landing.f.durable.title", body: "landing.f.durable.body" },
  { icon: Boxes, title: "landing.f.sandbox.title", body: "landing.f.sandbox.body" },
  { icon: GitPullRequest, title: "landing.f.delivery.title", body: "landing.f.delivery.body" },
  { icon: KeyRound, title: "landing.f.byok.title", body: "landing.f.byok.body" },
  { icon: GitFork, title: "landing.f.children.title", body: "landing.f.children.body" },
];

const SECURITY: { icon: typeof Lock; text: I18nKey }[] = [
  { icon: Lock, text: "landing.sec.1" },
  { icon: ShieldCheck, text: "landing.sec.2" },
  { icon: Network, text: "landing.sec.3" },
  { icon: History, text: "landing.sec.4" },
];

const SDK_SNIPPET = `import os
from sbx import SBXClient

client = SBXClient("https://sbx-agent.com", api_key=os.environ["SBX_API_KEY"])
result = client.execute(
    "Fix the flaky retry test",
    repository={"full_name": "acme/payments"},
    executor={"backend": "modal"},
)
changeset = client.changesets.wait_ready(
    result["session_id"], source_turn_id=result["turn"]["id"]
)
client.deliveries.request(changeset["id"])  # opens the pull request`;

const CLI_SNIPPET = `$ sbx auth login --email you@example.com
$ sbx sessions create --harness claude \\
    --message "Fix the flaky retry test"
$ sbx execute "Add a rate limiter to the API" \\
    --project payments`;

function Brand() {
  return (
    <span className="lp-brand">
      <span className="lp-brand__mark" aria-hidden="true">
        S
      </span>
      SBX Agent
    </span>
  );
}

function Header() {
  const { t, locale, setLocale } = useI18n();
  const { resolved, setTheme } = useTheme();
  const nextTheme = resolved === "dark" ? "light" : "dark";
  return (
    <header className="lp-header">
      <div className="lp-container lp-header__inner">
        <Link to="/" className="lp-header__home" aria-label="SBX Agent">
          <Brand />
        </Link>
        <nav className="lp-header__nav" aria-label={t("landing.nav.label")}>
          <a href="#features">{t("landing.nav.product")}</a>
          <a href="#how">{t("landing.nav.how")}</a>
          <a href="#developers">{t("landing.nav.developers")}</a>
          <a href={DOCS_URL}>{t("landing.nav.docs")}</a>
        </nav>
        <div className="lp-header__actions">
          <IconButton
            label={t("landing.nav.language")}
            icon={<Languages size={15} />}
            onClick={() => setLocale(locale === "en" ? "zh-CN" : "en")}
          />
          <IconButton
            label={t(`theme.${nextTheme}` as const)}
            icon={nextTheme === "dark" ? <Moon size={15} /> : <Sun size={15} />}
            onClick={() => setTheme(nextTheme)}
          />
          <a className="icon-btn lp-hide-sm" href={GITHUB_URL} aria-label="GitHub" title="GitHub">
            <Github size={15} />
          </a>
          <Link to="/login" className="lp-link lp-hide-sm">
            {t("landing.nav.sign_in")}
          </Link>
          <Link to="/register" className="lp-btn lp-btn--primary lp-btn--sm">
            {t("landing.nav.get_started")}
          </Link>
        </div>
      </div>
    </header>
  );
}

function CodeTabs() {
  const { t } = useI18n();
  const [tab, setTab] = useState<"sdk" | "cli">("sdk");
  return (
    <div className="lp-code">
      <div className="lp-code__tabs" role="tablist" aria-label={t("landing.dev.tabs")}>
        {(["sdk", "cli"] as const).map((id) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            className={`lp-code__tab${tab === id ? " lp-code__tab--active" : ""}`}
            onClick={() => setTab(id)}
          >
            {t(id === "sdk" ? "landing.dev.tab_sdk" : "landing.dev.tab_cli")}
          </button>
        ))}
      </div>
      <pre className="lp-code__body" role="tabpanel">
        <code>{tab === "sdk" ? SDK_SNIPPET : CLI_SNIPPET}</code>
      </pre>
    </div>
  );
}

export function LandingPage() {
  const { t } = useI18n();
  useTheme();
  return (
    <div className="landing">
      <a className="skip-link" href="#main">
        {t("common.skip")}
      </a>
      <Header />
      <main id="main">
        <section className="lp-hero">
          <div className="lp-container lp-hero__inner">
            <a className="lp-eyebrow" href={QUICK_START_URL}>
              {t("landing.hero.eyebrow")} <ArrowRight size={13} />
            </a>
            <h1 className="lp-hero__title">{t("landing.hero.title")}</h1>
            <p className="lp-hero__subtitle">{t("landing.hero.subtitle")}</p>
            <div className="lp-hero__actions">
              <Link to="/register" className="lp-btn lp-btn--primary">
                {t("landing.hero.cta_primary")} <ArrowRight size={15} />
              </Link>
              <a href={DOCS_URL} className="lp-btn lp-btn--secondary">
                <BookOpen size={15} /> {t("landing.hero.cta_secondary")}
              </a>
            </div>
          </div>
          <div className="lp-container lp-hero__preview">
            <ProductPreview label={t("landing.hero.preview_label")} />
          </div>
        </section>

        <section className="lp-clis" aria-label={t("landing.clis.label")}>
          <div className="lp-container">
            <p className="lp-clis__label">{t("landing.clis.label")}</p>
            <ul className="lp-clis__list">
              {CLIS.map((name) => (
                <li key={name}>{name}</li>
              ))}
            </ul>
          </div>
        </section>

        <section className="lp-section" id="how" aria-labelledby="how-h">
          <div className="lp-container">
            <div className="lp-section__head">
              <p className="lp-kicker">{t("landing.how.kicker")}</p>
              <h2 id="how-h">{t("landing.how.title")}</h2>
              <p>{t("landing.how.subtitle")}</p>
            </div>
            <ol className="lp-steps">
              {STEPS.map((s) => (
                <li key={s.n} className="lp-step">
                  <span className="lp-step__n">{s.n}</span>
                  <h3>{t(s.title)}</h3>
                  <p>{t(s.body)}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section className="lp-section" id="features" aria-labelledby="features-h">
          <div className="lp-container">
            <div className="lp-section__head">
              <p className="lp-kicker">{t("landing.f.kicker")}</p>
              <h2 id="features-h">{t("landing.f.title")}</h2>
              <p>{t("landing.f.subtitle")}</p>
            </div>
            <div className="lp-features">
              {FEATURES.map(({ icon: Icon, title, body }) => (
                <article key={title} className="lp-feature">
                  <span className="lp-feature__icon" aria-hidden="true">
                    <Icon size={17} />
                  </span>
                  <h3>{t(title)}</h3>
                  <p>{t(body)}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="lp-section" id="developers" aria-labelledby="dev-h">
          <div className="lp-container lp-split">
            <div className="lp-split__text">
              <p className="lp-kicker">{t("landing.dev.kicker")}</p>
              <h2 id="dev-h">{t("landing.dev.title")}</h2>
              <p>{t("landing.dev.body")}</p>
              <ul className="lp-checks">
                <li>{t("landing.dev.b1")}</li>
                <li>{t("landing.dev.b2")}</li>
                <li>{t("landing.dev.b3")}</li>
              </ul>
              <a className="lp-link lp-link--arrow" href={`${DOCS_URL}/sdk/python/`}>
                {t("landing.dev.link")} <ArrowRight size={14} />
              </a>
            </div>
            <CodeTabs />
          </div>
        </section>

        <section className="lp-section" id="security" aria-labelledby="sec-h">
          <div className="lp-container">
            <div className="lp-section__head">
              <p className="lp-kicker">{t("landing.sec.kicker")}</p>
              <h2 id="sec-h">{t("landing.sec.title")}</h2>
              <p>{t("landing.sec.subtitle")}</p>
            </div>
            <ul className="lp-security">
              {SECURITY.map(({ icon: Icon, text }) => (
                <li key={text}>
                  <Icon size={16} aria-hidden="true" />
                  <span>{t(text)}</span>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section className="lp-cta" aria-labelledby="cta-h">
          <div className="lp-container lp-cta__inner">
            <h2 id="cta-h">{t("landing.cta.title")}</h2>
            <p>{t("landing.cta.body")}</p>
            <div className="lp-hero__actions">
              <Link to="/register" className="lp-btn lp-btn--primary">
                {t("landing.cta.primary")} <ArrowRight size={15} />
              </Link>
              <a href={QUICK_START_URL} className="lp-btn lp-btn--secondary">
                {t("landing.cta.secondary")}
              </a>
            </div>
          </div>
        </section>
      </main>

      <footer className="lp-footer">
        <div className="lp-container lp-footer__inner">
          <div className="lp-footer__brand">
            <Brand />
            <p>{t("landing.footer.tagline")}</p>
          </div>
          <nav className="lp-footer__links" aria-label={t("landing.footer.label")}>
            <a href={DOCS_URL}>{t("landing.nav.docs")}</a>
            <a href={GITHUB_URL}>GitHub</a>
            <a href={SECURITY_URL}>{t("landing.footer.security")}</a>
            <Link to="/login">{t("landing.nav.sign_in")}</Link>
          </nav>
          <p className="lp-footer__copy">© {new Date().getFullYear()} Soren Labs</p>
        </div>
      </footer>
    </div>
  );
}
