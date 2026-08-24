import { Languages } from "lucide-react";
import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";

export type AppLanguage = "zh-CN" | "en";

const STORAGE_KEY = "savedstream-language";

export function resolveLanguage(stored: string | null, browserLanguage: string): AppLanguage {
  if (stored === "zh-CN" || stored === "en") return stored;
  return browserLanguage.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}

export function localizedText(language: AppLanguage, chinese: string, english: string): string {
  return language === "en" ? english : chinese;
}

function detectLanguage(): AppLanguage {
  if (typeof window === "undefined") return "zh-CN";
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return resolveLanguage(stored, window.navigator.language);
  } catch {
    // Storage can be unavailable in hardened/private browser contexts.
  }
  return resolveLanguage(null, window.navigator.language);
}

let currentLanguage: AppLanguage = detectLanguage();

interface I18nContextValue {
  language: AppLanguage;
  locale: string;
  setLanguage: (language: AppLanguage) => void;
  tr: (chinese: string, english: string) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function translateNow(chinese: string, english: string): string {
  return localizedText(currentLanguage, chinese, english);
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<AppLanguage>(detectLanguage);

  useEffect(() => {
    currentLanguage = language;
    try { window.localStorage.setItem(STORAGE_KEY, language); } catch { /* keep the in-memory preference */ }
    document.documentElement.lang = language;
  }, [language]);

  const setLanguage = useCallback((next: AppLanguage) => {
    currentLanguage = next;
    setLanguageState(next);
  }, []);
  const tr = useCallback((chinese: string, english: string) => localizedText(language, chinese, english), [language]);
  const value = useMemo<I18nContextValue>(() => ({
    language,
    locale: language === "en" ? "en-US" : "zh-CN",
    setLanguage,
    tr,
  }), [language, setLanguage, tr]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("I18nProvider is missing");
  return value;
}

export function LanguageSelector({ compact = false }: { compact?: boolean }) {
  const { language, setLanguage, tr } = useI18n();
  return (
    <label className={`language-selector${compact ? " compact" : ""}`} title={tr("切换语言", "Switch language")}>
      <Languages size={17} aria-hidden="true" />
      <span className="sr-only">{tr("语言", "Language")}</span>
      <select
        aria-label={tr("选择语言", "Select language")}
        value={language}
        onChange={(event) => setLanguage(event.target.value as AppLanguage)}
      >
        <option value="zh-CN">中文</option>
        <option value="en">English</option>
      </select>
    </label>
  );
}
