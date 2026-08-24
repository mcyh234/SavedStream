import { useEffect, useState } from "react";
import { Palette } from "lucide-react";
import { useI18n } from "./I18n";

export type ThemeName = "system" | "midnight" | "graphite" | "paper" | "warm";

const STORAGE_KEY = "savedstream-theme";

export const THEME_OPTIONS: Array<{ value: ThemeName; zh: string; en: string }> = [
  { value: "system", zh: "跟随系统", en: "System" },
  { value: "midnight", zh: "午夜深色", en: "Midnight" },
  { value: "graphite", zh: "石墨深色", en: "Graphite" },
  { value: "paper", zh: "纸张浅色", en: "Paper" },
  { value: "warm", zh: "暖沙浅色", en: "Warm sand" },
];

function readTheme(): ThemeName {
  if (typeof window === "undefined") return "midnight";
  const value = window.localStorage.getItem(STORAGE_KEY);
  return THEME_OPTIONS.some((option) => option.value === value) ? value as ThemeName : "system";
}

function resolvedTheme(theme: ThemeName): Exclude<ThemeName, "system"> {
  if (theme !== "system") return theme;
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "paper" : "midnight";
}

export function applyTheme(theme: ThemeName) {
  if (typeof document === "undefined") return;
  const resolved = resolvedTheme(theme);
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.colorScheme = resolved === "paper" || resolved === "warm" ? "light" : "dark";
}

export function useTheme() {
  const [theme, setThemeState] = useState<ThemeName>(readTheme);

  useEffect(() => {
    applyTheme(theme);
    window.localStorage.setItem(STORAGE_KEY, theme);
  }, [theme]);

  useEffect(() => {
    if (theme !== "system") return;
    const media = window.matchMedia?.("(prefers-color-scheme: light)");
    if (!media) return;
    const onChange = () => applyTheme("system");
    media.addEventListener?.("change", onChange);
    return () => media.removeEventListener?.("change", onChange);
  }, [theme]);

  return {
    theme,
    setTheme(next: ThemeName) {
      setThemeState(next);
    },
  };
}

export default function ThemeSelector() {
  const { theme, setTheme } = useTheme();
  const { tr } = useI18n();
  return (
    <label className="theme-selector" title={tr("选择主题", "Select theme")}>
      <Palette size={17} aria-hidden="true" />
      <span className="sr-only">{tr("主题", "Theme")}</span>
      <select value={theme} onChange={(event) => setTheme(event.target.value as ThemeName)} aria-label={tr("选择主题", "Select theme")}>
        {THEME_OPTIONS.map((option) => <option key={option.value} value={option.value}>{tr(option.zh, option.en)}</option>)}
      </select>
    </label>
  );
}
