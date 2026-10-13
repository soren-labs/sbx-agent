import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Spinner } from "./Spinner";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: ReactNode;
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", icon, loading, className, children, disabled, type, ...rest },
  ref,
) {
  const cls = `btn btn--${variant}${size === "sm" ? " btn--sm" : ""}${className ? ` ${className}` : ""}`;
  return (
    <button ref={ref} type={type ?? "button"} className={cls} disabled={disabled || loading} {...rest}>
      {loading ? <Spinner small /> : icon}
      {children}
    </button>
  );
});

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Accessible name; also used as the tooltip. */
  label: string;
  icon: ReactNode;
  size?: "sm" | "md";
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, size = "md", className, type, ...rest },
  ref,
) {
  const cls = `icon-btn${size === "sm" ? " icon-btn--sm" : ""}${className ? ` ${className}` : ""}`;
  return (
    <button ref={ref} type={type ?? "button"} className={cls} aria-label={label} title={label} {...rest}>
      {icon}
    </button>
  );
});
