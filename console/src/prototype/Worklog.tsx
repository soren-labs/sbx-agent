import { useEffect, useState } from "react";
import type { ActivityItem, Session } from "../api/types";
import { groupActivity, type WorkGroup } from "./domain";
import { Icon, Mark } from "./Icon";
import { demoMode } from "./demo";

export function Status({ phase }: { phase: Session["phase"] }) {
  const labels = {
    running: "Working",
    queued: "Queued",
    starting: "Starting",
    idle: "Idle",
    ended: "Stopped",
    failed: "Needs attention",
  };
  return (
    <span className={`status status-${phase}`}>
      <span
        className={phase === "running" ? "status-dot pulse" : "status-dot"}
      />
      {labels[phase]}
    </span>
  );
}
function EventDetail({ item }: { item: ActivityItem }) {
  const title = item.command ?? item.path ?? item.text ?? item.kind;
  return (
    <details className="event-detail">
      <summary>
        <Icon
          name={
            item.kind === "command"
              ? "terminal"
              : item.kind === "file_change"
                ? "file"
                : item.kind === "error"
                  ? "x"
                  : "chevron"
          }
          size={13}
        />
        <span>{title}</span>
        {item.exitCode !== undefined && (
          <span
            className={`event-exit ${item.exitCode === 0 ? "positive" : "negative"}`}
          >
            exit {item.exitCode}
          </span>
        )}
        {item.status === "running" && <span className="working-ring" />}
      </summary>
      {item.output && <pre>{item.output}</pre>}
      {item.changes && (
        <div className="event-files">
          {item.changes.map((c) => (
            <span key={c.path}>
              <Icon name="file" />
              {c.path}
              <em>{c.kind}</em>
            </span>
          ))}
        </div>
      )}
      <details className="raw-event">
        <summary>Event details</summary>
        <pre>{JSON.stringify(item, null, 2)}</pre>
      </details>
    </details>
  );
}
function WorkBlock({
  group,
  active,
  onContext,
  startedAt,
  finishedAt,
}: {
  startedAt?: string | null;
  finishedAt?: string | null;
  group: WorkGroup;
  active: boolean;
  onContext: (tab: string) => void;
}) {
  const running = active;
  const [open, setOpen] = useState(running);
  useEffect(() => setOpen(running), [running]);
  const start = startedAt ? new Date(startedAt).getTime() : NaN;
  const end = running ? Date.now() : finishedAt ? new Date(finishedAt).getTime() : NaN;
  const duration = Number.isFinite(start) && Number.isFinite(end) ? Math.max(1, Math.round((end-start)/1000)) : null;
  const elapsed = duration === null ? null : duration < 60 ? `${duration}s` : `${Math.floor(duration/60)}m ${duration%60}s`;
  const outputs = group.items.map((i) => i.output ?? "").join(" ");
  const passed = /(?:Tests\s+|^)(\d+) passed/m.exec(outputs)?.[1];
  return (
    <div
      className={`work-block ${open ? "expanded" : ""} ${running ? "work-running" : ""}`}
    >
      <div className="work-summary-row">
        <button
          className="work-summary"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          <Icon name="chevron" className={open ? "rotated" : ""} size={13} />
          <strong>
            {running ? "Working" : "Worked"}{elapsed ? ` for ${elapsed}` : ""}
          </strong>
          {running && <span className="working-ring" />}
          {passed && <span className="work-result">{passed} tests passed</span>}
        </button>
        {group.category === "changes" && (
          <button
            className="work-changes-link"
            onClick={() => onContext("changes")}
          >
            <Icon name="file" size={12} />
            Changes
          </button>
        )}
      </div>
      {open && (
        <div className="work-evidence">
          {group.items.map((item) => (
            <EventDetail key={item.id} item={item} />
          ))}
        </div>
      )}
      {running && <div className="current-work-caption">{group.title}</div>}
    </div>
  );
}
export function PRCard({
  session,
  onReview,
}: {
  session: Session;
  onReview: () => void;
}) {
  return (
    <button className="pr-outcome" onClick={onReview}>
      <span className="pr-icon">
        <Icon name="pr" size={19} />
      </span>
      <span>
        <span className="pr-card-label">
          {session.delivery?.prState === "draft"
            ? "Draft pull request"
            : "Pull request"}{" "}
          {session.delivery?.prNumber ? ` · #${session.delivery.prNumber}` : ""}
        </span>
        <strong>{session.title}</strong>
        <span className="pr-card-meta">
          {session.repo?.name}
          {demoMode && (
            <>
              <span className="positive">+34</span>
              <span className="negative">−5</span>
            </>
          )}
        </span>
      </span>
      <Icon name="chevron" />
      <span className="pr-card-footer">
        <span>
          <Icon name="check" size={13} />
          {demoMode ? "Demo checks passed" : "Ready for review"}
        </span>
        <span>
          Review pull request <Icon name="external" size={12} />
        </span>
      </span>
    </button>
  );
}
export function Worklog({
  session,
  onContext,
  progressOnly = false,
}: {
  session: Session;
  onContext: (tab: string) => void;
  progressOnly?: boolean;
}) {
  const active = ["running", "queued", "starting"].includes(session.phase);
  return (
    <div className="worklog-content">
      <div className="timeline-day">
        Today <span>{demoMode ? "Demo session" : "Session worklog"}</span>
      </div>
      {!session.turns.length && !progressOnly && <article className="message user-message"><p>{session.prompt}</p></article>}
      {session.turns.map((turn) => (
        <div key={turn.id} className="turn">
          {!progressOnly && (
            <article className="message user-message">
              <div className="message-head">
                <span className="avatar user-avatar">S</span>
                <strong>You</strong>
                <time>
                  {new Date(turn.createdAt).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </time>
              </div>
              <p>{turn.prompt}</p>
              {session.repo && (
                <span className="repo-mention">
                  <Icon name="github" size={12} />
                  {session.repo.name}
                </span>
              )}
            </article>
          )}
          <div className="agent-work">
            <div className="message-head">
              <Mark small />
              <strong>SBX</strong>
              <span className="muted">
                {session.provider === "codex" ? "Codex" : session.provider}
              </span>
              <time>
                {new Date(turn.startedAt ?? turn.createdAt).toLocaleTimeString(
                  [],
                  { hour: "2-digit", minute: "2-digit" },
                )}
              </time>
            </div>
            <div className="agent-body">
              {groupActivity(turn.activity).map((entry, index, entries) =>
                entry.type === "message" ? (
                  !progressOnly && (
                    <p key={entry.item.id} className="assistant-message">
                      {entry.item.text}
                    </p>
                  )
                ) : entry.type === "group" ? (
                  <WorkBlock
                    key={entry.group.id}
                    group={entry.group}
                    active={
                      active &&
                      turn.status === "running" &&
                      index === entries.length - 1
                    }
                    startedAt={entries.filter(e => e.type === "group").length === 1 || (active && turn.status === "running" && index === entries.length - 1) ? turn.startedAt : null}
                    finishedAt={turn.finishedAt}
                    onContext={onContext}
                  />
                ) : null,
              )}
              {turn.result &&
                !progressOnly &&
                !turn.activity.some(
                  (item) =>
                    item.kind === "message" &&
                    item.text?.trim() === turn.result?.trim(),
                ) && <p className="assistant-message">{turn.result}</p>}
              {turn.status === "queued" && (
                <p className="muted">
                  <span className="working-ring" />
                  Preparing your workspace…
                </p>
              )}
            </div>
          </div>
        </div>
      ))}
      {session.phase === "idle" && session.delivery?.status === "delivered" && (
        <div className="outcome-wrap">
          <PRCard session={session} onReview={() => onContext("review")} />
        </div>
      )}
      {active && (
        <div className="live-work">
          <span className="working-ring" />
          <span>
            {session.phase === "running"
              ? "SBX is working"
              : "Preparing the session"}
            <small>Activity updates automatically</small>
          </span>
          <span className="live-wave">
            <i />
            <i />
            <i />
            <i />
          </span>
        </div>
      )}
      {session.phase === "ended" && (
        <div className="notice">
          Session stopped. Work performed so far is preserved.
        </div>
      )}
    </div>
  );
}
