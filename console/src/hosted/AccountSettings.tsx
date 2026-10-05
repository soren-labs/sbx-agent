import { useEffect, useState } from "react";
import { hostedRequest } from "./api";
import { Icon } from "../prototype/Icon";

export function AccountSettings() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {void hostedRequest("/auth/me").then(data => setEmail(data.user.email)).catch(e => setError(e.message));}, []);
  const logout = async () => {
    try {
      await hostedRequest("/auth/logout", {});
      window.location.assign("/");
    } catch(e) {setError((e as Error).message);}
  };
  return <section className="settings-section hs-card" aria-label="Your account">
    <h2 className="hs-section-title">Your account</h2>
    <div className="hs-account">
      <span className="user-avatar avatar hs-avatar-lg">{email ? email[0].toUpperCase() : <Icon name="user" size={16} />}</span>
      <div><strong>{email.split("@")[0] || "Signed in"}</strong><p>{email}</p></div>
      <button className="button" onClick={() => void logout()}><Icon name="logout" size={13} />Sign out</button>
    </div>
    {error && <p className="hs-alert" role="alert">{error}</p>}
  </section>;
}
