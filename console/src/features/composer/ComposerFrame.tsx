import { useEffect, useRef, type FormEvent, type KeyboardEvent, type ReactNode, type RefObject } from "react";

/**
 * Shared composer card: optional context row, an auto-growing textarea
 * (min-height via CSS, capped at 220px) and a two-sided toolbar. Enter sends,
 * Shift+Enter inserts a line, Ctrl/⌘+Enter also sends; IME composition is
 * never interrupted. Escape blurs the field (open pickers close themselves).
 */
export function ComposerFrame({
  label,
  value,
  onChange,
  placeholder,
  inputLabel,
  inputRef,
  context,
  toolbarLeft,
  toolbarRight,
  onSubmit,
  disabled,
}: {
  /** Accessible form name (aria-label). */
  label: string;
  /** Accessible name of the textarea itself. */
  inputLabel: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  inputRef?: RefObject<HTMLTextAreaElement | null>;
  context?: ReactNode;
  toolbarLeft?: ReactNode;
  toolbarRight?: ReactNode;
  /** Called on Enter / Ctrl-Enter / form submit; the host decides whether to act. */
  onSubmit: () => void;
  disabled?: boolean;
}) {
  const areaRef = useRef<HTMLTextAreaElement | null>(null);

  // Auto-grow up to the 220px ceiling, then let the field scroll.
  useEffect(() => {
    const el = areaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [value]);

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    onSubmit();
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      onSubmit();
    } else if (e.key === "Escape") {
      e.currentTarget.blur();
    }
  };

  return (
    <form className="composer" aria-label={label} onSubmit={submit}>
      {context}
      <textarea
        ref={(el) => {
          areaRef.current = el;
          if (inputRef) (inputRef as { current: HTMLTextAreaElement | null }).current = el;
        }}
        className="composer__input"
        rows={2}
        aria-label={inputLabel}
        placeholder={placeholder}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKey}
      />
      <div className="composer__toolbar">
        <div className="composer__tools">{toolbarLeft}</div>
        <div className="composer__tools composer__tools--right">{toolbarRight}</div>
      </div>
    </form>
  );
}
