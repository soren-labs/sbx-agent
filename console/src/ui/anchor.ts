import { useLayoutEffect, useState, type CSSProperties, type RefObject } from "react";
import type { PopoverAlign, PopoverSide } from "./Popover";

const VIEW_PAD = 8;

export interface AnchoredPanelLayout {
  style: CSSProperties;
  /** The side actually used after flip logic — equals the requested side when it fits. */
  side: PopoverSide;
}

/**
 * Positions an absolutely anchored panel inside the viewport: flips to the
 * roomier side when the requested side cannot fit the panel, clamps
 * max-height to the available space, and clamps the horizontal position so
 * the panel never exceeds `100vw - 16px`. Re-measures on window resize and
 * panel size changes while open.
 */
export function useAnchoredPanel({
  open,
  side,
  align,
  width,
  gap,
  anchorRef,
  panelRef,
}: {
  open: boolean;
  side: PopoverSide;
  align: PopoverAlign;
  width?: number;
  gap: number;
  anchorRef: RefObject<HTMLElement | null>;
  panelRef: RefObject<HTMLElement | null>;
}): AnchoredPanelLayout {
  const [layout, setLayout] = useState<AnchoredPanelLayout>({ style: {}, side });

  useLayoutEffect(() => {
    if (!open) return;
    let last = "";
    const measure = () => {
      const anchor = anchorRef.current;
      const panel = panelRef.current;
      if (!anchor || !panel) return;
      const rect = anchor.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const w = Math.min(width ?? panel.offsetWidth, Math.max(0, vw - 2 * VIEW_PAD));
      const h = panel.offsetHeight || 0;
      const above = rect.top;
      const below = vh - rect.bottom;
      let chosen: PopoverSide;
      if (side === "top") {
        chosen = h + gap + VIEW_PAD <= above || above >= below ? "top" : "bottom";
      } else {
        chosen = h + gap + VIEW_PAD <= below || below >= above ? "bottom" : "top";
      }
      const space = Math.max(0, (chosen === "top" ? above : below) - gap - VIEW_PAD);
      const left = Math.min(
        Math.max(align === "start" ? rect.left : rect.right - w, VIEW_PAD),
        Math.max(VIEW_PAD, vw - VIEW_PAD - w),
      );
      const style: CSSProperties = {
        width: w,
        maxHeight: space,
        overflowY: "auto",
        left: left - rect.left,
        right: "auto",
        top: chosen === "bottom" ? `calc(100% + ${gap}px)` : "auto",
        bottom: chosen === "top" ? `calc(100% + ${gap}px)` : "auto",
      };
      const key = `${chosen}|${w}|${space}|${left.toFixed(1)}`;
      if (key !== last) {
        last = key;
        setLayout({ style, side: chosen });
      }
    };
    measure();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    if (panelRef.current) ro?.observe(panelRef.current);
    window.addEventListener("resize", measure);
    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [open, side, align, width, gap, anchorRef, panelRef]);

  return layout;
}
