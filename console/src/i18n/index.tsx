import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { en, type I18nKey } from "./en";
import { zhCN } from "./zh-CN";

export type Locale = "en" | "zh-CN";
const LOCALES: Record<Locale, Record<I18nKey, string>> = {
  en,
  "zh-CN": zhCN,
};
const KEY = "sbx.console.locale";

interface I18nCtx {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: (key: I18nKey, vars?: Record<string, string | number>) => string;
}

const Ctx = createContext<I18nCtx>({
  locale: "en",
  setLocale: () => undefined,
  t: (k) => en[k] ?? k,
});

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => {
    const saved = localStorage.getItem(KEY);
    if (saved === "zh-CN" || saved === "en") return saved;
    return navigator.language.startsWith("zh") ? "zh-CN" : "en";
  });

  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l);
    localStorage.setItem(KEY, l);
    document.documentElement.lang = l;
  }, []);

  const t = useCallback(
    (key: I18nKey, vars?: Record<string, string | number>) => {
      let s = LOCALES[locale][key] ?? en[key] ?? key;
      if (vars) {
        for (const [k, v] of Object.entries(vars)) {
          s = s.replaceAll(`{${k}}`, String(v));
        }
      }
      return s;
    },
    [locale],
  );

  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n() {
  return useContext(Ctx);
}
