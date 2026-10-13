import { useState, type RefObject } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowUp, ChevronDown, FolderGit2, Github, Plus, SlidersHorizontal, X } from "lucide-react";
import { useI18n } from "../../i18n";
import { useApi, useQueryClient } from "../../state/context";
import { IconButton } from "../../ui/Button";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Menu, MenuItem } from "../../ui/Menu";
import { Popover } from "../../ui/Popover";
import { Spinner } from "../../ui/Spinner";
import { useAction } from "../../ui/useAction";
import { harnessName } from "../sessions/harnesses";
import { ComposerFrame } from "./ComposerFrame";
import { ConfigPicker, ModelPicker, RepoPicker } from "./ComposerPickers";
import { useComposerConfig } from "./useComposerConfig";

type Picker = "repo" | "config" | "model" | null;

export function NewSessionComposer({
  prompt,
  onPromptChange,
  inputRef,
}: {
  prompt: string;
  onPromptChange: (prompt: string) => void;
  /** Lets the host focus/select the textarea (e.g. suggestion chips). */
  inputRef?: RefObject<HTMLTextAreaElement | null>;
}) {
  const { t } = useI18n();
  const api = useApi();
  const qc = useQueryClient();
  const nav = useNavigate();
  const cfg = useComposerConfig();
  const { w } = cfg;
  const [picker, setPicker] = useState<Picker>(null);
  const [focusProject, setFocusProject] = useState(false);

  const create = useAction(async (key) => {
    const r = await api.sessions.create(w!, cfg.buildCreateBody(prompt), { idempotencyKey: key });
    qc.invalidate(["sessions", w]);
    nav(`/sessions/${r.session_id}`);
  });

  const submit = () => {
    if (!prompt.trim() || !w || create.pending || cfg.blocked || !cfg.ready) return;
    void create.run();
  };

  const openPicker = (next: Exclude<Picker, null>) => {
    setFocusProject(false);
    setPicker(next);
  };
  const pickerChange = (id: Exclude<Picker, null>) => (open: boolean) => setPicker(open ? id : null);

  const sourceLabel = cfg.slot ? cfg.slot.label : harnessName(cfg.harness);
  const effortText = cfg.effort
    ? cfg.thinkingToggle
      ? t(cfg.effort === "none" ? "composer.thinking_off" : "composer.thinking_on")
      : cfg.effort
    : "";

  return (
    <>
      <ComposerFrame
        label={t("composer.heading")}
        inputLabel={t("composer.task")}
        placeholder={t("composer.task_ph")}
        value={prompt}
        onChange={onPromptChange}
        inputRef={inputRef}
        onSubmit={submit}
        context={
          cfg.repo ? (
            <div className="composer__context">
              <Popover
                side="top"
                width={360}
                open={picker === "repo"}
                onOpenChange={pickerChange("repo")}
                trigger={
                  <button type="button" className="composer__chip">
                    <Github size={13} />
                    <span className="truncate">{cfg.repo}</span>
                    <span className="faint">· {cfg.baseRef}</span>
                  </button>
                }
              >
                <RepoPicker cfg={cfg} close={() => setPicker(null)} />
              </Popover>
              <IconButton size="sm" label={t("composer.repo_none")} icon={<X size={13} />} onClick={() => cfg.setRepo("")} />
            </div>
          ) : null
        }
        toolbarLeft={
          <>
            <Menu side="top" label={t("composer.add")} trigger={<IconButton label={t("composer.add")} icon={<Plus size={16} />} />}>
              <MenuItem icon={<Github size={14} />} label={t("composer.add_repo")} onSelect={() => openPicker("repo")} />
              <MenuItem
                icon={<FolderGit2 size={14} />}
                label={t("composer.use_project")}
                onSelect={() => {
                  setFocusProject(true);
                  setPicker("config");
                }}
              />
            </Menu>
            {!cfg.repo ? (
              <Popover
                side="top"
                width={360}
                open={picker === "repo"}
                onOpenChange={pickerChange("repo")}
                trigger={<span className="composer__anchor" aria-hidden="true" />}
              >
                <RepoPicker cfg={cfg} close={() => setPicker(null)} />
              </Popover>
            ) : null}
            <Popover
              side="top"
              width={360}
              open={picker === "config"}
              onOpenChange={pickerChange("config")}
              trigger={<IconButton label={t("composer.config")} icon={<SlidersHorizontal size={15} />} />}
            >
              <ConfigPicker cfg={cfg} close={() => setPicker(null)} focusProject={focusProject} />
            </Popover>
          </>
        }
        toolbarRight={
          <>
            <Popover
              side="top"
              align="end"
              width={360}
              open={picker === "model"}
              onOpenChange={pickerChange("model")}
              trigger={
                <button type="button" className="composer__model-chip" data-testid="agent-model-chip">
                  <span className="truncate">
                    {sourceLabel}
                    <span className="faint">
                      {" "}
                      · {cfg.model || t("composer.model_server")}
                      {effortText ? ` · ${effortText}` : ""}
                    </span>
                  </span>
                  <ChevronDown size={12} />
                </button>
              }
            >
              <ModelPicker cfg={cfg} close={() => setPicker(null)} />
            </Popover>
            <button
              type="submit"
              className="composer__send"
              aria-label={t("composer.start")}
              title={cfg.ready ? t("composer.start_title") : t("composer.loading_config")}
              disabled={!prompt.trim() || !w || create.pending || cfg.blocked || !cfg.ready}
            >
              {create.pending || !cfg.ready ? <Spinner small /> : <ArrowUp size={16} />}
            </button>
          </>
        }
      />

      {cfg.blocked ? (
        <p className="composer__blocked" role="status">
          {t("composer.blocked", { harness: harnessName(cfg.harness) })}{" "}
          <Link to="/connections">{t("composer.blocked_add_key")}</Link> {t("composer.blocked_or")}{" "}
          <Link to="/machines">{t("composer.blocked_machine")}</Link>
        </p>
      ) : null}

      <ErrorNotice error={create.error} onRetry={() => void create.run()} retryLabel={t("composer.retry_create")} />
    </>
  );
}
