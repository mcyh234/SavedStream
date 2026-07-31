import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, LoaderCircle, ServerCog } from "lucide-react";
import { api, errorMessage } from "./api";
import { AdminKeyGate, CenterShell, ViewerGate } from "./AuthPanels";
import GalleryPage from "./GalleryPage";
import AdminPage from "./AdminPage";
import type { PublicStatus } from "./types";

export default function App() {
  const isAdminPage = window.location.pathname === "/admin" || window.location.pathname.startsWith("/admin/");
  const [status, setStatus] = useState<PublicStatus | null>(null);
  const [error, setError] = useState("");

  const refreshStatus = useCallback(async () => {
    try {
      const next = await api<PublicStatus>("/api/status");
      setStatus(next);
      setError("");
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }, []);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  useEffect(() => {
    if (isAdminPage) return;
    const timer = window.setInterval(refreshStatus, 20_000);
    return () => window.clearInterval(timer);
  }, [isAdminPage, refreshStatus]);

  if (isAdminPage) {
    return <AdminPage onSessionChanged={refreshStatus} />;
  }

  if (!status && !error) {
    return (
      <main className="app-loading" aria-label="正在加载">
        <LoaderCircle className="spin" size={30} />
      </main>
    );
  }

  if (error && !status) {
    return (
      <CenterShell icon={<AlertTriangle size={30} />} title="服务连接失败">
        <p className="gate-message" role="alert">{error}</p>
        <button className="button secondary wide" onClick={refreshStatus} type="button">重新连接</button>
      </CenterShell>
    );
  }

  if (!status?.configuration_ok) {
    return (
      <CenterShell icon={<ServerCog size={30} />} title="等待服务器配置">
        <p className="gate-message">
          请配置 TELEGRAM_API_ID、TELEGRAM_API_HASH 和 ADMIN_KEY 后重启容器。
        </p>
      </CenterShell>
    );
  }

  if (status.access_restricted && !status.viewer_authenticated) {
    return <ViewerGate onAuthenticated={refreshStatus} />;
  }

  if (!status.telegram_authenticated) {
    if (!status.admin_authenticated) {
      return <AdminKeyGate onAuthenticated={refreshStatus} />;
    }
    return (
      <CenterShell icon={<ServerCog size={30} />} title="尚未连接托管账号">
        <p className="gate-message">请打开管理页，在“多账号协调”中填写 Telegram API ID、API Hash，然后扫码连接账号。</p>
        <a className="button primary wide" href="/admin">打开管理页配置</a>
      </CenterShell>
    );
  }

  return <GalleryPage />;
}
