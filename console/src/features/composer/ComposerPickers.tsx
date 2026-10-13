import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Github, Plug, Server, TerminalSquare } from "lucide-react";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { Button } from "../../ui/Button";
import { Field, Input, Select } from "../../ui/Field";
import { harnessName } from "../sessions/harnesses";
import type { ComposerConfig } from "./useComposerConfig";

function Option({
  icon,
  children,
  selected,
  disabled,
  note,
  onClick,
}: {
  icon?: React.ReactNode;
  children: React.ReactNode;
  selected?: boolean;
  disabled?: boolean;
  note?: string;
  onClick?: () => void;
}) {
  return (
    <button type="button" className="picker__option" disabled={disabled} aria-pressed={selected} onClick={onClick}>
      {icon}
      <span className="picker__option-label truncate">
        {children}
        {note ? <span className="faint"> · {note}</span> : null}
      </span>
      {selected ? <Check size={13} className="picker__check" /> : null}
    </button>
  );
}

function PickerSection({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="picker__section">
      <div className="picker__label">{label}</div>
      {children}
    </div>
  );
}

export function RepoPicker({ cfg, close }: { cfg: ComposerConfig; close: () => void }) {
  const { t } = useI18n();
  const [q, setQ] = useState(cfg.repo);
  const repos = cfg.repos.filter((r) => r.toLowerCase().includes(q.trim().toLowerCase()));
  const typed = q.trim();

  const pick = (name: string) => {
    cfg.setRepo(name);
    close();
  };
  const commitTyped = () => {
    if (/^\S+\/\S+$/.test(typed)) pick(typed);
  };

  return (
    <div className="picker">
      <input
        className="input picker__search"
        autoFocus
        spellCheck={false}
        aria-label={t("composer.select_repo")}
        placeholder={t("composer.repo_ph")}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commitTyped();
          }
        }}
      />
      {repos.length ? (
        <div className="picker__options">
          {repos.map((r) => (
            <Option key={r} icon={<Github size={14} />} selected={cfg.repo === r} onClick={() => pick(r)}>
              {r}
            </Option>
          ))}
        </div>
      ) : (
        <p className="picker__note faint">{t("composer.repo_empty")}</p>
      )}
      <Field label={t("composer.base_ref")} htmlFor="picker-base-ref">
        <Input id="picker-base-ref" spellCheck={false} value={cfg.baseRef} onChange={(e) => cfg.setBaseRef(e.target.value)} />
      </Field>
      <div className="picker__footer">
        {cfg.repo ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              cfg.setRepo("");
              close();
            }}
          >
            {t("composer.repo_none")}
          </Button>
        ) : null}
        <span className="picker__spacer" />
        <Button size="sm" variant="ghost" onClick={close}>
          {t("composer.done")}
        </Button>
      </div>
    </div>
  );
}

