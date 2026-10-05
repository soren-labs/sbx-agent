import { UnifiedApp } from "./unified/UnifiedApp";

/**
 * One console, one state model (RFC 167 §08): the unified app replaces the
 * hosted/prototype split. Legacy surfaces are deleted in Phase 6.
 */
export function App() {
  return <UnifiedApp />;
}
