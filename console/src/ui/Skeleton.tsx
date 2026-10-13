export function Skeleton({ width, height = 14 }: { width?: number | string; height?: number | string }) {
  return <span className="skeleton" style={{ display: "block", width, height }} aria-hidden="true" />;
}