export function ConfigPicker({ cfg, close, focusProject }: { cfg: ComposerConfig; close: () => void; focusProject?: boolean }) {
  const { t } = useI18n();
  const projectRef = useRef<HTMLSelectElement>(null);
  useEffect(() => {
    if (focusProject) projectRef.current?.focus();
  }, [focusProject]);
  const backendKinds = cfg.backendKinds.length ? cfg.backendKinds : ["modal", "local"];
  const backendLabel = (kind: string): string =>
    kind === "modal" || kind === "local" ? t(`composer.backend.${kind}` as I18nKey) : kind;

  return (
    <div className="picker">
      <PickerSection label={t("composer.configuration")}>
        <Field label={t("composer.compute")} htmlFor="picker-backend">
          <Select id="picker-backend" value={cfg.backend} disabled={cfg.slot !== null} onChange={(e) => cfg.setBackendOverride(e.target.value)}>
            {backendKinds.map((kind) => (
              <option key={kind} value={kind}>
                {backendLabel(kind)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t("composer.project")} htmlFor="picker-project">
          <Select id="picker-project" ref={projectRef} value={cfg.projectId} onChange={(e) => cfg.setProjectId(e.target.value)}>
            <option value="">{t("composer.no_project")}</option>
            {(cfg.projects.data?.items ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </Field>
        {!cfg.slot && cfg.usable.length > 1 ? (
          <Field label={t("composer.connection")} htmlFor="picker-connection">
            <Select id="picker-connection" value={cfg.pinned} onChange={(e) => cfg.chooseConnection(e.target.value)}>
              <option value="">{t("composer.connection_auto")}</option>
              {cfg.usable.map((c) => (
                <option key={c.connection_id} value={c.connection_id}>
                  {c.label}
                </option>
              ))}
            </Select>
          </Field>
        ) : null}
        <Field label={t("composer.base_ref")} htmlFor="picker-config-ref">
          <Input
            id="picker-config-ref"
            spellCheck={false}
            disabled={cfg.projectId !== ""}
            value={cfg.baseRef}
            onChange={(e) => cfg.setBaseRef(e.target.value)}
          />
        </Field>
      </PickerSection>
      <div className="picker__footer">
        <span className="picker__spacer" />
        <Button size="sm" variant="ghost" onClick={close}>
          {t("composer.done")}
        </Button>
      </div>
    </div>
  );
}

export function ModelPicker({ cfg, close }: { cfg: ComposerConfig; close: () => void }) {
  const { t } = useI18n();
  const effortLabel = (id: string) =>
    cfg.thinkingToggle ? t(id === "none" ? "composer.thinking_off" : "composer.thinking_on") : id;
  const slotNote = (m: (typeof cfg.slots)[number]) =>
    m.status === "running" ? t("composer.source_busy") : m.status === "ready" ? undefined : t("composer.source_needs_login");

  return (
    <div className="picker">
      <PickerSection label={t("composer.source")}>
        <div className="picker__options" data-testid="source-options">
          {cfg.hasApiKey ? (
            <Option icon={<Plug size={14} />} selected={!cfg.slot} onClick={() => cfg.chooseSource("")}>
              {t("composer.source_api")}
            </Option>
          ) : null}
          {cfg.usableSlots.map((m) => (
            <Option
              key={m.id}
              icon={<Server size={14} />}
              selected={cfg.slot?.id === m.id}
              disabled={m.status !== "ready"}
              note={slotNote(m)}
              onClick={() => cfg.chooseSource(m.id)}
            >
              {m.label} · {m.provider_name}
            </Option>
          ))}
        </div>
        {cfg.idleSlots ? (
          <p className="picker__note">
            <Link to="/machines" onClick={close}>
              {t("composer.source_more", { count: cfg.idleSlots })}
            </Link>
          </p>
        ) : null}
      </PickerSection>

      {!cfg.slot && cfg.apiOffered.length > 1 ? (
        <PickerSection label={t("composer.harness")}>
          <div className="picker__options" data-testid="harness-options">
            {cfg.apiOffered.map((h) => (
              <Option
                key={h.provider_id}
                icon={<TerminalSquare size={14} />}
                selected={h.provider_id === cfg.harness}
                onClick={() => cfg.chooseHarness(h.provider_id)}
              >
                {harnessName(h.provider_id)}
              </Option>
            ))}
          </div>
        </PickerSection>
      ) : null}

      <PickerSection label={t("composer.model")}>
        {cfg.slot ? (
          <p className="picker__note faint" data-testid="model-source">
            {cfg.catalog ? t("composer.slot_models_from", { source: cfg.catalog.source ?? "" }) : t("composer.slot_no_catalog")}
          </p>
        ) : null}
        <div className="picker__options" data-testid="model-options">
          {cfg.options.length ? (
            cfg.options.map((id) => (
              <Option key={id} selected={id === cfg.model} onClick={() => cfg.chooseModel(id)}>
                {cfg.slotModels.find((m) => m.id === id)?.name ?? id}
              </Option>
            ))
          ) : (
            <p className="picker__note faint">{t("composer.models_empty")}</p>
          )}
        </div>
      </PickerSection>

      {cfg.efforts.length ? (
        <PickerSection label={t(cfg.thinkingToggle ? "composer.thinking" : "composer.reasoning")}>
          <div className="picker__options" data-testid="effort-options">
            <Option selected={cfg.effort === ""} onClick={() => cfg.setEffortOverride(null)}>
              {t(cfg.thinkingToggle ? "composer.thinking_on" : "composer.effort_default")}
            </Option>
            {cfg.efforts.map((e) => (
              <Option key={e.id} selected={e.id === cfg.effort} onClick={() => cfg.setEffortOverride(e.id)}>
                {effortLabel(e.id)}
              </Option>
            ))}
          </div>
        </PickerSection>
      ) : null}
    </div>
  );
}
