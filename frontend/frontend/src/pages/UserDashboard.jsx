import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import Navbar from "../components/Navbar";

export default function UserDashboard() {
  const user = JSON.parse(localStorage.getItem("docragUser") || "{}");
  const historyKey = `docragHistory:${user.group_id || "group"}:${user.id || "user"}`;
  const [message, setMessage] = useState("");
  const [messages, setMessages] = useState(() => {
    try { const saved = JSON.parse(localStorage.getItem(historyKey) || "null"); if (Array.isArray(saved)) return saved; } catch {}
    return [{ id: 1, sender: "bot", text: `Welcome to ${user.group_name || "your group"}'s DocRAG assistant. Ask about the group's uploaded documents.` }];
  });
  const [mode, setMode] = useState("local");
  const [loading, setLoading] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const historyWrites = useRef(Promise.resolve());
  const navigate = useNavigate();
  useEffect(() => {
    const token = localStorage.getItem("docragToken") || "";
    fetch("http://127.0.0.1:8000/chat/history", { headers: { Authorization: `Bearer ${token}` } })
      .then(response => response.ok ? response.json() : null)
      .then(rows => { if (rows?.length) setMessages(rows.map(row => ({ id: row.id, sender: row.role === "assistant" ? "bot" : "user", text: row.content }))); setHistoryLoaded(true); })
      .catch(() => setHistoryLoaded(true));
  }, [historyKey]);
  useEffect(() => {
    if (!historyLoaded) return;
    localStorage.setItem(historyKey, JSON.stringify(messages));
    const savedMessages = messages.filter(item => item.id !== 1 || item.sender !== "bot").slice(-100).map(item => ({ role: item.sender === "bot" ? "assistant" : "user", content: item.text }));
    historyWrites.current = historyWrites.current.catch(() => {}).then(() => fetch("http://127.0.0.1:8000/chat/history", { method: "PUT", headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("docragToken") || ""}` }, body: JSON.stringify({ messages: savedMessages }) }));
  }, [historyKey, historyLoaded, messages]);

  async function sendMessage(event) {
    event.preventDefault(); const query = message.trim(); if (!query || loading) return;
    setMessages(current => [...current, { id: Date.now(), sender: "user", text: query }]); setMessage(""); setLoading(true);
    try {
      const response = await fetch("http://127.0.0.1:8000/query", { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("docragToken") || ""}` }, body: JSON.stringify({ query, n_results: 5, answer_style: "balanced", assistant_mode: mode }) });
      const data = await response.json(); if (!response.ok) throw new Error(data.detail || "Query failed.");
      setMessages(current => [...current, { id: Date.now() + 1, sender: "bot", text: data.answer }]);
    } catch (error) { setMessages(current => [...current, { id: Date.now() + 1, sender: "bot", text: error.message }]); }
    finally { setLoading(false); }
  }
  return <div className="app-shell"><Navbar role="User"/><main className="chat-page">
    <section className="chat-intro"><span className="eyebrow">{user.group_name || "GROUP"} DOC RAG</span><h1>Ask your group documents.</h1><p className="muted">Answers are grounded in files uploaded to this group's library.</p></section>
    <section className="chat-card"><header className="chat-header"><div className="chat-bot-avatar">✳</div><div className="chat-title"><h2>{mode === "local" ? "DocRAG assistant" : "Federated General bot"}</h2><span className="online-status"><span className="status-dot"/>{mode === "local" ? `${user.group_name || "Group"} cluster` : "Shared HF adapter · local context"}</span></div><div className="bot-toggle" role="group" aria-label="Assistant mode"><button type="button" className={mode === "local" ? "active" : ""} onClick={() => setMode("local")}>Local bot</button><button type="button" className={mode === "general" ? "active" : ""} onClick={() => setMode("general")}>General bot</button></div>{["host","subhost"].includes(user.role) && <button className="logout-btn" onClick={() => navigate("/host")}>Host tools</button>}</header>
      <div className="messages-area" aria-live="polite">{messages.map(item => <div key={item.id} className={`message-row ${item.sender === "user" ? "user-row" : ""}`}>{item.sender === "bot" && <div className="message-avatar">✳</div>}<div className={`message-bubble ${item.sender === "user" ? "user-bubble" : "bot-bubble"}`}>{item.text}</div></div>)}{loading && <div className="message-row"><div className="message-avatar">✳</div><div className="message-bubble bot-bubble typing">Searching group documents…</div></div>}</div>
      <form className="chat-composer" onSubmit={sendMessage}><input aria-label="Message" value={message} onChange={e => setMessage(e.target.value)} placeholder="Ask a question about the documents…" disabled={loading}/><button type="submit" className="send-btn" disabled={!message.trim() || loading}>Send ↑</button></form>
    </section></main></div>;
}
