const paths: Record<string, string> = {
  plus: "M12 5v14M5 12h14",
  arrow: "M12 19V5m-5 5 5-5 5 5",
  chevron: "m9 5 7 7-7 7",
  down: "m6 9 6 6 6-6",
  sessions: "M5 3h14v18H5zM8 7h8M8 11h8M8 15h5",
  search: "M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  settings: "M4 7h16M4 17h16M8 4v6M16 14v6",
  plug: "M9 3v4m6-4v4M6 7h12v4a6 6 0 0 1-12 0zM12 17v4",
  check: "m5 12 4 4L19 6",
  clock: "M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
  code: "m8 6-6 6 6 6m8-12 6 6-6 6m-3-14-2 16",
  terminal: "m5 7 5 5-5 5m8 0h6",
  branch:
    "M6 6v12m0-6h8a4 4 0 0 0 4-4M9 3a3 3 0 1 1-6 0 3 3 0 0 1 6 0M9 21a3 3 0 1 1-6 0 3 3 0 0 1 6 0M21 5a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  pr: "M6 8v8M9 5a3 3 0 1 1-6 0 3 3 0 0 1 6 0M9 19a3 3 0 1 1-6 0 3 3 0 0 1 6 0m6-15h3a3 3 0 0 1 3 3v9m-6-12 3-3m-3 3 3 3M21 19a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  sun: "M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1",
  file: "M5 3h9l5 5v13H5zM14 3v6h5",
  globe:
    "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M3 12h18M12 3a20 20 0 0 1 0 18 20 20 0 0 1 0-18",
  external: "M14 3h7v7m0-7-11 11M10 3H3v18h18v-7",
  x: "m6 6 12 12M6 18 18 6",
  stop: "M6 6h12v12H6z",
  refresh: "M20 7a9 9 0 1 0 1 8M20 2v6h-6",
  pin: "m9 3 6 0-1 6 4 5H6l4-5-1-6m3 11v7",
  moon: "M21 13a9 9 0 1 1-10-10 7 7 0 0 0 10 10",
  menu: "M4 6h16M4 12h16M4 18h16",
  more: "M4 12h.01M12 12h.01M20 12h.01",
  shield: "m12 2 9 4v6c0 6-9 10-9 10S3 18 3 12V6zM8 12l3 3 5-6",
  github:
    "M9 19c-5 1-5-3-7-3m14 6v-4c0-1-.4-2-1-3 4 0 6-2 6-6 0-2-1-3-2-4 0-1 0-3-1-4l-4 2H9L5 1c-1 1-1 3-1 4-1 1-2 2-2 4 0 4 2 6 6 6-1 1-1 2-1 3v4",
};
export function Icon({
  name,
  size = 16,
  className = "",
}: {
  name: string;
  size?: number;
  className?: string;
}) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name] ?? paths.code} />
    </svg>
  );
}
export function Mark({ small = false }: { small?: boolean }) {
  return (
    <span className={`sbx-mark ${small ? "small" : ""}`} aria-hidden="true">
      <svg viewBox="0 0 32 32" fill="none">
        <path
          d="m16 3 12 7v13l-12 7L4 23V10l12-7Z"
          stroke="currentColor"
          strokeWidth="2"
        />
        <path
          d="m4 10 12 7 12-7M16 17v13m-6-23 12 7"
          stroke="currentColor"
          strokeWidth="2"
        />
      </svg>
    </span>
  );
}
