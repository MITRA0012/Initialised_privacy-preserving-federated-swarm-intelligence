import { Routes, Route, Navigate } from "react-router-dom";
import Login from "./pages/Login";
import HostDashboard from "./pages/HostDashboard";
import UserDashboard from "./pages/UserDashboard";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Login />} />
      <Route path="/host" element={localStorage.getItem("docragToken") ? <HostDashboard /> : <Navigate to="/" replace />} />
      <Route path="/user" element={localStorage.getItem("docragToken") ? <UserDashboard /> : <Navigate to="/" replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
