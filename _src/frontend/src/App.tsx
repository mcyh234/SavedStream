import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, LoaderCircle, ServerCog } from "lucide-react";
import { api, errorMessage } from "./api";
import { AccountAuthGate, AccountStateGate, CenterShell } from "./AuthPanels";
import GalleryPage from "./GalleryPage";
import { MediaEncryptionGate, useMediaCrypto } from "./MediaCrypto";
import AdminPage from "./AdminPage";
import { useI18n } from "./I18n";
import { useTheme } from "./ThemeSelector";
import type { PublicStatus } from "./types";

export default function App() {
  useTheme();
  const { tr } = useI18n();
  const mediaCrypto = useMediaCrypto();
  const cryptoMode = mediaCrypto.mode;
  const resetMediaCrypto = mediaCrypto.reset;
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

  useEffect(() => {
    if (status && !status.media_authenticated && cryptoMode === "session") {
      void resetMediaCrypto(false);
    }
  }, [cryptoMode, resetMediaCrypto, status]);

  if (isAdminPage) {
    return <AdminPage onSessionChanged={refreshStatus} />;
  }

  if (!status && !error) {
    return (
      <main className="app-loading" aria-label={tr("正在加载", "Loading")}>
        <LoaderCircle className="spin" size={30} />
      </main>
    );
  }

  if (error && !status) {
    return (
      <CenterShell icon={<AlertTriangle size={30} />} title={tr("服务连接失败", "Service connection failed")}>
        <p className="gate-message" role="alert">{error}</p>
        <button className="button secondary wide" onClick={refreshStatus} type="button">{tr("重新连接", "Reconnect")}</button>
      </CenterShell>
    );
  }

  if (!status?.configuration_ok) {
    return (
      <CenterShell icon={<ServerCog size={30} />} title={tr("等待服务器配置", "Server configuration required")}>
        <p className="gate-message">
          {tr("请配置 TELEGRAM_API_ID、TELEGRAM_API_HASH 和 ADMIN_KEY 后重启容器。", "Configure TELEGRAM_API_ID, TELEGRAM_API_HASH, and ADMIN_KEY, then restart the containers.")}
        </p>
      </CenterShell>
    );
  }

  if (!status.media_authenticated) {
    if (status.access_status === "unauthenticated") {
      return (
        <AccountAuthGate
          registrationEnabled={status.registration_enabled}
          approvalRequired={status.registration_requires_approval}
          onAuthenticated={refreshStatus}
        />
      );
    }
    return (
      <AccountStateGate
        accessStatus={status.access_status}
        bindingSyncStatus={status.binding_sync_status}
        publicAlbumEnabled={status.public_album_enabled}
        approvalRequired={status.registration_requires_approval}
        onAuthenticated={refreshStatus}
      />
    );
  }

  if (!status.telegram_authenticated) {
    if (!status.admin_authenticated) return (
      <AccountStateGate
        accessStatus={status.access_status}
        bindingSyncStatus={status.binding_sync_status}
        publicAlbumEnabled={status.public_album_enabled}
        approvalRequired={status.registration_requires_approval}
        serviceUnavailable
        onAuthenticated={refreshStatus}
      />
    );
    return (
      <CenterShell icon={<ServerCog size={30} />} title={tr("尚未连接托管账号", "No managed account connected")}>
        <p className="gate-message">{tr("请打开管理页，在“多账号协调”中填写 Telegram API ID、API Hash，然后扫码连接账号。", "Open the admin page, configure the Telegram API credentials under multi-account coordination, then connect an account by QR code.")}</p>
        <a className="button primary wide" href="/admin">{tr("打开管理页配置", "Open admin settings")}</a>
      </CenterShell>
    );
  }

  return (
    <MediaEncryptionGate
      mode={status.admin_authenticated ? "persistent" : "session"}
      sessionId={status.media_session_id || ""}
    >
      <GalleryPage isAdmin={status.admin_authenticated} />
    </MediaEncryptionGate>
  );
}
