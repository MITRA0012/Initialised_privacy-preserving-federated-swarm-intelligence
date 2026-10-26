import { useNavigate } from "react-router-dom";

export default function Navbar({ role }) {
  const navigate = useNavigate();

  async function logout() {
    const token = localStorage.getItem("docragToken");
    if (token) {
      try {
        await fetch("http://127.0.0.1:8000/auth/logout", {
          method: "POST", headers: { Authorization: `Bearer ${token}` },
        });
      } catch { /* Clear the local session even when the API is offline. */ }
    }
    localStorage.removeItem("docragToken");
    localStorage.removeItem("docragUser");
    navigate("/");
  }

  return (
    <header className="navbar">
      <div className="brand">
        <div className="brand-icon">N</div>
        <span>Nova<span className="brand-light">Connect</span></span>
      </div>

      <div className="nav-right">
        <span className="role-badge">{role} Workspace</span>
        <button className="logout-btn" onClick={logout}>
          Log out
        </button>
      </div>
    </header>
  );
}
