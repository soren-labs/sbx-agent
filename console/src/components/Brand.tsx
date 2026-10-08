import { Mark } from "./icons";

export function Brand({ small = false }: { small?: boolean }) {
  return (
    <div className={`workspace-brand brand ${small ? "small" : ""}`}>
      <Mark small={small} />
      <span>SBX Agent</span>
    </div>
  );
}
