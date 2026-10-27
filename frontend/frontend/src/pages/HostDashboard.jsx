import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Navbar from "../components/Navbar";

const API = "http://127.0.0.1:8000";
const headers = () => ({ Authorization: `Bearer ${localStorage.getItem("docragToken") || ""}` });

export default function HostDashboard() {
  const user = JSON.parse(localStorage.getItem("docragUser") || "{}");
  const [files, setFiles] = useState([]), [members, setMembers] = useState([]);
  const [instructions, setInstructions] = useState("");
  const [federation, setFederation] = useState({ adapter_dir: "", inference_port: 11435 });
  const [federationStatus, setFederationStatus] = useState(null);
  const [notice, setNotice] = useState(""), [member, setMember] = useState({ full_name: "", phone: "", unique_id: "", role: "user" });
  const navigate = useNavigate();

  async function refresh() {
    try {
      const [docs, people, settings, federated] = await Promise.all([
        fetch(`${API}/documents`, { headers: headers() }), fetch(`${API}/auth/members`, { headers: headers() }), fetch(`${API}/cluster/settings`, { headers: headers() }), fetch(`${API}/federated/status`, { headers: headers() }),
      ]);
      if (!docs.ok || !people.ok || !settings.ok || !federated.ok) throw new Error("Session expired. Sign in again.");
      setFiles(await docs.json()); setMembers(await people.json()); setInstructions((await settings.json()).instructions || "");
      const runtime = await federated.json(); setFederationStatus(runtime); setFederation(runtime.config);
    } catch (error) { setNotice(error.message); }
  }
  async function saveInstructions(event) {
    event.preventDefault();
    const response = await fetch(`${API}/cluster/settings`, { method: "PUT", headers: { ...headers(), "Content-Type": "application/json" }, body: JSON.stringify({ instructions }) });
    const data = await response.json(); setNotice(response.ok ? "Instructions saved to this group cluster." : data.detail || "Could not save instructions.");
  }
  async function saveFederation(event) {
    event.preventDefault();
    const response = await fetch(`${API}/federated/config`, { method: "PUT", headers: { ...headers(), "Content-Type": "application/json" }, body: JSON.stringify({ ...federation, inference_port: Number(federation.inference_port) }) });
    const data = await response.json(); setNotice(response.ok ? "Adapter folder saved for this group cluster." : data.detail || "Could not save settings.");
    if (response.ok) refresh();
  }
  async function setFederationRuntime(action) {
    const response = await fetch(`${API}/federated/${action}`, { method: "POST", headers: headers() });
    const data = await response.json(); setNotice(response.ok ? data.message || `Local inference ${action === "start" ? "started" : "stopped"}.` : data.detail || "Could not control local inference.");
    refresh();
  }
  useEffect(() => { refresh(); }, []);

  async function uploadFiles(event) {
    const chosen = Array.from(event.target.files || []);
    for (const file of chosen) {
      const body = new FormData(); body.append("file", file);
      const response = await fetch(`${API}/upload`, { method: "POST", headers: headers(), body });
      const data = await response.json();
      if (!response.ok) setNotice(data.detail || "Upload failed.");
      else setNotice(`${file.name} uploaded${data.indexed ? " and indexed" : ", but could not be indexed"}.`);
    }
    event.target.value = ""; refresh();
  }
  async function removeFile(id) {
    const response = await fetch(`${API}/documents/${id}`, { method: "DELETE", headers: headers() });
    if (!response.ok) setNotice("Could not remove this document."); else setNotice("Document removed from the group library.");
    refresh();
  }
  async function addMember(event) {
    event.preventDefault();
    const response = await fetch(`${API}/auth/members`, { method: "POST", headers: { ...headers(), "Content-Type": "application/json" }, body: JSON.stringify(member) });
    const data = await response.json();
    if (!response.ok) { setNotice(data.detail || "Could not add member."); return; }
    setNotice(`${member.full_name} added. Their unique ID is ${data.unique_id}; share it securely.`);
    setMember({ full_name: "", phone: "", unique_id: "", role: "user" }); refresh();
  }
  async function promote(id) {
    const response = await fetch(`${API}/auth/members/${id}/promote`, { method: "PATCH", headers: headers() });
    const data = await response.json(); setNotice(response.ok ? "Member now has subhost access. Their uploads remain in this host's group library." : data.detail);
    refresh();
  }
  async function removeMember(id) {
    const response = await fetch(`${API}/auth/members/${id}`, { method: "DELETE", headers: headers() });
    const data = await response.json(); setNotice(response.ok ? "Member removed from this group." : data.detail);
    refresh();
  }

  return <div className="app-shell"><Navbar role={user.role === "subhost" ? "Subhost" : "Host"} />
    <main className="dashboard">
      <section className="page-heading"><div><span className="eyebrow">HOST CONTROL CENTER</span><h1>{user.group_name || "Workspace"}</h1><p className="muted">Manage the shared DocRAG library and group access.</p></div><button className="secondary-btn" type="button" onClick={() => { window.location.href = "kin-agent://open"; }}>Open Kin Agent ↗</button><div className="avatar">{(user.full_name || "H").slice(0,1)}</div></section>
      {notice && <div className="notice" role="status">{notice}<button onClick={() => setNotice("")} aria-label="Dismiss">×</button></div>}
      <section className="stats-grid"><article className="stat-card"><p>Group documents</p><h2>{files.length}</h2><span className="muted small-text">Shared with group members</span></article><article className="stat-card"><p>Group members</p><h2>{members.length + 1}</h2><span className="muted small-text">Unique IDs are checked by the database</span></article><article className="stat-card"><p>Workspace</p><h2 className="status-heading">Active</h2><span className="muted small-text">{user.group_name}</span></article></section>
      <section className="host-grid">
        <article className="panel"><div className="panel-heading"><div><h2>DocRAG library</h2><p className="muted">Files uploaded by hosts and subhosts are indexed for this group.</p></div></div>
          <label className="upload-zone" htmlFor="fileUpload"><strong>Upload documents</strong><span className="muted small-text">PDF or DOCX</span><span className="upload-action">Browse files</span></label>
          <input id="fileUpload" type="file" accept=".pdf,.docx" multiple className="visually-hidden" onChange={uploadFiles} />
          <div className="section-divider"/><h3>Group documents ({files.length})</h3>
          {files.length === 0 ? <p className="empty-state">No documents uploaded yet.</p> : <div className="item-list">{files.map(file => <div className="file-row" key={file.id}><div className="file-info"><strong>{file.filename}</strong><p className="muted small-text">{file.index_status || "stored"} · {(file.file_size / 1024).toFixed(1)} KB</p></div><button className="delete-btn" onClick={() => removeFile(file.id)}>Remove</button></div>)}</div>}
        </article>
        <article className="panel"><div className="panel-heading"><div><h2>Add group member</h2><p className="muted">Each unique ID can only be assigned once across all groups.</p></div></div>
          <form onSubmit={addMember}><label htmlFor="memberName">Full name</label><input id="memberName" value={member.full_name} onChange={e => setMember({...member,full_name:e.target.value})} required />
            <label htmlFor="memberPhone">Phone number</label><input id="memberPhone" value={member.phone} onChange={e => setMember({...member,phone:e.target.value})} required />
            <label htmlFor="memberId">Unique ID</label><input id="memberId" value={member.unique_id} onChange={e => setMember({...member,unique_id:e.target.value})} required />
            <label htmlFor="memberRole">Access</label><select id="memberRole" value={member.role} onChange={e => setMember({...member,role:e.target.value})}><option value="user">User</option><option value="subhost">Subhost</option></select>
            <button className="primary-btn full-btn" type="submit">Add member</button></form>
          <div className="section-divider"/><h3>Members</h3>{members.map(person => <div className="list-row" key={person.id}><div><strong>{person.full_name}</strong><p className="muted small-text">{person.role}</p></div><div>{person.role === "user" && user.role === "host" && <button className="text-danger" onClick={() => promote(person.id)}>Make subhost</button>} {user.role === "host" && <button className="text-danger" onClick={() => removeMember(person.id)}>Remove</button>}</div></div>)}
        </article>
      </section>
      <article className="panel cluster-instructions"><div className="panel-heading"><div><h2>Cluster assistant instructions</h2><p className="muted">Saved in this DocRAG group’s database cluster and used for this group’s answers. Kin settings stay on Kin.</p></div></div><form onSubmit={saveInstructions}><textarea aria-label="Cluster assistant instructions" rows={4} maxLength={4000} value={instructions} onChange={event => setInstructions(event.target.value)} placeholder="Set response style and group specific guidance…"/><button className="primary-btn" type="submit">Save instructions</button></form></article>
      <article className="panel cluster-instructions"><div className="panel-heading"><div><h2>Federated General bot (use only)</h2><p className="muted">This group only uses the shared adapter for its General bot. Its documents are never used for training and this group does not join the federation. The General bot sends the question and this group's retrieved excerpts only to the inference service on this machine.</p></div></div>
        <form onSubmit={saveFederation} className="federation-config"><label>Aggregated adapter folder (copied from the coordinator)<input value={federation.adapter_dir} onChange={e => setFederation({...federation, adapter_dir:e.target.value})} placeholder="C:\\DocRAG\\federated-adapters" required /></label>
          <label>Local inference port<input type="number" min="1024" max="65535" value={federation.inference_port} onChange={e => setFederation({...federation, inference_port:Number(e.target.value)})} required /></label>
          <button className="primary-btn" type="submit">Save settings</button></form>
        {federationStatus && <p className="muted small-text">Missing packages: {federationStatus.missing_packages.join(", ") || "none"} · inference service: {Object.values(federationStatus.processes).includes("running") ? "running" : "stopped"} · aggregate: {federationStatus.inference?.ready ? `ready (round ${federationStatus.inference.round})` : "not received yet"}</p>}
        <div className="form-actions"><button className="primary-btn" type="button" onClick={() => setFederationRuntime("start")}>Start local inference</button><button className="secondary-btn" type="button" onClick={() => setFederationRuntime("stop")}>Stop inference</button></div>
      </article>
      <button className="text-danger" onClick={() => navigate("/user")}>Open user chat</button>
    </main></div>;
}
