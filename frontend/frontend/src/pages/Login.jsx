import { useState } from "react";
import { useNavigate } from "react-router-dom";

export default function Login() {
  const [role, setRole] = useState("user");
  const [hostAction, setHostAction] = useState("signin");
  const [form, setForm] = useState({
    name: "",
    phone: "",
    groupName: "",
    uniqueId: "",
  });
  const [error, setError] = useState("");
  const navigate = useNavigate();
  const needsContactDetails = role === "user" || hostAction === "signup";

  function handleChange(event) {
    setForm({
      ...form,
      [event.target.name]: event.target.value,
    });
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");

    if (!form.groupName.trim() || !form.uniqueId.trim() ||
        (needsContactDetails && (!form.name.trim() || !form.phone.trim()))) {
      setError("Please complete the required fields.");
      return;
    }

    if (needsContactDetails && !/^[0-9+\-\s()]{7,20}$/.test(form.phone)) {
      setError("Please enter a valid phone number.");
      return;
    }

    try {
      const response = await fetch("http://127.0.0.1:8000/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ full_name: form.name || "Host", phone: form.phone || "0000000", group_name: form.groupName, unique_id: form.uniqueId, role, action: role === "host" ? hostAction : "signin" }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Unable to sign in.");
      localStorage.setItem("docragToken", data.token);
      localStorage.setItem("docragUser", JSON.stringify(data.user));
      navigate(role === "host" ? "/host" : "/user");
    } catch (err) { setError(err.message || "DocRAG API is unavailable."); }
  }

  return (
    <main className="login-page">
      <section className="login-visual">
        <div className="visual-content">
          <div className="brand login-brand">
            <div className="brand-icon">N</div>
            <span>Nova<span className="brand-light">Connect</span></span>
          </div>

          <div className="visual-copy">
            <span className="eyebrow">ONE WORKSPACE. SMARTER CONNECTIONS.</span>
            <h1>Communication,<br />reimagined.</h1>
            <p>
              A unified workspace for secure interactions,
              intelligent conversations and effortless control.
            </p>
          </div>

          <div className="visual-footer">
            <span className="status-dot" />
            Your workspace starts here
          </div>
        </div>
      </section>

      <section className="login-form-section">
        <form className="login-card" onSubmit={handleSubmit}>
          <span className="eyebrow">WELCOME ABOARD</span>
          <h2>Get started</h2>
          <p className="muted">
            Choose your workspace and enter your details.
          </p>

          <div className="role-switch">
            <button
              type="button"
              className={role === "user" ? "role-option active" : "role-option"}
              onClick={() => setRole("user")}
            >
              <span>◉</span> User
            </button>

            <button
              type="button"
              className={role === "host" ? "role-option active" : "role-option"}
              onClick={() => setRole("host")}
            >
              <span>▦</span> Host
            </button>
          </div>

          {role === "host" && <div className="role-switch" aria-label="Host account action">
            <button type="button" className={hostAction === "signin" ? "role-option active" : "role-option"} onClick={() => { setHostAction("signin"); setError(""); }}>Sign In</button>
            <button type="button" className={hostAction === "signup" ? "role-option active" : "role-option"} onClick={() => { setHostAction("signup"); setError(""); }}>Sign Up</button>
          </div>}

          <label htmlFor="groupName">Group name</label>
          <input id="groupName" name="groupName" placeholder="Enter your group name" value={form.groupName} onChange={handleChange} required />

          {needsContactDetails && <><label htmlFor="name">Full name</label><input
            id="name"
            name="name"
            placeholder="Enter your full name"
            value={form.name}
            onChange={handleChange}
            autoComplete="name"
            required
          /></>}

          {needsContactDetails && <><label htmlFor="phone">Phone number</label><input
            id="phone"
            name="phone"
            placeholder="Enter phone number"
            value={form.phone}
            onChange={handleChange}
            autoComplete="tel"
            required
          /></>}

          <label htmlFor="uniqueId">Unique ID</label>
          <input
            id="uniqueId"
            name="uniqueId"
            placeholder="Enter your assigned ID"
            value={form.uniqueId}
            onChange={handleChange}
            required
          />

          {error && <p className="error-message">{error}</p>}

          <button className="primary-btn full-btn" type="submit">
            {role === "host" ? (hostAction === "signup" ? "Create Host Account" : "Sign In as Host") : "Sign In as User"}
            <span>→</span>
          </button>

          <button className="secondary-btn full-btn" type="button" onClick={() => { window.location.href = "kin-agent://open"; }}>
            Open Kin Agent <span>↗</span>
          </button>

          <p className="form-note">
            {role === "host" && hostAction === "signup" ? "Sign up creates a new group and its owner account." : "Sign in with your group name and assigned unique ID."}
          </p>
        </form>
      </section>
    </main>
  );
}
