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
import { useAnchoredPanel } from "./anchor";

export type PopoverAlign = "start" | "end";
export type PopoverSide = "top" | "bottom";

interface PopoverCtxValue {
  open: boolean;
  setOpen: (open: boolean) => void;
  triggerRef: React.RefObject<HTMLElement | null>;
}

const PopoverCtx = createContext<PopoverCtxValue | null>(null);

export function usePopover() {
  const c = useContext(PopoverCtx);
  if (!c) throw new Error("Popover context missing");
  return c;
}

/**
 * Anchors an absolutely positioned panel to its trigger. Closes on outside
 * pointer-down and Escape. The trigger element gets onClick + aria-expanded.
 * Pass `open` + `onOpenChange` for controlled usage.
 */
export function Popover({
  trigger,
  children,
  align = "start",
  side = "bottom",
  width,
  className,
  open: controlled,
  onOpenChange,
}: {
  trigger: ReactElement;
  children: ReactNode;
  align?: PopoverAlign;
  side?: PopoverSide;
  width?: number;
  className?: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const [innerOpen, setInnerOpen] = useState(false);
  const open = controlled ?? innerOpen;
  const rootRef = useRef<HTMLSpanElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const panel = useAnchoredPanel({ open, side, align, width, gap: 6, anchorRef: rootRef, panelRef });

  const setOpen = useCallback(
    (next: boolean) => {
      setInnerOpen(next);
      onOpenChange?.(next);
    },
    [onOpenChange],
  );

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
        triggerRef.current?.focus();
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
        "aria-haspopup": true,
        ref: (el: HTMLElement | null) => {
          triggerRef.current = el;
        },
      } as HTMLAttributes<HTMLElement>)
    : trigger;

  return (
    <PopoverCtx.Provider value={{ open, setOpen, triggerRef }}>
      <span ref={rootRef} className="menu">
        {triggerEl}
        {open ? (
          <div
            ref={panelRef}
            className={`popover__panel${className ? ` ${className}` : ""}`}
            data-side={panel.side}
            style={panel.style}
          >
            {children}
          </div>
        ) : null}
      </span>
    </PopoverCtx.Provider>
  );
}
