import { useState } from "react";
import type { Delivery } from "../../api/types";
import { ErrorNotice, Pill, useAction, when, type Tone } from "../../components/ui";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useApi } from "../../state/context";

const stateTone = (s: string): Tone =>
  s === "succeeded" || s === "merged" ? "ok" : s === "failed" || s === "cancelled" ? "err" : s === "blocked" ? "warn" : "run";

const safeUrl = (u: string | null | undefined) => (u && /^https?:\/\//i.test(u) ? u : null);

/**
 * One Delivery with its server-computed merge eligibility. The UI never decides
 * eligibility: it renders `merge_eligibility` and sends exactly its pins.
 */
/** Server vocabulary as words when a translation exists; the raw value otherwise. */
function word(t: ReturnType<typeof useI18n>["t"], prefix: string, value: string): string {
  const key = `${prefix}.${value}` as I18nKey;
  return t(key) === key ? value.replaceAll("_", " ") : t(key);
}

export function DeliveryCard({ delivery: d, onChanged }: { delivery: Delivery; onChanged: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const [markReady, setMarkReady] = useState(false);
  const elig = d.merge_eligibility;
  const merge = useAction(async (key) => {
    await api.deliveries.requestMerge(
      d.id,
      {
        expected_head_sha: elig.head_sha ?? "",
        subject_digest: elig.subject_digest ?? "",
        expected_version: d.version,
        method: "squash",
        ...(markReady ? { mark_ready: true } : {}),
      },
      { idempotencyKey: key },
    );
    onChanged();
  });
  const retry = useAction(async (key) => {
    await api.deliveries.retry(d.id, { idempotencyKey: key });
    onChanged();
  });
  const refresh = useAction(async (key) => {
    await api.deliveries.refresh(d.id, { idempotencyKey: key });
    onChanged();
  });
  const pr = d.pull_request;
  const prUrl = safeUrl(pr?.url);

  return (
    <div className="card delivery" role="group" aria-label={t("delivery.aria", { id: d.id.slice(-6) })}>
      <div className="row wrap">
        <strong className="grow">
          {word(t, "delivery.transport", d.transport)}
        </strong>
        <Pill tone={stateTone(d.state)}>{word(t, "delivery.state", d.state)}</Pill>
      </div>
      {d.state_reason ? <p className="small muted">{d.state_reason}</p> : null}
      <dl className="kv small">
        {d.commit_sha ? (
          <>
            <dt>{t("delivery.commit")}</dt>
            <dd>
              <code>{d.commit_sha.slice(0, 12)}</code>
            </dd>
          </>
        ) : null}
        {pr ? (
          <>
            <dt>{t("delivery.pr")}</dt>
            <dd>
              {prUrl ? (
                <a className="pr-link" href={prUrl} target="_blank" rel="noopener noreferrer">
                  {t("delivery.open_pr", { n: pr.number })}
                </a>
              ) : (
                `#${pr.number}`
              )}{" "}
              · {pr.state}
              {pr.draft ? ` · ${t("delivery.draft")}` : ""}
            </dd>
          </>
        ) : null}
        {d.remote?.checks_state ? (
          <>
            <dt>{t("delivery.checks")}</dt>
            <dd>{d.remote.checks_state}</dd>
          </>
        ) : null}
      </dl>
      {d.steps.length ? (
        <ol className="steps small" aria-label={t("delivery.steps")}>
          {d.steps.map((s, i) => (
            <li key={i}>
              {word(t, "delivery.step", s.kind)} <span className="faint">· {word(t, "delivery.outcome", s.outcome)}</span>
            </li>
          ))}
        </ol>
      ) : null}
      {d.merge_requests.map((m) => (
        <div key={m.id} className="small">
          {t("delivery.merge_request")}: <Pill tone={stateTone(m.state)}>{m.state}</Pill>
          {m.merge_sha ? <code> {m.merge_sha.slice(0, 12)}</code> : null}
        </div>
      ))}

      <div className="merge-gate" aria-label={t("delivery.eligibility")}>
        {elig.eligible ? (
          <p className="small" role="status">
            <Pill tone="ok">{t("delivery.eligible")}</Pill>
          </p>
        ) : (
          <div className="small">
            <Pill tone="warn">{t("delivery.not_eligible")}</Pill>
            <ul className="reasons">
              {elig.reasons.map((r) => (
                <li key={r} title={r}>
                  {word(t, "delivery.reason", r.split(":")[0])}
                  {r.includes(":") ? <code> {r.split(":").slice(1).join(":")}</code> : null}
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="faint small">
          {t("delivery.observed")}: {when(elig.observed_at)}
        </div>
      </div>

      <ErrorNotice error={merge.error ?? retry.error ?? refresh.error} />
      <div className="row wrap">
        {pr?.draft ? (
          <label className="small row">
            <input type="checkbox" checked={markReady} onChange={(e) => setMarkReady(e.target.checked)} />
            {t("delivery.mark_ready")}
          </label>
        ) : null}
        <button type="button" className="btn btn-primary btn-sm" disabled={!elig.eligible || merge.pending} onClick={() => void merge.run()}>
          {t("delivery.merge")}
        </button>
        <button type="button" className="btn btn-sm" disabled={refresh.pending} onClick={() => void refresh.run()}>
          {t("delivery.refresh")}
        </button>
        {d.state === "failed" || d.state === "blocked" ? (
          <button type="button" className="btn btn-sm" disabled={retry.pending} onClick={() => void retry.run()}>
            {t("delivery.retry")}
          </button>
        ) : null}
      </div>
    </div>
  );
}
