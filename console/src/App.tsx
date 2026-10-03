import { hostedMode } from "./hosted/api";
import { AuthGate } from "./hosted/AuthGate";
import { PrototypeApp } from "./prototype/PrototypeApp";

export function App() {
  return hostedMode ? <AuthGate><PrototypeApp /></AuthGate> : <PrototypeApp />;
}
