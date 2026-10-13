import {
  cloneElement,
  createContext,
  isValidElement,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type HTMLAttributes,
  type MouseEvent as ReactMouseEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import { Check } from "lucide-react";
import type { PopoverAlign, PopoverSide } from "./Popover";
import { useAnchoredPanel } from "./anchor";

interface MenuProps {
  trigger: ReactElement;
  children: ReactNode;
  align?: PopoverAlign;
  side?: PopoverSide;
  width?: number;
  /** Accessible name of the menu. */
  label?: string;
  onOpenChange?: (open: boolean) => void;
}

const MenuCtx = createContext<() => void>(() => undefined);
const ITEM_SEL = '[role="menuitem"]:not(:disabled)';

/**
 * Dropdown anchored to a trigger: Escape/outside click/select closes it and
 * Arrow keys move between items. Panels are plain absolutely positioned divs.
 */
export function Menu({ trigger, children, align = "start", side = "bottom", width, label, onOpenChange }: MenuProps) {
  const [open, setOpenState] = useState(false);
  const rootRef = useRef<HTMLSpanElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const panel = useAnchoredPanel({ open, side, align, width, gap: 4, anchorRef: rootRef, panelRef });

  const setOpen = useCallback(
    (next: boolean) => {
      setOpenState(next);
      onOpenChange?.(next);
    },
    [onOpenChange],
  );
  const close = useCallback(() => setOpen(false), [setOpen]);

  useEffect(() => {
    if (!open) return;
    const items = () => [...(panelRef.current?.querySelectorAll<HTMLElement>(ITEM_SEL) ?? [])];
    items()[0]?.focus();

    const onPointer = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      const list = items();
      if (!list.length) return;
      const idx = list.indexOf(document.activeElement as HTMLElement);
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
        triggerRef.current?.focus();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        list[(idx + 1) % list.length]?.focus();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        list[(idx - 1 + list.length) % list.length]?.focus();
      } else if (e.key === "Home") {
        e.preventDefault();
        list[0]?.focus();
      } else if (e.key === "End") {
        e.preventDefault();
        list[list.length - 1]?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open, setOpen]);

  const triggerEl = isValidElement(trigger)
    ? cloneElement(trigger, {
        onClick: (e: ReactMouseEvent) => {
          (trigger.props as { onClick?: (e: ReactMouseEvent) => void }).onClick?.(e);
          setOpen(!open);
        },
        "aria-expanded": open,
        "aria-haspopup": "menu",
        ref: (el: HTMLElement | null) => {
          triggerRef.current = el;
        },
      } as HTMLAttributes<HTMLElement>)
    : trigger;

  return (
    <MenuCtx.Provider value={close}>
      <span ref={rootRef} className="menu">
        {triggerEl}
        {open ? (
          <div
            ref={panelRef}
            role="menu"
            aria-label={label}
            className="menu__panel"
            data-side={panel.side}
            style={panel.style}
          >
            {children}
          </div>
        ) : null}
      </span>
    </MenuCtx.Provider>
  );
}

export function MenuItem({
  icon,
  label,
  shortcut,
  onSelect,
  disabled,
  checked,
  danger,
}: {
  icon?: ReactNode;
  label: ReactNode;
  shortcut?: ReactNode;
  onSelect?: () => void;
  disabled?: boolean;
  /** Marks the item as the current selection (radio/checkbox semantics). */
  checked?: boolean;
  danger?: boolean;
}) {
  const close = useContext(MenuCtx);
  return (
    <button
      type="button"
      role="menuitem"
      className={`menu__item${danger ? " menu__item--danger" : ""}`}
      disabled={disabled}
      onClick={() => {
        onSelect?.();
        close();
      }}
    >
      {icon}
      <span className="menu__item-label truncate">{label}</span>
      {shortcut ? <span className="menu__shortcut">{shortcut}</span> : null}
      {checked ? <Check size={14} className="menu__item-check" /> : null}
    </button>
  );
}

export function MenuSeparator() {
  return <div className="menu__separator" role="separator" />;
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <div className="menu__label">{children}</div>;
}
