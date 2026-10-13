import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Bug, FileText, FlaskConical, RefreshCw } from "lucide-react";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useAuth } from "../../state/auth";
import { useDocumentTitle } from "../../state/title";
import { StatusDot, type Tone } from "../../ui/Pill";
import { NewSessionComposer } from "../composer/Composer";
import { useConnections } from "../connections/useConnections";
import { computeSetup, type SetupStatus } from "../setup/checklist";

const SUGGESTIONS: { icon: typeof Bug; label: I18nKey; prompt: I18nKey }[] = [
  { icon: Bug, label: "composer.suggest.bug", prompt: "composer.suggest.bug_prompt" },
  { icon: FlaskConical, label: "composer.suggest.tests", prompt: "composer.suggest.tests_prompt" },
  { icon: RefreshCw, label: "composer.suggest.refactor", prompt: "composer.suggest.refactor_prompt" },
  { icon: FileText, label: "composer.suggest.docs", prompt: "composer.suggest.docs_prompt" },
];

const STATUS_TONE: Record<SetupStatus, Tone> = {
  ready: "ok",
  pending: "warn",
  attention: "err",
  missing: "dim",
};

export function HomePage() {
  const { t } = useI18n();
  const { me } = useAuth();
  useDocumentTitle(t("nav.new"));
  const connections = useConnections();
  const [prompt, setPrompt] = useState("");
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  // Server-provided health only: the checklist never decides readiness itself.
  const setup = computeSetup(me, connections.data?.items ?? []);
  const incomplete = setup.items.filter((i) => i.id !== "account" && i.status !== "ready");

  const fillPrompt = (text: string) => {
    setPrompt(text);
    requestAnimationFrame(() => {
      const el = inputRef.current;
      el?.focus();
      el?.setSelectionRange(text.length, text.length);
    });
  };

  return (
    <div className="home">
      <div className="home__col">
        <h1 className="home__heading">{t("home.heading")}</h1>
        <NewSessionComposer prompt={prompt} onPromptChange={setPrompt} inputRef={inputRef} />

        {connections.data && !setup.complete ? (
          <div className="home__setup" role="status">
            <span className="home__setup-text">{t("home.setup_needed")}</span>
            {incomplete.map((item) => (
              <Link key={item.id} to="/connections" className="home__setup-item">
                <StatusDot tone={STATUS_TONE[item.status]} />
                {t(`home.setup.${item.id}` as I18nKey)}
              </Link>
            ))}
          </div>
        ) : null}

        <div className="home__suggestions" aria-label={t("composer.suggestions")}>
          {SUGGESTIONS.map(({ icon: SugIcon, label, prompt: promptKey }) => (
            <button key={label} type="button" className="home__suggestion" onClick={() => fillPrompt(t(promptKey))}>
              <SugIcon size={13} />
              <span>{t(label)}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
