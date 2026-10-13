import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";
import { IconButton } from "./Button";
import { useI18n } from "../i18n";

export type ToastTone = "ok" | "err" | "info";

export interface ToastInput {
  text: string;
  tone?: ToastTone;
  /** Optional action (e.g. Retry) rendered inside the toast. */
  action?: { label: string; onClick: () => void };
}

interface ToastItem extends ToastInput {
  id: number;
  tone: ToastTone;
}

const ToastCtx = createContext<(t: ToastInput) => void>(() => undefined);

/** Queue a notification from anywhere under <ToastProvider>. */
export function useToast() {
  return useContext(ToastCtx);
}

const ICONS: Record<ToastTone, ReactNode> = {
  ok: <CheckCircle2 size={15} />,
  err: <AlertCircle size={15} />,
  info: <Info size={15} />,
};

/** Bottom-right stack; errors stay until dismissed, the rest fade after 6s. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const push = useCallback((input: ToastInput) => {
    const item: ToastItem = { id: nextId.current++, text: input.text, tone: input.tone ?? "info", action: input.action };
    setItems((prev) => [...prev.slice(-4), item]);
    if (item.tone !== "err") {
      setTimeout(() => setItems((prev) => prev.filter((x) => x.id !== item.id)), 6000);
    }
  }, []);

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toast-stack" role="region" aria-label={t("toast.dismiss")}>
        {items.map((item) => (
          <div key={item.id} className={`toast toast--${item.tone}`} role={item.tone === "err" ? "alert" : "status"}>
            <span className="toast__icon">{ICONS[item.tone]}</span>
            <span className="toast__text">{item.text}</span>
            {item.action ? (
              <button type="button" className="toast__action" onClick={item.action.onClick}>
                {item.action.label}
              </button>
            ) : null}
            <IconButton
              size="sm"
              label={t("toast.dismiss")}
              icon={<X size={13} />}
              onClick={() => setItems((prev) => prev.filter((x) => x.id !== item.id))}
            />
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
