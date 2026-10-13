import { useNavigate } from "react-router-dom";
import { LogOut, Monitor, Moon, Settings, Sun } from "lucide-react";
import { Menu, MenuItem, MenuLabel, MenuSeparator } from "../ui/Menu";
import type { PopoverAlign, PopoverSide } from "../ui/Popover";
import { useI18n, type Locale } from "../i18n";
import { useAuth } from "../state/auth";
import { useTheme, type Theme } from "../theme";

const THEMES: { id: Theme; icon: typeof Sun }[] = [
  { id: "dark", icon: Moon },
  { id: "light", icon: Sun },
  { id: "system", icon: Monitor },
];
const LOCALES: { id: Locale; name: string }[] = [
  { id: "en", name: "English" },
  { id: "zh-CN", name: "简体中文" },
];

/** Workspace/user menu shared by the sidebar chevron and the footer avatar. */
export function UserMenu({
  trigger,
  align = "start",
  side = "bottom",
}: {
  trigger: React.ReactElement;
  align?: PopoverAlign;
  side?: PopoverSide;
}) {
  const { t, locale, setLocale } = useI18n();
  const { me, workspace, logout } = useAuth();
  const { theme, setTheme } = useTheme();
  const nav = useNavigate();

  return (
    <Menu trigger={trigger} align={align} side={side} width={220} label={t("shell.account")}>
      <MenuLabel>{workspace?.name ?? t("shell.workspace")}</MenuLabel>
      <MenuItem label={me?.user.email ?? ""} disabled />
      <MenuSeparator />
      <MenuLabel>{t("shell.theme")}</MenuLabel>
      {THEMES.map(({ id, icon: ThemeIcon }) => (
        <MenuItem
          key={id}
          icon={<ThemeIcon size={14} />}
          label={t(`theme.${id}` as const)}
          checked={theme === id}
          onSelect={() => setTheme(id)}
        />
      ))}
      <MenuLabel>{t("shell.language")}</MenuLabel>
      {LOCALES.map((l) => (
        <MenuItem key={l.id} label={l.name} checked={locale === l.id} onSelect={() => setLocale(l.id)} />
      ))}
      <MenuSeparator />
      <MenuItem icon={<Settings size={14} />} label={t("nav.settings")} onSelect={() => nav("/settings")} />
      <MenuItem icon={<LogOut size={14} />} label={t("nav.sign_out")} onSelect={() => void logout()} />
    </Menu>
  );
}
