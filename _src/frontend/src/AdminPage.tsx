import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  Activity,
  Archive,
  ArrowLeft,
  ArrowDownToLine,
  Bot,
  Check,
  ChevronDown,
  CircleAlert,
  Copy,
  Database,
  EyeOff,
  Gauge,
  Globe2,
  HardDrive,
  KeyRound,
  LoaderCircle,
  LogOut,
  Mail,
  Pencil,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Send,
  Server,
  Shield,
  Trash2,
  Upload,
  UserPlus,
  Users,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import QRCode from "qrcode";
import { ApiError, api, errorMessage } from "./api";
import { AdminKeyGate, CenterShell, TelegramLogin } from "./AuthPanels";
import { FileKindIcon, ThumbnailImage, formatBytes } from "./GalleryPage";
import { MediaEncryptionGate, useMediaCrypto } from "./MediaCrypto";
import { LanguageSelector, translateNow, useI18n } from "./I18n";
import type {
  AdminSettings,
  BackupEntry,
  BackupListResponse,
  MediaItem,
  MediaPage,
  MediaVisibility,
  NotificationItem,
  StorageAlert,
  StorageRecommendation,
  StorageSnapshot,
  UploadJob,
  PublicStatus,
  TelegramAuthStatus,
  TrafficSeriesPoint,
  TrafficSummary,
  HelperRateLimit,
} from "./types";
import ThemeSelector from "./ThemeSelector";

type AdminPhase = "checking" | "guest" | "ready" | "error";

type AdminTab = "dashboard" | "telegram" | "review" | "users" | "album" | "media" | "upload" | "traffic" | "cache" | "rate" | "mailbox" | "backups" | "storage";

const ADMIN_TABS: Array<{ id: AdminTab; zh: string; en: string; icon: typeof Gauge }> = [
  { id: "dashboard", zh: "仪表盘", en: "Dashboard", icon: Gauge },
  { id: "telegram", zh: "Telegram 与多账号", en: "Telegram & accounts", icon: Wifi },
  { id: "review", zh: "审核队列", en: "Review queue", icon: Shield },
  { id: "users", zh: "用户管理", en: "Users", icon: Users },
  { id: "album", zh: "公开相册", en: "Public album", icon: Globe2 },
  { id: "media", zh: "媒体库", en: "Media library", icon: Database },
  { id: "upload", zh: "上传", en: "Uploads", icon: Upload },
  { id: "traffic", zh: "流量限额", en: "Traffic", icon: Activity },
  { id: "cache", zh: "本地缓存", en: "Cache", icon: HardDrive },
  { id: "rate", zh: "Bot 限流", en: "Bot limits", icon: Bot },
  { id: "mailbox", zh: "站内信", en: "Mailbox", icon: Mail },
  { id: "backups", zh: "备份管理", en: "Backups", icon: Archive },
  { id: "storage", zh: "存储", en: "Storage", icon: Server },
];

const MEDIA_PAGE_SIZE = 24;
const MIN_CACHE_GB = 0.5;
const MAX_CACHE_GB = 200;

interface AdminPageProps {
  onSessionChanged?: () => void | Promise<void>;
}

export default function AdminPage({ onSessionChanged }: AdminPageProps) {
  const mediaCrypto = useMediaCrypto();
  const { tr } = useI18n();
  const [phase, setPhase] = useState<AdminPhase>("checking");
  const [bootstrapError, setBootstrapError] = useState("");
  const [settings, setSettings] = useState<AdminSettings | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [telegramLoginOpen, setTelegramLoginOpen] = useState(false);

  const [settingsError, setSettingsError] = useState("");
  const [tab, setTab] = useState<AdminTab>(() => {
    try {
      const saved = window.localStorage.getItem("savedstream-admin-tab");
      return ADMIN_TABS.some((item) => item.id === saved) ? (saved as AdminTab) : "dashboard";
    } catch {
      return "dashboard";
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem("savedstream-admin-tab", tab);
    } catch {
      // Storage can be unavailable in hardened browser contexts.
    }
  }, [tab]);

  useEffect(() => {
    void bootstrap();
    // The initial session check intentionally runs once for this route.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function bootstrap() {
    setPhase("checking");
    setBootstrapError("");
    try {
      const status = await api<PublicStatus>("/api/status");
      if (!status.configuration_ok) {
        setBootstrapError(
          tr("请先配置 TELEGRAM_API_ID、TELEGRAM_API_HASH 与 ADMIN_KEY，然后重启容器。", "Configure TELEGRAM_API_ID, TELEGRAM_API_HASH, and ADMIN_KEY, then restart the containers."),
        );
        setPhase("error");
        return;
      }
      if (!status.admin_authenticated) {
        setPhase("guest");
        return;
      }
      setPhase("ready");
      await loadDashboard();
    } catch (reason) {
      setBootstrapError(errorMessage(reason));
      setPhase("error");
    }
  }

  function applySettings(next: AdminSettings) {
    setSettings(next);
  }

  async function loadDashboard() {
    setPageLoading(true);
    setSettingsError("");
    try {
      const next = await api<AdminSettings>("/api/admin/settings");
      applySettings(next);
    } catch (reason) {
      if (isAdminSessionError(reason)) {
        setSettings(null);
        setPhase("guest");
      } else {
        setSettingsError(errorMessage(reason));
      }
    } finally {
      setPageLoading(false);
    }
  }

  async function refreshSettings() {
    setPageLoading(true);
    setSettingsError("");
    try {
      const next = await api<AdminSettings>("/api/admin/settings");
      applySettings(next);
    } catch (reason) {
      if (isAdminSessionError(reason)) {
        setPhase("guest");
      } else {
        setSettingsError(errorMessage(reason));
      }
    } finally {
      setPageLoading(false);
    }
  }

  async function handleAdminAuthenticated() {
    setPhase("ready");
    await syncOuterStatus();
    await loadDashboard();
  }

  async function handleTelegramAuthenticated() {
    setTelegramLoginOpen(false);
    await syncOuterStatus();
    await loadDashboard();
  }

  async function syncOuterStatus() {
    try {
      await onSessionChanged?.();
    } catch {
      // The admin page keeps its own authoritative state if the outer refresh fails.
    }
  }

  async function handleAdminLogout() {
    try {
      await api<{ ok: boolean }>("/api/admin/logout", { method: "POST" });
      await syncOuterStatus();
    } finally {
      setSettings(null);
      setPhase("guest");
    }
  }

  async function reconnectTelegram() {
    if (settings?.telegram.authenticated) {
      const confirmed = window.confirm(
        tr("重新登录会注销当前 Telegram 会话。确认继续吗？", "Signing in again will terminate the current Telegram session. Continue?"),
      );
      if (!confirmed) return;
      setPageLoading(true);
      setSettingsError("");
      try {
        await api<{ ok: boolean }>("/api/auth/logout?reset=true", { method: "POST" });
        await syncOuterStatus();
      } catch (reason) {
        setSettingsError(errorMessage(reason));
        setPageLoading(false);
        return;
      }
      setPageLoading(false);
    }
    setTelegramLoginOpen(true);
  }


  if (phase === "checking") {
    return (
      <CenterShell icon={<LoaderCircle className="spin" size={30} />} title={tr("正在验证管理员会话", "Verifying administrator session")}>
        <p className="gate-copy" role="status">{tr("正在读取 SavedStream 管理状态…", "Reading SavedStream administration status…")}</p>
      </CenterShell>
    );
  }

  if (phase === "error") {
    return (
      <CenterShell icon={<CircleAlert size={30} />} title={tr("无法打开管理后台", "Unable to open the admin console")}>
        <p className="form-error" role="alert">{bootstrapError}</p>
        <button className="button secondary wide" onClick={() => void bootstrap()} type="button">
          <RefreshCw size={18} />{tr("重新尝试", "Try again")}
        </button>
      </CenterShell>
    );
  }

  if (phase === "guest") {
    return <AdminKeyGate onAuthenticated={() => void handleAdminAuthenticated()} />;
  }

  if (telegramLoginOpen) {
    return (
      <div className="telegram-auth-route">
        <button
          className="button secondary telegram-auth-back"
          onClick={() => setTelegramLoginOpen(false)}
          type="button"
        >
          <ArrowLeft size={18} />{tr("返回管理后台", "Back to admin console")}
        </button>
        <TelegramLogin onAuthenticated={() => void handleTelegramAuthenticated()} />
      </div>
    );
  }

  if (!settings) {
    return (
      <CenterShell
        icon={settingsError ? <CircleAlert size={30} /> : <LoaderCircle className="spin" size={30} />}
        title={settingsError ? tr("无法载入管理后台", "Unable to load the admin console") : tr("正在载入管理后台", "Loading admin console")}
      >
        {settingsError ? (
          <>
            <p className="form-error" role="alert">{settingsError}</p>
            <button className="button secondary wide" onClick={() => void loadDashboard()} type="button">
              <RefreshCw size={18} />{tr("重新尝试", "Try again")}
            </button>
          </>
        ) : (
          <p className="gate-copy" role="status">{tr("正在读取缓存、隐私与媒体设置…", "Reading cache, privacy, and media settings…")}</p>
        )}
      </CenterShell>
    );
  }

  if (mediaCrypto.status !== "ready" || mediaCrypto.mode !== "persistent") {
    return <MediaEncryptionGate mode="persistent"><div /></MediaEncryptionGate>;
  }

  const telegram = settings.telegram;

  return (
    <div className="admin-page">
      <header className="admin-topbar">
        <a className="brand" href="/" aria-label={tr("返回 SavedStream 媒体库", "Back to the SavedStream media library")}>
          <span className="brand-mark"><Play size={16} fill="currentColor" /></span>
          <span>SavedStream</span>
        </a>
        <div className="admin-title-block">
          <span>{tr("控制台", "Console")}</span>
          <strong>{tr("媒体与隐私设置", "Media and privacy settings")}</strong>
        </div>
        <div className="admin-topbar-actions">
          <LanguageSelector compact />
          <ThemeSelector />
          <a className="button secondary" href="/">
            <ArrowLeft size={18} />{tr("返回媒体库", "Back to library")}
          </a>
          <button className="icon-button" onClick={() => void handleAdminLogout()} type="button" title={tr("退出管理员", "Sign out as administrator")} aria-label={tr("退出管理员", "Sign out as administrator")}>
            <LogOut size={20} />
          </button>
        </div>
      </header>

      <main className="admin-main" id="main-content">
        <div className="admin-heading">
          <div>
            <p className="admin-eyebrow">ADMIN SETTINGS</p>
            <h1>{tr("SavedStream 管理后台", "SavedStream admin console")}</h1>
          </div>
          <button className="button secondary" disabled={pageLoading} onClick={() => void refreshSettings()} type="button">
            <RefreshCw className={pageLoading ? "spin" : ""} size={18} />{tr("刷新状态", "Refresh status")}
          </button>
        </div>

        <div className="admin-feedback" aria-live="polite">
          {settingsError && <p className="form-error" role="alert"><CircleAlert size={17} />{settingsError}</p>}
        </div>

        <nav className="admin-tabs" role="tablist" aria-label={tr("管理分区", "Admin sections")}>
          {ADMIN_TABS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                role="tab"
                aria-selected={tab === item.id}
                className={tab === item.id ? "active" : ""}
                onClick={() => setTab(item.id)}
                type="button"
              >
                <Icon size={16} />
                <span>{tr(item.zh, item.en)}</span>
              </button>
            );
          })}
        </nav>

        <div className="admin-tab-body">
        {tab === "dashboard" && <AdminDashboardPanel settings={settings} onRefresh={refreshSettings} />}

        {tab === "telegram" && !settings.accounts.some((item) => item.state === "authenticated") && (
          <section className="admin-section telegram-section" aria-labelledby="telegram-setup-heading">
            <div className="admin-section-heading">
              <div className="section-icon warning" aria-hidden="true"><KeyRound size={22} /></div>
              <div>
                <h2 id="telegram-setup-heading">{settings.accounts.length === 0 ? tr("配置 Telegram API", "Configure Telegram API") : tr("连接 Telegram 账号", "Connect Telegram account")}</h2>
                <p>{settings.accounts.length === 0 ? tr("尚未配置托管账号", "No managed account configured") : tr("托管账号尚未完成登录", "The managed account is not signed in")}</p>
              </div>
              <span className="status-pill warning"><span aria-hidden="true" />{tr("需要配置", "Configuration required")}</span>
            </div>
            <div className="telegram-status-body">
              <div className="status-detail">
                <strong>{settings.accounts.length === 0 ? tr("需要 Telegram API ID 和 API Hash", "Telegram API ID and API Hash are required") : tr("需要扫码完成账号登录", "Scan a QR code to finish signing in")}</strong>
                <p>{settings.accounts.length === 0 ? tr("请在下方“新增托管账号”中填写凭据。凭据将保存在 TeleBox 数据卷中，无需修改 Docker 环境变量。", "Enter the credentials under Add managed account below. They are stored in the TeleBox data volume; Docker environment variables are not required.") : tr("请在下方“托管账号”列表中点击扫码连接。", "Click QR sign-in in the managed accounts list below.")}</p>
              </div>
              <button className="button primary" onClick={() => document.getElementById("managed-account-form")?.scrollIntoView({ behavior: "smooth", block: "center" })} type="button">
                <KeyRound size={18} />{settings.accounts.length === 0 ? tr("立即配置", "Configure now") : tr("前往连接", "Connect now")}
              </button>
            </div>
          </section>
        )}

        {tab === "telegram" && settings.accounts.some((item) => item.state === "authenticated") && (<section className="admin-section telegram-section" aria-labelledby="telegram-heading">
          <div className="admin-section-heading">
            <div className={`section-icon ${telegram.authenticated ? "success" : "warning"}`} aria-hidden="true">
              {telegram.authenticated ? <Wifi size={22} /> : <WifiOff size={22} />}
            </div>
            <div>
              <h2 id="telegram-heading">{tr("Telegram 连接", "Telegram connection")}</h2>
              <p>{tr("收藏夹读取会话", "Saved Messages read session")}</p>
            </div>
            <StatusPill auth={telegram} />
          </div>
          <div className="telegram-status-body">
            <div className="status-detail">
              <span>{tr("当前状态", "Current status")}</span>
              <strong>{telegramStateLabel(telegram.state)}</strong>
              {telegram.error && <p role="alert">{telegram.error}</p>}
              {telegram.state === "configuration_required" && (
                <p>{tr("请先配置 Docker 环境变量 TELEGRAM_API_ID 与 TELEGRAM_API_HASH。", "Configure the TELEGRAM_API_ID and TELEGRAM_API_HASH Docker environment variables first.")}</p>
              )}
            </div>
            <button
              className="button primary"
              disabled={pageLoading || telegram.state === "configuration_required"}
              onClick={() => document.getElementById("managed-account-form")?.scrollIntoView({ behavior: "smooth", block: "center" })}
              type="button"
            >
              {telegram.authenticated ? <RefreshCw size={18} /> : <KeyRound size={18} />}
              {telegram.authenticated ? tr("重新登录", "Sign in again") : tr("扫码登录", "QR sign-in")}
            </button>
          </div>
        </section>)}

        {tab === "telegram" && <CoordinationPanel settings={settings} onRefresh={refreshSettings} />}
        {tab === "review" && <ReviewQueuePanel settings={settings} onRefresh={refreshSettings} />}
        {tab === "rate" && <HelperRateLimitPanel settings={settings} onRefresh={refreshSettings} />}
        {tab === "users" && <AccessUsersPanel settings={settings} onRefresh={refreshSettings} />}
        {tab === "album" && <PublicAlbumPanel settings={settings} onRefresh={refreshSettings} />}
        {tab === "media" && <MediaIndexPanel settings={settings} onRefresh={refreshSettings} />}
        {tab === "upload" && <UploadPanel settings={settings} onRefresh={refreshSettings} />}
        {tab === "traffic" && <TrafficSettingsPanel settings={settings} onRefresh={refreshSettings} />}
        {tab === "cache" && <CacheSettingsPanel settings={settings} onRefresh={refreshSettings} />}
        {tab === "mailbox" && <MailboxAdminPanel settings={settings} onRefresh={refreshSettings} />}
        {tab === "backups" && <BackupAdminPanel />}
        {tab === "storage" && <StorageAdminPanel onGoToBackups={() => setTab("backups")} />}

        {tab === "media" && <MediaLibraryPanel settings={settings} onRefresh={refreshSettings} />}
        </div>
      </main>
    </div>
  );
}

function AdminDashboardPanel({ settings }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { locale, tr } = useI18n();
  const [summary, setSummary] = useState<TrafficSummary>(settings.traffic);
  const [series, setSeries] = useState<TrafficSeriesPoint[]>([]);
  const [range, setRange] = useState<"7d" | "30d" | "month">("7d");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setSummary(settings.traffic);
  }, [settings.traffic]);

  useEffect(() => {
    let disposed = false;
    async function load() {
      setLoading(true);
      try {
        const [nextSummary, nextSeries] = await Promise.all([
          api<TrafficSummary>("/api/admin/traffic/summary"),
          api<{ range: string; items: TrafficSeriesPoint[] }>(`/api/admin/traffic/series?range=${range}`),
        ]);
        if (!disposed) {
          setSummary(nextSummary);
          setSeries(nextSeries.items);
          setError("");
        }
      } catch (reason) {
        if (!disposed) setError(errorMessage(reason));
      } finally {
        if (!disposed) setLoading(false);
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 20_000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [range]);

  const trafficSettings = summary.settings;
  const usage = summary.usage;
  const usagePercent = trafficSettings.enabled
    ? Math.min(100, usage.usage_percent)
    : 0;
  const warningReached = trafficSettings.enabled && usagePercent >= trafficSettings.warning_percent;
  const authenticatedAccounts = settings.accounts.filter((item) => item.state === "authenticated").length;
  const activeUploads = settings.upload_jobs.filter((item) => !["completed", "failed", "cancelled"].includes(item.status)).length;
  const indexing = settings.media_sync.filter((item) => ["running", "queued", "indexing"].includes(item.status)).length;

  return (
    <AdminSectionCard
      storageKey="dashboard"
      title={tr("运行仪表盘", "Operations dashboard")}
      summary={`${authenticatedAccounts}/${settings.accounts.length} ${tr("个账号在线", "accounts online")} · ${formatTrafficBytes(usage.bytes_total)} ${tr("本月媒体流量", "media traffic this month")}`}
      icon={<Gauge size={22} />}
      defaultOpen
    >
      <div className="dashboard-stat-grid">
        <DashboardStat icon={<Activity size={18} />} label={tr("本月总流量", "Total this month")} value={formatTrafficBytes(usage.bytes_total)} detail={trafficSettings.enabled ? `${tr("剩余", "Remaining")} ${formatTrafficBytes(usage.remaining_bytes ?? 0)} / ${formatTrafficBytes(trafficSettings.monthly_limit_bytes)}` : tr("限额未启用", "Limit disabled")} tone={warningReached ? "warning" : "default"} />
        <DashboardStat icon={<ArrowDownToLine size={18} />} label={tr("下行 / 上行", "Download / upload")} value={`${formatRate(summary.outbound_bps)} / ${formatRate(summary.inbound_bps)}`} detail={`${formatTrafficBytes(usage.bytes_out)} ↓ · ${formatTrafficBytes(usage.bytes_in)} ↑`} />
        <DashboardStat icon={<Wifi size={18} />} label={tr("活跃请求", "Active requests")} value={String(summary.active_requests)} detail={`${summary.active_streams} ${tr("个媒体流", "media streams")} · ${summary.active_uploads} ${tr("个上传", "uploads")}`} />
        <DashboardStat icon={<HardDrive size={18} />} label={tr("缓存占用", "Cache usage")} value={formatBytes(settings.cache_bytes)} detail={`${settings.cache_files.toLocaleString(locale)} ${tr("个文件", "files")} · ${cachePercent(settings)}%`} />
        <DashboardStat icon={<Users size={18} />} label={tr("账号与访问", "Accounts and access")} value={`${authenticatedAccounts} ${tr("个在线", "online")}`} detail={`${settings.access_users.filter((item) => item.status === "approved").length} ${tr("个已批准用户", "approved users")}`} />
        <DashboardStat icon={<Database size={18} />} label={tr("索引与任务", "Indexes and jobs")} value={`${indexing} ${tr("个索引中", "indexing")}`} detail={`${activeUploads} ${tr("个上传/入库任务", "upload/ingest jobs")}`} />
      </div>

      <div className="dashboard-columns">
        <section className="dashboard-subcard traffic-chart-card" aria-labelledby="traffic-chart-heading">
          <div className="dashboard-subcard-heading">
            <div>
              <h3 id="traffic-chart-heading">{tr("流量用量曲线", "Traffic usage")}</h3>
              <p>{tr("SavedStream 对外媒体上下行总量，不包含 Telegram 容器内部传输。", "Total external SavedStream media traffic; internal Telegram container traffic is excluded.")}</p>
            </div>
            <div className="traffic-range-tabs" role="tablist" aria-label={tr("流量曲线范围", "Traffic chart range")}>
              {(["7d", "30d", "month"] as const).map((value) => (
                <button key={value} className={range === value ? "active" : ""} onClick={() => setRange(value)} type="button">
                  {value === "7d" ? tr("7 天", "7 days") : value === "30d" ? tr("30 天", "30 days") : tr("本月", "This month")}
                </button>
              ))}
            </div>
          </div>
          {error && <p className="form-error" role="alert"><CircleAlert size={16} />{error}</p>}
          <TrafficChart items={series} loading={loading} />
          <div className="traffic-legend">
            <span><i className="traffic-dot inbound" />{tr("上行", "Upload")} {formatTrafficBytes(usage.bytes_in)}</span>
            <span><i className="traffic-dot outbound" />{tr("下行", "Download")} {formatTrafficBytes(usage.bytes_out)}</span>
            <span><i className="traffic-dot limit" />{tr("已用", "Used")} {usagePercent.toFixed(1)}%</span>
          </div>
        </section>

      </div>
    </AdminSectionCard>
  );
}

function TrafficSettingsPanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { tr } = useI18n();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [capacityGb, setCapacityGb] = useState(settings.traffic.settings.monthly_capacity_gb);
  const [limitGb, setLimitGb] = useState(settings.traffic.settings.monthly_limit_gb);
  const [enabled, setEnabled] = useState(settings.traffic.settings.enabled);
  const [warningPercent, setWarningPercent] = useState(settings.traffic.settings.warning_percent);
  const [adminBypass, setAdminBypass] = useState(settings.traffic.settings.admin_bypass);

  useEffect(() => {
    const next = settings.traffic.settings;
    setCapacityGb(next.monthly_capacity_gb);
    setLimitGb(next.monthly_limit_gb);
    setEnabled(next.enabled);
    setWarningPercent(next.warning_percent);
    setAdminBypass(next.admin_bypass);
  }, [settings.traffic.settings]);

  async function saveTrafficSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const next = await api<TrafficSummary["settings"]>("/api/admin/traffic/settings", {
        method: "PUT",
        body: JSON.stringify({
          enabled,
          monthly_capacity_gb: capacityGb,
          monthly_limit_gb: limitGb,
          warning_percent: warningPercent,
          admin_bypass: adminBypass,
        }),
      });
      setNotice(tr("流量限制设置已保存。额度按 UTC 月份统计。", "Traffic limit settings saved. Allowances are measured by UTC month."));
      setEnabled(next.enabled);
      await onRefresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  async function resetTraffic() {
    if (!window.confirm(tr("确认清空当前月份的流量统计吗？这不会删除媒体、缓存或 Telegram 数据。", "Reset traffic statistics for the current month? Media, cache, and Telegram data will not be deleted."))) return;
    setSaving(true);
    setError("");
    try {
      await api("/api/admin/traffic/reset?scope=month", { method: "POST" });
      setNotice(tr("当前月份流量统计已重置。", "Traffic statistics for the current month were reset."));
      await onRefresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  const trafficSettings = settings.traffic.settings;
  return (
    <section className="admin-section traffic-section" aria-labelledby="traffic-heading">
      <div className="admin-section-heading">
        <div className="section-icon" aria-hidden="true"><Activity size={22} /></div>
        <div>
          <h2 id="traffic-heading">{tr("服务器流量限额", "Server traffic limit")}</h2>
          <p>{tr("达到允许额度后，新的媒体传输和上传会返回 509。", "New media transfers and uploads return 509 after the allowance is reached.")}</p>
        </div>
        <span className={`status-pill ${trafficSettings.enabled ? "success" : "warning"}`}><span aria-hidden="true" />{trafficSettings.enabled ? tr("限额启用", "Limit enabled") : tr("未启用", "Disabled")}</span>
      </div>
      {error && <p className="form-error" role="alert"><CircleAlert size={17} />{error}</p>}
      {notice && <p className="form-success" role="status"><Check size={17} />{notice}</p>}
      <form className="traffic-settings-card traffic-tab-form" onSubmit={(event) => void saveTrafficSettings(event)}>
        <label className="toggle-control traffic-toggle">
          <span><strong>{tr("启用月度总量限制", "Enable monthly total limit")}</strong><small>{tr("上行 + 下行合计，按 UTC 月份归零", "Upload + download total, reset by UTC month")}</small></span>
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
          <span className="toggle-track"><span /></span>
        </label>
        <div className="traffic-number-grid">
          <label className="form-field"><span>{tr("服务器月容量（GB）", "Server monthly capacity (GB)")}</span><input type="number" min="1" step="1" value={capacityGb} onChange={(event) => setCapacityGb(Number(event.target.value))} /></label>
          <label className="form-field"><span>{tr("SavedStream 允许（GB）", "SavedStream allowance (GB)")}</span><input type="number" min="1" step="1" value={limitGb} onChange={(event) => setLimitGb(Number(event.target.value))} /></label>
        </div>
        <div className="range-field traffic-warning-field">
          <div className="field-label-row"><label htmlFor="traffic-warning">{tr("预警阈值", "Warning threshold")}</label><output htmlFor="traffic-warning">{warningPercent}%</output></div>
          <input id="traffic-warning" type="range" min="50" max="99" step="1" value={warningPercent} onChange={(event) => setWarningPercent(Number(event.target.value))} />
          <div className="range-bounds"><span>50%</span><span>99%</span></div>
        </div>
        <label className="check-control"><input type="checkbox" checked={adminBypass} onChange={(event) => setAdminBypass(event.target.checked)} />{tr("管理员媒体请求绕过限额", "Administrator media requests bypass the limit")}</label>
        <div className="admin-sticky-actions">
          <div className="admin-save-float">
            <button className="button primary" disabled={saving} type="submit">{saving ? <LoaderCircle className="spin" size={17} /> : <Save size={17} />}{tr("保存限额", "Save limit")}</button>
            <button className="button danger-ghost" disabled={saving} onClick={() => void resetTraffic()} type="button"><RotateCcw size={17} />{tr("重置本月统计", "Reset this month")}</button>
          </div>
        </div>
      </form>
    </section>
  );
}

function CacheSettingsPanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { locale, tr } = useI18n();
  const [cacheMaxGb, setCacheMaxGb] = useState(settings.cache_max_gb);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  useEffect(() => setCacheMaxGb(settings.cache_max_gb), [settings.cache_max_gb]);

  const cacheUsagePercent = settings.cache_max_gb > 0
    ? Math.min(100, (settings.cache_bytes / (settings.cache_max_gb * 1024 ** 3)) * 100)
    : 0;
  const cacheSummary = `${formatBytes(settings.cache_bytes)} / ${formatCacheLimit(settings.cache_max_gb)}`;

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    try {
      await api<{ ok: boolean }>("/api/admin/settings", {
        method: "PUT",
        body: JSON.stringify({ cache_max_gb: cacheMaxGb }),
      });
      setNotice(tr("设置已保存。", "Settings saved."));
      await onRefresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  async function clearCache() {
    if (!window.confirm(tr("确认清空全部本地媒体缓存吗？正在播放的媒体可能需要重新拉取。", "Clear all local media cache? Active media may need to be fetched again."))) return;
    setClearing(true);
    setError("");
    setNotice("");
    try {
      await api<{ ok: boolean }>("/api/admin/cache", { method: "DELETE" });
      setNotice(tr("本地缓存已清空。", "Local cache cleared."));
      await onRefresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setClearing(false);
    }
  }

  return (
    <section className="admin-section cache-section" aria-labelledby="cache-heading">
      <div className="admin-section-heading">
        <div className="section-icon" aria-hidden="true"><HardDrive size={22} /></div>
        <div>
          <h2 id="cache-heading">{tr("本地缓存", "Local cache")}</h2>
          <p>{tr("限制媒体分块占用的磁盘空间", "Limit disk space used by media chunks")}</p>
        </div>
        <span className="metric-value">{cacheSummary}</span>
      </div>
      {error && <p className="form-error" role="alert"><CircleAlert size={17} />{error}</p>}
      {notice && <p className="form-success" role="status"><Check size={17} />{notice}</p>}
      <form onSubmit={(event) => void saveSettings(event)}>
        <div className="cache-meter" aria-label={`${tr("缓存已使用", "Cache used")} ${cacheUsagePercent.toFixed(0)}%`}>
          <span style={{ width: `${cacheUsagePercent}%` }} />
        </div>
        <div className="cache-stats">
          <span><Database size={16} />{settings.cache_files.toLocaleString(locale)} {tr("个缓存文件", "cached files")}</span>
          <span><Gauge size={16} />{tr("已使用", "Used")} {cacheUsagePercent.toFixed(1)}%</span>
        </div>
        <div className="range-field">
          <div className="field-label-row">
            <label htmlFor="cache-limit">{tr("缓存上限", "Cache limit")}</label>
            <output htmlFor="cache-limit">{formatCacheLimit(cacheMaxGb)}</output>
          </div>
          <input
            id="cache-limit"
            type="range"
            min={MIN_CACHE_GB}
            max={MAX_CACHE_GB}
            step={0.5}
            value={cacheMaxGb}
            onChange={(event) => {
              setCacheMaxGb(Number(event.target.value));
              setNotice("");
            }}
            aria-valuetext={formatCacheLimit(cacheMaxGb)}
          />
          <div className="range-bounds" aria-hidden="true">
            <span>{MIN_CACHE_GB} GB</span>
            <span>{MAX_CACHE_GB} GB</span>
          </div>
        </div>
        <div className="admin-sticky-actions">
          <div className="admin-save-float">
            <button className="button primary" disabled={saving} type="submit">
              {saving ? <LoaderCircle className="spin" size={18} /> : <Save size={18} />}
              {tr("保存设置", "Save settings")}
            </button>
            <button className="button danger-ghost" disabled={clearing} onClick={() => void clearCache()} type="button">
              {clearing ? <LoaderCircle className="spin" size={18} /> : <Trash2 size={18} />}
              {tr("清空缓存", "Clear cache")}
            </button>
          </div>
        </div>
      </form>
    </section>
  );
}


function MediaLibraryPanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { tr } = useI18n();
  const telegram = settings.telegram;
  const [media, setMedia] = useState<MediaItem[]>([]);
  const [mediaCursor, setMediaCursor] = useState<string | number | null>(null);
  const [mediaHasMore, setMediaHasMore] = useState(false);
  const [mediaLoading, setMediaLoading] = useState(false);
  const [mediaLoadingMore, setMediaLoadingMore] = useState(false);
  const [mediaError, setMediaError] = useState("");
  const [notice, setNotice] = useState("");
  const [mediaQuery, setMediaQuery] = useState("");
  const [activeMediaQuery, setActiveMediaQuery] = useState("");
  const [titleDrafts, setTitleDrafts] = useState<Record<string, string>>({});
  const [savingTitleId, setSavingTitleId] = useState<string | null>(null);
  const [savedTitleId, setSavedTitleId] = useState<string | null>(null);
  const [selectedMediaKeys, setSelectedMediaKeys] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  useEffect(() => {
    void loadMediaPage(null);
    // The initial media page intentionally loads once for this panel.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function loadMediaPage(cursor: string | number | null, search = activeMediaQuery) {
    cursor === null ? setMediaLoading(true) : setMediaLoadingMore(true);
    setMediaError("");
    try {
      const params = new URLSearchParams({
        limit: String(MEDIA_PAGE_SIZE),
        order: "newest",
        kind: "all",
        q: search,
        scope: "all",
      });
      if (cursor !== null) params.set("cursor", String(cursor));
      const page = await api<MediaPage>(`/api/media?${params}`);
      setMedia((current) => cursor === null ? page.items : [...current, ...page.items]);
      if (cursor === null) setSelectedMediaKeys(new Set());
      setMediaCursor(page.next_cursor);
      setMediaHasMore(page.has_more);
      setTitleDrafts((current) => {
        const next = cursor === null ? {} : { ...current };
        for (const item of page.items) next[mediaKey(item)] = item.local_title || "";
        return next;
      });
    } catch (reason) {
      setMediaError(errorMessage(reason));
      if (cursor === null) setMedia([]);
    } finally {
      setMediaLoading(false);
      setMediaLoadingMore(false);
    }
  }

  async function searchMedia(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = mediaQuery.trim();
    setActiveMediaQuery(query);
    await loadMediaPage(null, query);
  }

  async function saveTitle(event: FormEvent<HTMLFormElement>, item: MediaItem) {
    event.preventDefault();
    const key = mediaKey(item);
    const title = (titleDrafts[key] || "").trim();
    setSavingTitleId(key);
    setSavedTitleId(null);
    setMediaError("");
    try {
      await api<{ ok: boolean }>(`/api/admin/media/${item.id}?account=${encodeURIComponent(item.account_id)}`, {
        method: "PUT",
        body: JSON.stringify({ title }),
      });
      setMedia((current) => current.map((currentItem) => mediaKey(currentItem) === key
        ? { ...currentItem, local_title: title || null, title: title || currentItem.original_title }
        : currentItem));
      setTitleDrafts((current) => ({ ...current, [key]: title }));
      setSavedTitleId(key);
    } catch (reason) {
      setMediaError(errorMessage(reason));
    } finally {
      setSavingTitleId(null);
    }
  }

  async function setVisibility(item: MediaItem, visibility: MediaVisibility) {
    setMediaError("");
    try {
      const updated = await api<MediaItem>(`/api/admin/media/${item.id}/visibility?account=${encodeURIComponent(item.account_id)}`, {
        method: "PATCH",
        body: JSON.stringify({ visibility }),
      });
      const key = mediaKey(item);
      setMedia((current) => current.map((currentItem) => mediaKey(currentItem) === key
        ? { ...currentItem, visibility: updated.visibility, hidden: updated.hidden }
        : currentItem));
      setNotice(tr("可见性已更新。", "Visibility updated."));
    } catch (reason) {
      setMediaError(errorMessage(reason));
    }
  }

  function toggleMediaSelection(item: MediaItem) {
    const key = mediaKey(item);
    setSelectedMediaKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }

  async function bulkSetVisibility(visibility: MediaVisibility) {
    const selected = media.filter((item) => selectedMediaKeys.has(mediaKey(item)));
    if (!selected.length || bulkBusy) return;
    setBulkBusy(true);
    setMediaError("");
    try {
      await api("/api/admin/media/visibility", {
        method: "POST",
        body: JSON.stringify({
          visibility,
          items: selected.map((item) => ({ account_id: item.account_id, message_id: item.id })),
        }),
      });
      setMedia((current) => current.map((item) => selectedMediaKeys.has(mediaKey(item)) ? { ...item, visibility } : item));
      setSelectedMediaKeys(new Set());
      setNotice(tr("批量可见性已更新。", "Bulk visibility updated."));
    } catch (reason) {
      setMediaError(errorMessage(reason));
    } finally {
      setBulkBusy(false);
    }
  }

  async function bulkDelete() {
    const selected = media.filter((item) => selectedMediaKeys.has(mediaKey(item)));
    if (!selected.length || bulkBusy) return;
    const confirmed = window.confirm(tr(
      `确认删除选中的 ${selected.length} 项资源吗？该操作会同时删除 Telegram 消息与本地缓存。`,
      `Delete the ${selected.length} selected items? Telegram messages and local cache are also removed.`,
    ));
    if (!confirmed) return;
    setBulkBusy(true);
    setMediaError("");
    try {
      for (const item of selected) {
        await api(`/api/admin/media/${item.id}?account=${encodeURIComponent(item.account_id)}`, {
          method: "DELETE",
          body: JSON.stringify({ reason: tr("管理员从媒体库批量删除", "Removed by administrator from the library") }),
        });
      }
      setSelectedMediaKeys(new Set());
      setNotice(tr("批量删除完成。", "Bulk deletion complete."));
      await loadMediaPage(null);
      await onRefresh();
    } catch (reason) {
      setMediaError(errorMessage(reason));
    } finally {
      setBulkBusy(false);
    }
  }

  async function deleteMedia(item: MediaItem) {
    const confirmed = window.confirm(tr(
      `确认删除「${item.title}」吗？该操作会同时删除 Telegram 消息与本地缓存。`,
      `Delete "${item.title}"? This also removes the Telegram message and local cache.`,
    ));
    if (!confirmed) return;
    setMediaError("");
    try {
      await api(`/api/admin/media/${item.id}?account=${encodeURIComponent(item.account_id)}`, {
        method: "DELETE",
        body: JSON.stringify({ reason: tr("管理员从媒体库删除", "Removed by administrator from the library") }),
      });
      const key = mediaKey(item);
      setMedia((current) => current.filter((currentItem) => mediaKey(currentItem) !== key));
      setNotice(tr("资源已删除。", "Media deleted."));
      await onRefresh();
    } catch (reason) {
      setMediaError(errorMessage(reason));
    }
  }

  return (
    <section className="admin-section media-title-section" aria-labelledby="media-title-heading">
      <div className="admin-section-heading media-title-heading-row">
        <div className="section-icon" aria-hidden="true"><Pencil size={22} /></div>
        <div>
          <h2 id="media-title-heading">{tr("媒体库管理", "Media library")}</h2>
          <p>{tr("本地标题、可见性与删除；可见性含公开、私有与仅管理员可见的隐藏。", "Local titles, visibility, and deletion. Visibility includes public, private, and admin-only hidden.")}</p>
        </div>
        <form className="admin-media-search" onSubmit={searchMedia} role="search">
          <label className="sr-only" htmlFor="admin-media-query">{tr("搜索收藏夹媒体", "Search Saved Messages media")}</label>
          <Search size={17} aria-hidden="true" />
          <input
            id="admin-media-query"
            type="search"
            value={mediaQuery}
            onChange={(event) => setMediaQuery(event.target.value)}
            placeholder={tr("搜索媒体", "Search media")}
          />
          <button className="button secondary" disabled={!telegram.authenticated || mediaLoading} type="submit">
            {tr("搜索", "Search")}
          </button>
        </form>
      </div>
      {mediaError && <p className="form-error" role="alert"><CircleAlert size={17} />{mediaError}</p>}
      {notice && <p className="form-success" role="status"><Check size={17} />{notice}</p>}
      {selectedMediaKeys.size > 0 && (
        <div className="media-bulk-toolbar">
          <span>{tr("已选择", "Selected")} {selectedMediaKeys.size} {tr("项", "items")}</span>
          <button className="button secondary" disabled={bulkBusy} onClick={() => void bulkSetVisibility("public")} type="button"><Globe2 size={16} />{tr("设为公开", "Make public")}</button>
          <button className="button secondary" disabled={bulkBusy} onClick={() => void bulkSetVisibility("private")} type="button"><Shield size={16} />{tr("设为私有", "Make private")}</button>
          <button className="button secondary" disabled={bulkBusy} onClick={() => void bulkSetVisibility("hidden")} type="button"><EyeOff size={16} />{tr("设为隐藏", "Hide")}</button>
          <button className="button danger" disabled={bulkBusy} onClick={() => void bulkDelete()} type="button"><Trash2 size={16} />{tr("删除", "Delete")}</button>
          <button className="button ghost" disabled={bulkBusy} onClick={() => setSelectedMediaKeys(new Set())} type="button">{tr("清除选择", "Clear selection")}</button>
        </div>
      )}

      {!telegram.authenticated ? (
        <div className="admin-empty-state">
          <WifiOff size={32} />
          <h3>{tr("连接 Telegram 后可编辑媒体", "Connect Telegram to edit media")}</h3>
          <button className="button primary" onClick={() => document.getElementById("managed-account-form")?.scrollIntoView({ behavior: "smooth", block: "center" })} type="button">
            <KeyRound size={18} />{tr("扫码登录", "QR sign-in")}
          </button>
        </div>
      ) : mediaLoading ? (
        <div className="admin-loading-row" role="status">
          <LoaderCircle className="spin" size={22} />{tr("正在读取收藏夹…", "Reading Saved Messages…")}
        </div>
      ) : mediaError && media.length === 0 ? (
        <div className="admin-empty-state" role="alert">
          <CircleAlert size={32} />
          <h3>{tr("无法读取媒体", "Unable to load media")}</h3>
          <p>{mediaError}</p>
          <button className="button secondary" onClick={() => void loadMediaPage(null)} type="button">
            <RefreshCw size={18} />{tr("重试", "Retry")}
          </button>
        </div>
      ) : media.length === 0 ? (
        <div className="admin-empty-state">
          <Database size={32} />
          <h3>{activeMediaQuery ? tr("没有匹配的媒体", "No matching media") : tr("收藏夹中还没有媒体", "Saved Messages does not contain media yet")}</h3>
          {mediaHasMore && mediaCursor !== null && (
            <button
              className="button secondary"
              disabled={mediaLoadingMore}
              onClick={() => void loadMediaPage(mediaCursor)}
              type="button"
            >
              {mediaLoadingMore && <LoaderCircle className="spin" size={18} />}
              {tr("继续查找", "Continue searching")}
            </button>
          )}
        </div>
      ) : (
        <>
          <div className="admin-media-list">
            {media.map((item) => {
              const key = mediaKey(item);
              return <form className="admin-media-row" key={key} onSubmit={(event) => void saveTitle(event, item)}>
                <label className="media-select-checkbox" title={tr("选择媒体", "Select media")}>
                  <input type="checkbox" checked={selectedMediaKeys.has(key)} onChange={() => toggleMediaSelection(item)} aria-label={`${tr("选择", "Select")} ${item.original_title}`} />
                </label>
                <AdminMediaThumbnail item={item} />
                <div className="admin-media-identity">
                  <strong title={item.original_title}>{item.original_title}</strong>
                  <span>{formatAdminMediaMeta(item)} · {item.account_id}</span>
                </div>
                <div className="admin-title-field">
                  <label className="sr-only" htmlFor={`media-title-${key}`}>
                    {item.original_title} {tr("的本地标题", "local title")}
                  </label>
                  <input
                    id={`media-title-${key}`}
                    type="text"
                    maxLength={200}
                    value={titleDrafts[key] ?? ""}
                    onChange={(event) => {
                      setTitleDrafts((current) => ({ ...current, [key]: event.target.value }));
                      if (savedTitleId === key) setSavedTitleId(null);
                    }}
                    placeholder={tr("使用 Telegram 原标题", "Use Telegram title")}
                  />
                </div>
                <select
                  className="admin-visibility-select"
                  value={item.visibility}
                  disabled={bulkBusy}
                  onChange={(event) => void setVisibility(item, event.target.value as MediaVisibility)}
                  aria-label={`${tr("可见性", "Visibility")} ${item.original_title}`}
                >
                  <option value="public">{tr("公开", "Public")}</option>
                  <option value="private">{tr("私有", "Private")}</option>
                  <option value="hidden">{tr("隐藏", "Hidden")}</option>
                </select>
                <button className="icon-button list-delete-button" disabled={bulkBusy} onClick={() => void deleteMedia(item)} title={tr("删除", "Delete")} aria-label={`${tr("删除", "Delete")} ${item.original_title}`} type="button">
                  <Trash2 size={17} />
                </button>
                <button
                  className="button secondary title-save-button"
                  disabled={savingTitleId === key}
                  type="submit"
                >
                  {savingTitleId === key
                    ? <LoaderCircle className="spin" size={17} />
                    : savedTitleId === key
                      ? <Check size={17} />
                      : <Save size={17} />}
                  {savedTitleId === key ? tr("已保存", "Saved") : tr("保存", "Save")}
                </button>
              </form>;
            })}
          </div>
          {mediaHasMore && mediaCursor !== null && (
            <div className="admin-load-more">
              <button
                className="button secondary"
                disabled={mediaLoadingMore}
                onClick={() => void loadMediaPage(mediaCursor)}
                type="button"
              >
                {mediaLoadingMore && <LoaderCircle className="spin" size={18} />}
                {tr("加载更多", "Load more")}
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function MailboxAdminPanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { tr } = useI18n();
  const [recipient, setRecipient] = useState<string>("all");
  const [kind, setKind] = useState("system");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [history, setHistory] = useState<NotificationItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  async function loadHistory() {
    setHistoryLoading(true);
    try {
      const result = await api<{ items: NotificationItem[] }>("/api/admin/notifications?limit=100");
      setHistory(Array.isArray(result.items) ? result.items : []);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setHistoryLoading(false);
    }
  }

  useEffect(() => {
    void loadHistory();
    // The sent-history loads once per panel mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!title.trim() || !body.trim() || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const payload: { kind: string; title: string; body: string; user_id?: number } = {
        kind,
        title: title.trim(),
        body: body.trim(),
      };
      if (recipient !== "all") payload.user_id = Number(recipient);
      const result = await api<{ sent: number }>("/api/admin/notifications", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setNotice(tr(`已发送给 ${result.sent} 位用户，对方信箱会显示红点。`, `Sent to ${result.sent} users. Their mailbox will show an unread badge.`));
      setTitle("");
      setBody("");
      await loadHistory();
      await onRefresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-section mailbox-admin-section" aria-labelledby="mailbox-admin-heading">
      <div className="admin-section-heading">
        <div className="section-icon" aria-hidden="true"><Mail size={22} /></div>
        <div>
          <h2 id="mailbox-admin-heading">{tr("站内信（信箱）", "Mailbox")}</h2>
          <p>{tr("向指定用户或全部用户发送系统通知；审核与资源操作会自动通知上传者。", "Send system notifications to a specific user or everyone. Review and media actions notify uploaders automatically.")}</p>
        </div>
        <span className="metric-value">{settings.auth_users.length} {tr("个用户", "users")}</span>
      </div>
      {error && <p className="form-error" role="alert"><CircleAlert size={17} />{error}</p>}
      {notice && <p className="form-success" role="status"><Check size={17} />{notice}</p>}
      <form className="mailbox-admin-form" onSubmit={(event) => void send(event)}>
        <div className="traffic-number-grid">
          <label className="form-field">
            <span>{tr("收件人", "Recipient")}</span>
            <select value={recipient} onChange={(event) => setRecipient(event.target.value)}>
              <option value="all">{tr("全部用户（广播）", "All users (broadcast)")}</option>
              {settings.auth_users.map((user) => (
                <option key={user.id} value={String(user.id)}>
                  {user.username || tr("用户", "User")} #{user.id}{user.telegram_user_id ? ` · TG ${user.telegram_user_id}` : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="form-field">
            <span>{tr("类型", "Kind")}</span>
            <select value={kind} onChange={(event) => setKind(event.target.value)}>
              <option value="system">{tr("系统通知", "System")}</option>
              <option value="media">{tr("资源管理", "Media")}</option>
              <option value="review">{tr("审核", "Review")}</option>
            </select>
          </label>
        </div>
        <label className="form-field">
          <span>{tr("标题", "Title")}</span>
          <input type="text" value={title} maxLength={200} onChange={(event) => setTitle(event.target.value)} required />
        </label>
        <label className="form-field">
          <span>{tr("正文", "Body")}</span>
          <textarea value={body} maxLength={2000} rows={5} onChange={(event) => setBody(event.target.value)} required />
        </label>
        <div className="admin-sticky-actions">
          <div className="admin-save-float">
            <button className="button primary" disabled={busy} type="submit">
              {busy ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />}{tr("发送", "Send")}
            </button>
          </div>
        </div>
      </form>
      <div className="mailbox-admin-history">
        <h3>{tr("最近发送", "Recently sent")}</h3>
        {historyLoading ? (
          <div className="admin-loading-row" role="status"><LoaderCircle className="spin" size={20} />{tr("正在读取发送记录", "Loading sent history")}</div>
        ) : history.length === 0 ? (
          <p className="muted">{tr("还没有发送记录。", "Nothing has been sent yet.")}</p>
        ) : (
          history.slice(0, 40).map((item) => (
            <div className="coordination-row" key={item.id}>
              <span><strong>{item.title}</strong><small>{item.body}</small></span>
              <small>{item.recipient || `#${item.user_id}`} · {item.is_read ? tr("已读", "Read") : tr("未读", "Unread")} · {item.kind}</small>
            </div>
          ))
        )}
      </div>
    </section>
  );
}



function StorageAdminPanel({ onGoToBackups }: { onGoToBackups: () => void }) {
  const { tr } = useI18n();
  const [state, setState] = useState<StorageSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    setError("");
    try {
      const result = await api<StorageSnapshot>("/api/admin/storage");
      setState(result);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), 60_000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function runRecommendation(entry: StorageRecommendation) {
    if (entry.action === "cleanup_backups") {
      onGoToBackups();
      return;
    }
    if (entry.action === "clear_cache") {
      const confirmed = window.confirm(tr(
        "确认清空全部本地媒体缓存吗？正在播放的媒体可能需要重新拉取。",
        "Clear all local media cache? Active media may need to be fetched again.",
      ));
      if (!confirmed) return;
      setBusy(entry.code);
      setError("");
      setNotice("");
      try {
        await api<{ ok: boolean }>("/api/admin/cache", { method: "DELETE" });
        setNotice(tr("本地缓存已清空。", "Local cache cleared."));
        await load(true);
      } catch (reason) {
        setError(errorMessage(reason));
      } finally {
        setBusy("");
      }
    }
  }

  if (loading && !state) {
    return (
      <section className="admin-section storage-section" aria-labelledby="storage-heading">
        <div className="admin-section-heading">
          <div className="section-icon" aria-hidden="true"><Server size={22} /></div>
          <div><h2 id="storage-heading">{tr("存储感知", "Storage awareness")}</h2><p>{tr("正在读取磁盘信息…", "Reading disk information…")}</p></div>
        </div>
        <div className="admin-loading-row" role="status"><LoaderCircle className="spin" size={22} />{tr("正在读取存储状态…", "Reading storage status…")}</div>
      </section>
    );
  }

  const host = state?.host;
  const dataVolume = state?.data_volume;
  const cacheInfo = state?.cache;
  const backupInfo = state?.backups;
  const hostPercent = host && host.total_bytes > 0 ? Math.min(100, (host.used_bytes / host.total_bytes) * 100) : 0;
  const worstAlert = state?.alerts?.find((alert) => alert.level === "critical") || state?.alerts?.[0];

  return (
    <section className="admin-section storage-section" aria-labelledby="storage-heading">
      <div className="admin-section-heading">
        <div className={`section-icon ${worstAlert ? (worstAlert.level === "critical" ? "warning" : "success") : "success"}`} aria-hidden="true"><Server size={22} /></div>
        <div>
          <h2 id="storage-heading">{tr("存储感知", "Storage awareness")}</h2>
          <p>{tr("服务器磁盘、数据卷、缓存与备份占用总览；低空间时自动向管理员信箱发送警报。", "Overview of server disk, data volume, cache and backup usage. Alerts are sent to the admin mailbox when space runs low.")}</p>
        </div>
        <span className="metric-value">{state?.alerts?.length || 0} {tr("条警报", "alerts")} · {state?.recommendations?.length || 0} {tr("条建议", "suggestions")}</span>
      </div>
      <div className="admin-section-heading-actions">
        <button className="button secondary" disabled={loading} onClick={() => void load()} type="button">
          <RefreshCw className={loading ? "spin" : ""} size={17} />{tr("刷新", "Refresh")}
        </button>
      </div>
      {error && <p className="form-error" role="alert"><CircleAlert size={17} />{error}</p>}
      {notice && <p className="form-success" role="status"><Check size={17} />{notice}</p>}

      <div className="storage-grid">
        {host && (
          <div className="storage-card">
            <div className="storage-card-heading"><HardDrive size={18} /><strong>{tr("服务器磁盘", "Server disk")}</strong><small>{state?.probe_path || ""}</small></div>
            <div className="storage-meter" aria-label={`${tr("磁盘已用", "Disk used")} ${hostPercent.toFixed(1)}%`}>
              <span style={{ width: `${hostPercent}%` }} />
            </div>
            <div className="storage-stats">
              <span>{tr("总量", "Total")} {formatBytes(host.total_bytes)}</span>
              <span>{tr("已用", "Used")} {formatBytes(host.used_bytes)}</span>
              <strong className={host.free_bytes < 10 * 1000 ** 3 ? "storage-critical" : ""}>{tr("剩余", "Free")} {formatBytes(host.free_bytes)} · {host.percent_used.toFixed(1)}%</strong>
            </div>
          </div>
        )}
        {dataVolume && (
          <div className="storage-card">
            <div className="storage-card-heading"><Database size={18} /><strong>{tr("SavedStream 数据卷", "SavedStream data volume")}</strong><small>{state?.data_volume_path || ""}</small></div>
            <div className="storage-meter" aria-label={`${tr("数据卷已用", "Volume used")} ${dataVolume.percent_used}%`}>
              <span style={{ width: `${Math.min(100, dataVolume.percent_used)}%` }} />
            </div>
            <div className="storage-stats">
              <span>{tr("总量", "Total")} {formatBytes(dataVolume.total_bytes)}</span>
              <span>{tr("已用", "Used")} {formatBytes(dataVolume.used_bytes)}</span>
              <span>{tr("剩余", "Free")} {formatBytes(dataVolume.free_bytes)}</span>
            </div>
          </div>
        )}
        <div className="storage-card">
          <div className="storage-card-heading"><Archive size={18} /><strong>{tr("部署备份", "Deployment backups")}</strong><small>{backupInfo?.count || 0} {tr("份", "backups")}</small></div>
          <div className="storage-stats">
            <span>{tr("占用", "Usage")} {formatBytes(backupInfo?.bytes || 0)}</span>
            <span>{backupInfo?.configured ? (backupInfo?.writable ? tr("可管理", "Manageable") : tr("不可写", "Not writable")) : tr("未挂载", "Not mounted")}</span>
            <button className="button secondary" onClick={onGoToBackups} type="button">{tr("管理备份", "Manage backups")}</button>
          </div>
        </div>
        <div className="storage-card">
          <div className="storage-card-heading"><Gauge size={18} /><strong>{tr("媒体缓存", "Media cache")}</strong><small>{cacheInfo?.files || 0} {tr("个文件", "files")}</small></div>
          <div className="storage-meter" aria-label={`${tr("缓存已用", "Cache used")} ${cacheInfo?.percent_used || 0}%`}>
            <span style={{ width: `${Math.min(100, cacheInfo?.percent_used || 0)}%` }} />
          </div>
          <div className="storage-stats">
            <span>{tr("占用", "Usage")} {formatBytes(cacheInfo?.bytes || 0)}</span>
            <span>{tr("上限", "Limit")} {formatBytes(cacheInfo?.limit_bytes || 0)}</span>
            <span>{tr("数据库", "Database")} {formatBytes(state?.database_bytes || 0)}</span>
          </div>
        </div>
      </div>

      {(state?.alerts?.length || 0) > 0 && (
        <div className="storage-alerts">
          <h3>{tr("当前警报", "Active alerts")}</h3>
          {state?.alerts?.map((alert: StorageAlert) => (
            <div className={`storage-alert ${alert.level}`} key={alert.code}>
              <CircleAlert size={17} />
              <span><strong>{alert.title}</strong><small>{alert.message}</small></span>
            </div>
          ))}
        </div>
      )}

      {(state?.recommendations?.length || 0) > 0 ? (
        <div className="storage-recommendations">
          <h3>{tr("优化建议", "Optimization suggestions")}</h3>
          {state?.recommendations?.map((entry: StorageRecommendation) => (
            <div className="storage-recommendation" key={entry.code}>
              <span><strong>{entry.title}</strong><small>{entry.message}</small></span>
              <button className="button secondary" disabled={Boolean(busy)} onClick={() => void runRecommendation(entry)} type="button">
                {busy === entry.code ? <LoaderCircle className="spin" size={16} /> : entry.action === "cleanup_backups" ? <Archive size={16} /> : <Trash2 size={16} />}
                {entry.action === "cleanup_backups" ? tr("前往备份管理", "Go to backups") : tr("立即清理", "Clear now")}
              </button>
            </div>
          ))}
        </div>
      ) : (
        <p className="muted storage-healthy"><Check size={16} />{tr("存储状态良好，暂无优化建议。", "Storage looks healthy. No suggestions right now.")}</p>
      )}
    </section>
  );
}

function BackupAdminPanel() {
  const { tr } = useI18n();
  const [state, setState] = useState<BackupListResponse>({ configured: false, writable: false, items: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [keep, setKeep] = useState(3);
  const [preview, setPreview] = useState<{ removed: BackupEntry[]; freed_bytes: number } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await api<BackupListResponse>("/api/admin/backups");
      setState(result);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    // The backup list loads once per panel mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function toggleExpand(stamp: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(stamp)) next.delete(stamp); else next.add(stamp);
      return next;
    });
  }

  async function removeBackup(entry: BackupEntry) {
    const confirmed = window.confirm(tr(
      `确认删除备份 ${entry.stamp} 吗？将同时删除该次部署的代码备份与数据卷备份，此操作不可恢复。`,
      `Delete backup ${entry.stamp}? Both the code backup and the volume backups are removed and cannot be recovered.`,
    ));
    if (!confirmed) return;
    setBusy(entry.stamp);
    setError("");
    setNotice("");
    try {
      await api(`/api/admin/backups/${encodeURIComponent(entry.stamp)}`, { method: "DELETE" });
      setNotice(tr("备份已删除。", "Backup deleted."));
      setPreview(null);
      await load();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  async function previewCleanup() {
    setBusy("preview");
    setError("");
    setNotice("");
    setPreview(null);
    try {
      const result = await api<{ removed: BackupEntry[]; freed_bytes: number }>("/api/admin/backups/cleanup", {
        method: "POST",
        body: JSON.stringify({ keep, dry_run: true }),
      });
      setPreview(result);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  async function runCleanup() {
    const confirmed = window.confirm(tr(
      "确认按保留策略删除旧备份吗？仅保留最近 N 份，此操作不可恢复。",
      "Delete old backups according to the retention policy? Only the newest N backups are kept.",
    ));
    if (!confirmed) return;
    setBusy("cleanup");
    setError("");
    setNotice("");
    try {
      const result = await api<{ removed: BackupEntry[]; freed_bytes: number }>("/api/admin/backups/cleanup", {
        method: "POST",
        body: JSON.stringify({ keep, dry_run: false }),
      });
      setPreview(null);
      setNotice(tr(
        `已清理 ${result.removed.length} 份旧备份，释放 ${formatBytes(result.freed_bytes)}。`,
        `Removed ${result.removed.length} old backups, freeing ${formatBytes(result.freed_bytes)}.`,
      ));
      await load();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  const totalBytes = (state.items || []).reduce((sum, item) => sum + item.size_bytes, 0);

  return (
    <section className="admin-section backup-section" aria-labelledby="backup-heading">
      <div className="admin-section-heading">
        <div className={`section-icon ${state.configured && state.writable ? "success" : "warning"}`} aria-hidden="true"><Archive size={22} /></div>
        <div>
          <h2 id="backup-heading">{tr("部署备份管理", "Deployment backups")}</h2>
          <p>{tr("每次部署会在服务器 /opt/tube/backups 生成 code-<stamp> 与 volumes-<stamp>，这里可以查看、删除并执行保留策略。", "Every deployment creates code-<stamp> and volumes-<stamp> under /opt/tube/backups on the server. Review, delete, and apply retention here.")}</p>
        </div>
        <span className="metric-value">{state.items.length} {tr("份备份", "backups")} · {formatBytes(totalBytes)}</span>
      </div>
      <div className="admin-section-heading-actions">
        <button className="button secondary" disabled={loading} onClick={() => void load()} type="button">
          <RefreshCw className={loading ? "spin" : ""} size={17} />{tr("刷新", "Refresh")}
        </button>
      </div>
      {error && <p className="form-error" role="alert"><CircleAlert size={17} />{error}</p>}
      {notice && <p className="form-success" role="status"><Check size={17} />{notice}</p>}

      {!state.configured ? (
        <div className="admin-empty-state">
          <Archive size={32} />
          <h3>{tr("备份目录未挂载", "Backup directory is not mounted")}</h3>
          <p>{tr("请更新 docker-compose.yml（挂载 ../backups:/backups）并重新部署后使用本页面。", "Update docker-compose.yml to mount ../backups:/backups and redeploy before using this page.")}</p>
        </div>
      ) : !state.writable ? (
        <div className="admin-empty-state">
          <CircleAlert size={32} />
          <h3>{tr("备份目录当前不可写", "Backup directory is not writable")}</h3>
          <p>{tr("容器用户无法删除备份。请在服务器执行：chown 10001:10001 /opt/tube/backups && chmod 700 /opt/tube/backups（新版部署脚本会自动处理）。", "The container user cannot delete backups. Run: chown 10001:10001 /opt/tube/backups && chmod 700 /opt/tube/backups (newer deploy scripts do this automatically).")}</p>
        </div>
      ) : loading ? (
        <div className="admin-loading-row" role="status">
          <LoaderCircle className="spin" size={22} />{tr("正在读取备份…", "Reading backups…")}
        </div>
      ) : state.items.length === 0 ? (
        <p className="muted">{tr("还没有部署备份。", "No deployment backups yet.")}</p>
      ) : (
        <div className="backup-list">
          {state.items.map((entry) => {
            const open = expanded.has(entry.stamp);
            return (
              <div className="backup-row" key={entry.stamp}>
                <button className="backup-row-main" onClick={() => toggleExpand(entry.stamp)} type="button" aria-expanded={open}>
                  <span className="backup-identity">
                    <strong>{entry.stamp}</strong>
                    <small>{entry.modified_at ? formatBackupTime(entry.modified_at) : tr("时间未知", "Unknown time")} · {tr("代码", "Code")} {formatBytes(entry.code_size_bytes)}{entry.has_volumes ? ` · ${tr("数据卷", "volumes")} ${formatBytes(entry.volume_size_bytes)}` : ""}</small>
                  </span>
                  <span className="backup-meta">
                    <small>{formatBytes(entry.size_bytes)} · {entry.file_count} {tr("个文件", "files")}</small>
                    <ChevronDown className={open ? "backup-chevron open" : "backup-chevron"} size={16} />
                  </span>
                </button>
                {open && (
                  <div className="backup-detail">
                    {entry.has_code && (
                      <div>
                        <strong>{tr("代码备份", "Code backup")}</strong>
                        <code>{entry.code_files.length ? entry.code_files.join(" · ") : tr("（空）", "(empty)")}</code>
                      </div>
                    )}
                    {entry.has_volumes && (
                      <div>
                        <strong>{tr("数据卷备份", "Volume backups")}</strong>
                        <code>{entry.volume_files.length ? entry.volume_files.join(" · ") : tr("（空）", "(empty)")}</code>
                      </div>
                    )}
                  </div>
                )}
                <div className="backup-row-actions">
                  <button
                    className="button danger-ghost"
                    disabled={Boolean(busy) || !state.writable || !entry.deletable}
                    onClick={() => void removeBackup(entry)}
                    type="button"
                    title={!entry.deletable ? tr("备份目录不可写，无法删除", "Backup directory is not writable") : tr("删除该次部署的全部备份", "Delete all backups of this deployment")}
                  >
                    {busy === entry.stamp ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}{tr("删除", "Delete")}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {state.configured && state.writable && (
        <div className="backup-retention">
          <h3>{tr("保留策略", "Retention policy")}</h3>
          <p>{tr("只保留最近", "Keep only the newest")} <input type="number" min={1} max={20} value={keep} onChange={(event) => setKeep(Number(event.target.value))} aria-label={tr("保留份数", "Keep count")} /> {tr("份备份，其余全部删除（代码 + 数据卷）。", "backups and delete everything older (code + volumes).")}</p>
          <div className="button-row">
            <button className="button secondary" disabled={Boolean(busy) || !state.items.length} onClick={() => void previewCleanup()} type="button">
              {busy === "preview" ? <LoaderCircle className="spin" size={16} /> : <Search size={16} />}{tr("预览可释放空间", "Preview freed space")}
            </button>
            <button className="button danger" disabled={Boolean(busy) || !state.items.length} onClick={() => void runCleanup()} type="button">
              {busy === "cleanup" ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}{tr("执行清理", "Run cleanup")}
            </button>
          </div>
          {preview && (
            <div className="backup-preview" role="status">
              {preview.removed.length === 0 ? (
                <p className="muted">{tr("没有需要清理的备份。", "Nothing to clean up.")}</p>
              ) : (
                <>
                  <p>{tr("将删除以下", "The following will be removed")} {preview.removed.length} {tr("份备份，释放", "backups, freeing")} <strong>{formatBytes(preview.freed_bytes)}</strong>：</p>
                  <code>{preview.removed.map((item) => item.stamp).join(" · ")}</code>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function formatBackupTime(value: string): string {
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(translateNow("zh-CN", "en-US"), {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  } catch {
    return value;
  }
}

function DashboardStat({ icon, label, value, detail, tone = "default" }: { icon: ReactNode; label: string; value: string; detail: string; tone?: "default" | "warning" }) {
  return (
    <div className={`dashboard-stat ${tone}`}>
      <div className="dashboard-stat-icon">{icon}</div>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function TrafficChart({ items, loading }: { items: TrafficSeriesPoint[]; loading: boolean }) {
  if (loading && !items.length) return <div className="traffic-chart-placeholder"><LoaderCircle className="spin" size={24} />{translateNow("读取流量曲线…", "Loading traffic chart…")}</div>;
  if (!items.length) return <div className="traffic-chart-placeholder"><Activity size={24} />{translateNow("暂无流量数据", "No traffic data")}</div>;
  const max = Math.max(1, ...items.flatMap((item) => [item.bytes_in, item.bytes_out]));
  const points = (field: "bytes_in" | "bytes_out") => items.map((item, index) => {
    const x = items.length === 1 ? 50 : (index / (items.length - 1)) * 100;
    const y = 96 - (Number(item[field]) / max) * 78;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  return (
    <div className="traffic-chart" aria-label={translateNow("流量上下行曲线", "Upload and download traffic chart")}>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img">
        {[18, 44, 70, 96].map((y) => <line key={y} x1="0" x2="100" y1={y} y2={y} className="chart-grid-line" />)}
        <polyline points={points("bytes_in")} className="chart-line inbound" />
        <polyline points={points("bytes_out")} className="chart-line outbound" />
      </svg>
      <div className="traffic-chart-labels"><span>{items[0].bucket_start}</span><span>{items[Math.floor(items.length / 2)].bucket_start}</span><span>{items[items.length - 1].bucket_start}</span></div>
    </div>
  );
}

function AdminSectionCard({ storageKey, title, summary, icon, children, defaultOpen = true, autoOpenWhen = false }: { storageKey: string; title: string; summary: string; icon: ReactNode; children: ReactNode; defaultOpen?: boolean; autoOpenWhen?: boolean }) {
  const [storedPreference, setStoredPreference] = useState<string | null>(() => window.localStorage.getItem(`savedstream-admin-card:${storageKey}`));
  const [open, setOpen] = useState(() => storedPreference === null ? defaultOpen : storedPreference === "open");
  useEffect(() => {
    if (autoOpenWhen && storedPreference === null) setOpen(true);
  }, [autoOpenWhen, storedPreference]);
  function toggle() {
    setOpen((current) => {
      const next = !current;
      const preference = next ? "open" : "closed";
      window.localStorage.setItem(`savedstream-admin-card:${storageKey}`, preference);
      setStoredPreference(preference);
      return next;
    });
  }
  return (
    <section className={`admin-section admin-collapsible-card ${open ? "is-open" : "is-closed"}`}>
      <div className="admin-card-heading">
        <div className="section-icon" aria-hidden="true">{icon}</div>
        <div><h2>{title}</h2><p>{summary}</p></div>
        <button className="admin-card-toggle" onClick={toggle} type="button" aria-expanded={open}>
          {open ? translateNow("收起", "Collapse") : translateNow("展开", "Expand")}<ChevronDown size={17} aria-hidden="true" />
        </button>
      </div>
      {open && <div className="admin-card-body">{children}</div>}
    </section>
  );
}

function ReviewQueuePanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { tr } = useI18n();
  const [items, setItems] = useState<MediaItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState("");
  const [reason, setReason] = useState("");
  const [banSubmitter, setBanSubmitter] = useState(false);
  const [error, setError] = useState("");

  const quickReasons = [
    [tr("色情或露骨内容", "Sexual or explicit content"), tr("色情或露骨内容，违反平台规则。", "Sexual or explicit content violates the platform rules.")],
    [tr("违法或危险内容", "Illegal or dangerous content"), tr("违法或危险内容，不符合平台规则。", "Illegal or dangerous content is not allowed.")],
    [tr("版权或侵权内容", "Copyright infringement"), tr("疑似版权或侵权内容，请勿公开传播。", "Suspected copyright infringement; public distribution is not allowed.")],
    [tr("恶意软件或危险文件", "Malware or dangerous file"), tr("恶意软件或危险文件，已从媒体库删除。", "Malware or a dangerous file was removed from the media library.")],
    [tr("垃圾内容或滥用", "Spam or abuse"), tr("垃圾内容或滥用行为，不符合共创平台规则。", "Spam or abusive content violates the community rules.")],
  ] as const;

  async function load() {
    try {
      const result = await api<{ items: MediaItem[] }>("/api/admin/media/review?status=pending&limit=200");
      setItems(result.items);
      setSelected(new Set());
    } catch (value) {
      setError(errorMessage(value));
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5_000);
    return () => window.clearInterval(timer);
  }, []);

  async function review(decision: "approved" | "rejected" | "deleted") {
    const targets = items.filter((item) => selected.has(mediaKey(item)));
    if (!targets.length) return;
    setBusy(decision);
    setError("");
    try {
      await api("/api/admin/media/review/bulk", {
        method: "POST",
        body: JSON.stringify({
          decision,
          reason: reason.trim() || null,
          ban_submitter: banSubmitter,
          items: targets.map((item) => ({ account_id: item.account_id, message_id: item.id })),
        }),
      });
      setReason("");
      setBanSubmitter(false);
      await load();
      await onRefresh();
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy("");
    }
  }

  const pendingCount = items.length;
  return (
    <AdminSectionCard
      storageKey="review-queue"
      title={tr("公开媒体审核", "Public media review")}
      summary={`${tr("公开申请必须经过管理员确认，拒绝后仍归上传者私人媒体。", "Public requests require administrator approval; rejected media stays private to its owner.")} · ${pendingCount} ${tr("项待审核", "pending")}`}
      icon={<Shield size={22} />}
      defaultOpen={pendingCount > 0}
      autoOpenWhen={pendingCount > 0}
    >
      {error && <p className="form-error" role="alert"><CircleAlert size={17} />{error}</p>}
      <div className="review-toolbar">
        <span className="metric-value">{pendingCount} {tr("项待审核", "pending")}</span>
        <button className="button ghost" disabled={Boolean(busy)} onClick={() => void load()} type="button" title={tr("刷新审核队列", "Refresh review queue")}>
          <RefreshCw className={busy ? "spin" : ""} size={17} />{tr("刷新", "Refresh")}
        </button>
        <input
          type="text"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          maxLength={1000}
          placeholder={tr("可选审核说明/拒绝理由", "Optional review or rejection reason")}
        />
        <div className="review-reason-presets" aria-label={tr("快速拒绝理由", "Quick rejection reasons")}>
          {quickReasons.map(([label, value]) => (
            <button className="chip-button" key={label} onClick={() => setReason(value)} type="button">{label}</button>
          ))}
        </div>
        <label className="review-ban-toggle">
          <input type="checkbox" checked={banSubmitter} onChange={(event) => setBanSubmitter(event.target.checked)} />
          {tr("同时封禁上传者", "Ban uploader too")}
        </label>
        <button className="button secondary" disabled={!selected.size || Boolean(busy)} onClick={() => void review("approved")} type="button">
          {busy === "approved" ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />}{tr("通过", "Approve")}
        </button>
        <button className="button danger-ghost" disabled={!selected.size || Boolean(busy)} onClick={() => void review("rejected")} type="button">
          {busy === "rejected" ? <LoaderCircle className="spin" size={17} /> : <X size={17} />}{tr("拒绝", "Reject")}
        </button>
        <button className="button danger" disabled={!selected.size || Boolean(busy)} onClick={() => void review("deleted")} type="button">
          {busy === "deleted" ? <LoaderCircle className="spin" size={17} /> : <Trash2 size={17} />}{tr("删除违规内容", "Delete violation")}
        </button>
      </div>
      {items.length === 0 ? (
        <p className="muted">{tr("暂无待审核媒体。", "There are no pending media reviews.")}</p>
      ) : (
        <div className="review-queue-list">
          {items.map((item) => {
            const key = mediaKey(item);
            return (
              <label className="review-queue-row" key={key}>
                <input
                  type="checkbox"
                  checked={selected.has(key)}
                  onChange={() => setSelected((current) => {
                    const next = new Set(current);
                    if (next.has(key)) next.delete(key); else next.add(key);
                    return next;
                  })}
                  aria-label={`${tr("选择", "Select")} ${item.filename}`}
                />
                <AdminMediaThumbnail item={item} />
                <span className="review-queue-identity">
                  <strong>{item.title || item.filename}</strong>
                  <small>{item.filename} · {formatBytes(item.size)} · {item.account_id}</small>
                  <small>{tr("上传者", "Uploader")}: {item.submitter_telegram_user_id || tr("未知", "unknown")} · {item.review_batch_id ? `${tr("媒体组", "album")} ${item.review_batch_id.slice(0, 8)}` : tr("单文件", "single file")}</small>
                </span>
              </label>
            );
          })}
        </div>
      )}
    </AdminSectionCard>
  );
}

function HelperRateLimitPanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { tr } = useI18n();
  const defaults: HelperRateLimit = {
    per_user_files_24h: 20,
    per_user_bytes_24h: 10_000_000_000,
    per_user_concurrent: 2,
    max_file_bytes: 2_000_000_000,
    global_files_per_minute: 30,
    max_album_items: 10,
    max_album_bytes: 2_000_000_000,
  };
  const [value, setValue] = useState<HelperRateLimit>(settings.helper_rate_limit || defaults);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => setValue(settings.helper_rate_limit || defaults), [settings.helper_rate_limit]);

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError(""); setNotice("");
    try {
      await api<HelperRateLimit>("/api/admin/helper-bot/rate-limit", {
        method: "PUT",
        body: JSON.stringify(value),
      });
      setNotice(tr("辅助 Bot 限流设置已保存。", "Helper bot rate limits saved."));
      await onRefresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  const gb = (bytes: number) => Number((bytes / 1_000_000_000).toFixed(2));
  return (
    <AdminSectionCard
      storageKey="helper-rate-limit"
      title={tr("辅助 Bot 限流", "Helper bot rate limits")}
      summary={tr("在复制到 userbot 前预占额度，防止文件和并发请求滥用。", "Reserve quota before copying to the userbot to prevent abuse.")}
      icon={<Gauge size={22} />}
      defaultOpen={false}
    >
      {error && <p className="form-error" role="alert"><CircleAlert size={17} />{error}</p>}
      {notice && <p className="form-success" role="status"><Check size={17} />{notice}</p>}
      <form className="traffic-settings-card helper-rate-form" onSubmit={(event) => void save(event)}>
        <div className="traffic-number-grid">
          <label><span>{tr("单用户 24 小时文件数", "Files per user / 24h")}</span><input type="number" min={1} value={value.per_user_files_24h} onChange={(e) => setValue({ ...value, per_user_files_24h: Number(e.target.value) })} /></label>
          <label><span>{tr("单用户 24 小时大小 GB", "User bytes / 24h (GB)")}</span><input type="number" min={0.01} step={0.01} value={gb(value.per_user_bytes_24h)} onChange={(e) => setValue({ ...value, per_user_bytes_24h: Number(e.target.value) * 1_000_000_000 })} /></label>
          <label><span>{tr("单用户并发任务", "Concurrent tasks per user")}</span><input type="number" min={1} value={value.per_user_concurrent} onChange={(e) => setValue({ ...value, per_user_concurrent: Number(e.target.value) })} /></label>
          <label><span>{tr("单文件大小 GB", "Max file size (GB)")}</span><input type="number" min={0.01} step={0.01} value={gb(value.max_file_bytes)} onChange={(e) => setValue({ ...value, max_file_bytes: Number(e.target.value) * 1_000_000_000 })} /></label>
          <label><span>{tr("全 Bot 每分钟文件数", "Global files / minute")}</span><input type="number" min={1} value={value.global_files_per_minute} onChange={(e) => setValue({ ...value, global_files_per_minute: Number(e.target.value) })} /></label>
          <label><span>{tr("媒体组文件数", "Files per album")}</span><input type="number" min={1} value={value.max_album_items} onChange={(e) => setValue({ ...value, max_album_items: Number(e.target.value) })} /></label>
          <label><span>{tr("媒体组大小 GB", "Album size (GB)")}</span><input type="number" min={0.01} step={0.01} value={gb(value.max_album_bytes)} onChange={(e) => setValue({ ...value, max_album_bytes: Number(e.target.value) * 1_000_000_000 })} /></label>
        </div>
        <div className="admin-sticky-actions">
          <div className="admin-save-float">
            <button className="button primary" disabled={busy} type="submit">{busy ? <LoaderCircle className="spin" size={17} /> : <Save size={17} />}{tr("保存限流设置", "Save rate limits")}</button>
          </div>
        </div>
      </form>
    </AdminSectionCard>
  );
}

function PublicAlbumPanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { tr } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [generatedKey, setGeneratedKey] = useState("");

  async function toggle(enabled: boolean) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api("/api/admin/public-album", { method: "PUT", body: JSON.stringify({ enabled }) });
      await onRefresh();
      setNotice(enabled ? tr("公开相册已开启。", "Public album enabled.") : tr("公开相册已关闭，现有公开会话会立即失效。", "Public album disabled. Existing public sessions are invalidated immediately."));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function generateKey() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api<{ key: string }>("/api/admin/public-album/key", { method: "POST" });
      setGeneratedKey(result.key);
      setNotice(tr("新密钥只在此处显示一次，请立即复制保存。", "The new key is shown only once. Copy and save it now."));
      await onRefresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function copyKey() {
    if (!generatedKey) return;
    await navigator.clipboard?.writeText(generatedKey);
    setNotice(tr("公开访问密钥已复制。", "Public access key copied."));
  }

  return (
    <section className="admin-section public-album-section" aria-labelledby="public-album-heading">
      <div className="admin-section-heading">
        <div className={`section-icon ${settings.public_album_enabled ? "success" : "warning"}`} aria-hidden="true"><Globe2 size={22} /></div>
        <div><h2 id="public-album-heading">{tr("公开相册访问", "Public album access")}</h2><p>{tr("公开媒体必须经过 Telegram 审批、访问密钥和管理员总开关。", "Public media requires Telegram approval, an access key, and the administrator master switch.")}</p></div>
        <span className={`status-pill ${settings.public_album_enabled ? "success" : "warning"}`}><span aria-hidden="true" />{settings.public_album_enabled ? tr("已开启", "Enabled") : tr("已关闭", "Disabled")}</span>
      </div>
      {error && <p className="form-error" role="alert"><CircleAlert size={17} />{error}</p>}
      {notice && <p className="form-success"><Check size={17} />{notice}</p>}
      <div className="public-album-actions">
        <div className="status-detail">
          <strong>{settings.public_key_configured ? `${tr("访问密钥已配置（版本", "Access key configured (version")} ${settings.public_key_version}${tr("）", ")")}` : tr("尚未配置访问密钥", "Access key is not configured")}</strong>
          <p>{tr("数据库只保存密钥哈希，轮换后旧密钥立即失效。", "Only the key hash is stored. Rotating the key invalidates the old key immediately.")}</p>
        </div>
        <div className="button-row">
          <button className="button secondary" disabled={busy || !settings.public_key_configured} onClick={() => void toggle(!settings.public_album_enabled)} type="button">
            {busy ? <LoaderCircle className="spin" size={18} /> : <Globe2 size={18} />}
            {settings.public_album_enabled ? tr("关闭公开访问", "Disable public access") : tr("允许公开访问", "Enable public access")}
          </button>
          <button className="button primary" disabled={busy} onClick={() => void generateKey()} type="button">
            {busy ? <LoaderCircle className="spin" size={18} /> : <RotateCcw size={18} />}
            {settings.public_key_configured ? tr("轮换密钥", "Rotate key") : tr("生成密钥", "Generate key")}
          </button>
        </div>
      </div>
      {generatedKey && (
        <div className="generated-secret" role="status">
          <code>{generatedKey}</code>
          <button className="button secondary" onClick={() => void copyKey()} type="button"><Copy size={17} />{tr("复制", "Copy")}</button>
        </div>
      )}
    </section>
  );
}

function MediaIndexPanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { tr } = useI18n();
  const [account, setAccount] = useState(() => settings.accounts.find((item) => item.state === "authenticated")?.id || settings.accounts[0]?.id || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function sync(full: boolean) {
    if (!account) return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/admin/media/sync?account=${encodeURIComponent(account)}&full=${full ? "true" : "false"}`, { method: "POST" });
      await onRefresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-section index-section" aria-labelledby="media-index-heading">
      <div className="admin-section-heading">
        <div className="section-icon" aria-hidden="true"><Database size={22} /></div>
        <div><h2 id="media-index-heading">{tr("媒体索引与时间相册", "Media index and timeline album")}</h2><p>{tr("列表和时间线读取本地 SQLite，不会每次扫描 Telegram。", "Lists and timelines read local SQLite instead of scanning Telegram on every request.")}</p></div>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="index-controls">
        <select value={account} onChange={(event) => setAccount(event.target.value)} aria-label={tr("选择索引账号", "Select account to index")}>
          {settings.accounts.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select>
        <button className="button secondary" disabled={busy || !account} onClick={() => void sync(false)} type="button"><RefreshCw className={busy ? "spin" : ""} size={17} />{tr("增量同步", "Incremental sync")}</button>
        <button className="button primary" disabled={busy || !account} onClick={() => void sync(true)} type="button"><Database size={17} />{tr("重建索引", "Rebuild index")}</button>
      </div>
      <div className="index-status-list">
        {settings.media_sync.length === 0 ? <p className="muted">{tr("尚未开始索引。", "Indexing has not started.")}</p> : settings.media_sync.map((state) => (
          <div className="coordination-row" key={state.account_id}>
            <span><strong>{state.account_id}</strong><small>{state.status} · {tr("已索引", "Indexed")} {state.indexed_count} {tr("项", "items")}</small></span>
            <small>{state.error || state.last_sync_at || tr("等待任务", "Waiting for job")}</small>
          </div>
        ))}
      </div>
    </section>
  );
}

function UploadPanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { tr } = useI18n();
  const authenticatedAccounts = settings.accounts.filter((item) => item.state === "authenticated");
  const [account, setAccount] = useState(authenticatedAccounts[0]?.id || "");
  const [file, setFile] = useState<File | null>(null);
  const [browserProgress, setBrowserProgress] = useState(0);
  const [job, setJob] = useState<UploadJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const xhrRef = useRef<XMLHttpRequest | null>(null);

  useEffect(() => {
    if (!authenticatedAccounts.some((item) => item.id === account)) {
      setAccount(authenticatedAccounts[0]?.id || "");
    }
  }, [account, authenticatedAccounts]);

  async function upload() {
    if (!file || !account) return;
    setBusy(true);
    setError("");
    setBrowserProgress(0);
    setJob(null);
    const form = new FormData();
    form.append("file", file);
    await new Promise<void>((resolve) => {
      const xhr = new XMLHttpRequest();
      xhrRef.current = xhr;
      xhr.open("POST", `/api/admin/uploads?account=${encodeURIComponent(account)}`);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) setBrowserProgress((event.loaded / event.total) * 100);
      };
      xhr.onerror = () => {
        xhrRef.current = null;
        setError(tr("上传请求失败", "Upload request failed"));
        setBusy(false);
        resolve();
      };
      xhr.onabort = () => {
        xhrRef.current = null;
        setError(tr("上传已取消", "Upload canceled"));
        setBusy(false);
        resolve();
      };
      xhr.onload = () => {
        xhrRef.current = null;
        try {
          const payload = JSON.parse(xhr.responseText);
          if (xhr.status < 200 || xhr.status >= 300) throw new Error(payload?.detail?.code || tr("上传失败", "Upload failed"));
          void watchUpload(String(payload.id));
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : tr("上传失败", "Upload failed"));
          setBusy(false);
          resolve();
        }
        resolve();
      };
      xhr.send(form);
    });
  }

  async function cancelUpload() {
    const xhr = xhrRef.current;
    if (xhr && xhr.readyState !== XMLHttpRequest.DONE) {
      xhr.abort();
      return;
    }
    if (!job || ["completed", "failed", "cancelled"].includes(job.status)) return;
    try {
      const next = await api<UploadJob>(`/api/admin/uploads/${encodeURIComponent(job.id)}`, { method: "DELETE" });
      setJob(next);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function watchUpload(id: string) {
    try {
      for (;;) {
        const next = await api<UploadJob>(`/api/admin/uploads/${encodeURIComponent(id)}`);
        setJob(next);
        if (["completed", "failed", "cancelled"].includes(next.status)) break;
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
      }
      await onRefresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-section upload-section" aria-labelledby="upload-heading">
      <div className="admin-section-heading">
        <div className="section-icon" aria-hidden="true"><Upload size={22} /></div>
        <div><h2 id="upload-heading">{tr("私人相册上传", "Private album upload")}</h2><p>{tr("文件通过指定 userbot 写入 Saved Messages，默认保持私人。", "Files are written to Saved Messages through the selected userbot and remain private by default.")}</p></div>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="upload-controls">
        <select value={account} onChange={(event) => setAccount(event.target.value)} aria-label={tr("上传到账号", "Upload to account")}>
          {authenticatedAccounts.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select>
        <input type="file" onChange={(event) => setFile(event.target.files?.[0] || null)} />
        <button className="button primary" disabled={busy || !file || !account} onClick={() => void upload()} type="button">
          {busy ? <LoaderCircle className="spin" size={18} /> : <Upload size={18} />}{tr("上传并入库", "Upload and ingest")}
        </button>
        {busy && !job && <button className="button danger-ghost" onClick={() => void cancelUpload()} type="button"><X size={16} />{tr("取消上传", "Cancel upload")}</button>}
      </div>
      {file && <p className="muted">{tr("浏览器上传进度：", "Browser upload progress: ")}{browserProgress.toFixed(0)}% · {file.name} · {formatBytes(file.size)}</p>}
      {job && <div className="upload-job-status"><strong>{job.phase}</strong><span>{job.progress.toFixed(1)}%</span>{job.error && <small>{job.error}</small>}{busy && !["completed", "failed", "cancelled"].includes(job.status) && <button className="button danger-ghost" onClick={() => void cancelUpload()} type="button"><X size={16} />{tr("取消", "Cancel")}</button>}</div>}
      {settings.upload_jobs.slice(0, 5).map((item) => <div className="coordination-row" key={item.id}><span><strong>{item.filename}</strong><small>{item.account_id} · {item.status}</small></span><small>{item.progress.toFixed(0)}%</small></div>)}
    </section>
  );
}

function AccessUsersPanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { tr } = useI18n();
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const pending = settings.access_users.filter((user) => user.status === "pending").length;

  async function setUserStatus(telegramUserId: string, status: "approved" | "disabled" | "denied") {
    setBusy(`${telegramUserId}-${status}`);
    setError("");
    try {
      await api(`/api/admin/access-users/${encodeURIComponent(telegramUserId)}`, {
        method: "PUT",
        body: JSON.stringify({ status }),
      });
      await onRefresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="admin-section access-users-section" aria-labelledby="access-users-heading">
      <div className="admin-section-heading">
        <div className={`section-icon ${pending ? "warning" : "success"}`} aria-hidden="true"><Users size={22} /></div>
        <div><h2 id="access-users-heading">{tr("媒体访问用户", "Media access users")}</h2><p>{tr("Telegram 身份绑定与管理员审批", "Telegram identity binding and administrator approval")}</p></div>
        <span className="metric-value">{pending ? `${pending} ${tr("个待审批", "pending")}` : `${settings.access_users.length} ${tr("个用户", "users")}`}</span>
      </div>
      {error && <p className="form-error" role="alert"><CircleAlert size={17} />{error}</p>}
      {settings.access_users.length === 0 ? (
        <div className="admin-empty-state compact">
          <Users size={28} />
          <p>{tr("用户通过辅助 Bot 的", "Users appear here after signing in through the helper bot's")} <code>/web</code> {tr("登录后会出现在这里。", "command.")}</p>
        </div>
      ) : (
        <div className="access-users-list">
          {settings.access_users.map((user) => (
            <div className="coordination-row access-user-row" key={user.telegram_user_id}>
              <span>
                <strong>{user.display_name}{user.username ? ` · @${user.username}` : ""}</strong>
                <small>Telegram {user.telegram_user_id} · {user.account_id} · {accessUserStatusLabel(user.status)}</small>
              </span>
              <div className="access-user-actions">
                {user.status !== "approved" && (
                  <button className="button secondary" disabled={Boolean(busy)} onClick={() => void setUserStatus(user.telegram_user_id, "approved")} type="button">
                    {busy === `${user.telegram_user_id}-approved` ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}{tr("批准", "Approve")}
                  </button>
                )}
                {user.status === "pending" && (
                  <button className="button danger-ghost" disabled={Boolean(busy)} onClick={() => void setUserStatus(user.telegram_user_id, "denied")} type="button">{tr("拒绝", "Deny")}</button>
                )}
                {user.status === "approved" && (
                  <button className="button danger-ghost" disabled={Boolean(busy)} onClick={() => void setUserStatus(user.telegram_user_id, "disabled")} type="button">{tr("禁用", "Disable")}</button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function accessUserStatusLabel(status: "pending" | "approved" | "disabled" | "denied") {
  return {
    pending: translateNow("待审批", "Pending"),
    approved: translateNow("已批准", "Approved"),
    disabled: translateNow("已禁用", "Disabled"),
    denied: translateNow("已拒绝", "Denied"),
  }[status];
}

function CoordinationPanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
  const { tr } = useI18n();
  const [botToken, setBotToken] = useState("");
  const [selectedAccount, setSelectedAccount] = useState(settings.accounts[0]?.id || "");
  const [inviteCode, setInviteCode] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [profileId, setProfileId] = useState("");
  const [profileLabel, setProfileLabel] = useState("");
  const [apiId, setApiId] = useState("");
  const [apiHash, setApiHash] = useState("");
  const [loginAccountId, setLoginAccountId] = useState("");
  const [loginStatus, setLoginStatus] = useState<{ state: string; qr_url?: string | null; expires_at?: string | null; error?: string | null } | null>(null);
  const loginCanvasRef = useRef<HTMLCanvasElement>(null);

  async function run(key: string, action: () => Promise<unknown>) {
    setBusy(key); setError("");
    try { await action(); await onRefresh(); } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(""); }
  }

  useEffect(() => {
    if (!loginStatus?.qr_url || !loginCanvasRef.current) return;
    QRCode.toCanvas(loginCanvasRef.current, loginStatus.qr_url, {
      width: 236,
      margin: 2,
      color: { dark: "#111111", light: "#ffffff" },
      errorCorrectionLevel: "M",
    }).catch((reason) => setError(errorMessage(reason)));
  }, [loginStatus?.qr_url]);

  useEffect(() => {
    if (!loginAccountId || !loginStatus || ["authenticated", "error"].includes(loginStatus.state)) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await api<typeof loginStatus>(`/api/admin/accounts/${encodeURIComponent(loginAccountId)}/login`);
        setLoginStatus(next);
        if (next.state === "authenticated") {
          window.clearInterval(timer);
          await onRefresh();
        }
      } catch (reason) {
        setError(errorMessage(reason));
      }
    }, 1800);
    return () => window.clearInterval(timer);
  }, [loginAccountId, loginStatus?.state, onRefresh]);

  async function addAccount(event: FormEvent) {
    event.preventDefault();
    await run("account", async () => {
      const created = await api<{ id: string }>("/api/admin/accounts", {
        method: "POST",
        body: JSON.stringify({ id: profileId, label: profileLabel || profileId, api_id: Number(apiId), api_hash: apiHash, session: "" }),
      });
      const login = await api<typeof loginStatus>(`/api/admin/accounts/${encodeURIComponent(created.id)}/login/qr`, { method: "POST" });
      setLoginAccountId(created.id);
      setLoginStatus(login);
      setProfileId(""); setProfileLabel(""); setApiId(""); setApiHash("");
    });
  }

  async function cancelLogin() {
    if (!loginAccountId) return;
    try {
      await api(`/api/admin/accounts/${encodeURIComponent(loginAccountId)}/login`, { method: "DELETE" });
    } finally {
      setLoginAccountId("");
      setLoginStatus(null);
    }
  }

  function beginAccountLogin(accountId: string) {
    setBusy(`login-${accountId}`); setError("");
    void api(`/api/admin/accounts/${encodeURIComponent(accountId)}/login`, { method: "DELETE" })
      .catch(() => undefined)
      .then(() => api<typeof loginStatus>(`/api/admin/accounts/${encodeURIComponent(accountId)}/login/qr`, { method: "POST" }))
      .then((login) => {
        setLoginAccountId(accountId);
        setLoginStatus(login);
      })
      .catch((reason) => setError(errorMessage(reason)))
      .finally(() => setBusy(""));
  }

  return (
    <section className="admin-section coordination-section" aria-labelledby="coordination-heading">
      <div className="admin-section-heading">
        <div className="section-icon" aria-hidden="true"><Users size={22} /></div>
        <div><h2 id="coordination-heading">{tr("多账号协调", "Multi-account coordination")}</h2><p>{tr("辅助 Bot、提交绑定与入库队列", "Helper bot, submitter bindings, and ingest queue")}</p></div>
        <span className="metric-value">{settings.accounts.length} {tr("个账号", "accounts")}</span>
      </div>
      {error && <p className="form-error" role="alert"><CircleAlert size={17} />{error}</p>}

      <div className="coordination-grid">
        <form className="coordination-pane" id="managed-account-form" onSubmit={(event) => { void addAccount(event); }}>
          <h3><UserPlus size={18} />{tr("新增托管账号", "Add managed account")}</h3>
          <div className="compact-fields">
            <input required value={profileId} onChange={(e) => setProfileId(e.target.value)} placeholder={tr("账号 ID", "Account ID")} pattern="[a-zA-Z0-9_-]+" />
            <input required value={profileLabel} onChange={(e) => setProfileLabel(e.target.value)} placeholder={tr("显示名称", "Display name")} />
            <input required inputMode="numeric" value={apiId} onChange={(e) => setApiId(e.target.value)} placeholder="Telegram API ID" />
            <input required type="password" value={apiHash} onChange={(e) => setApiHash(e.target.value)} placeholder="Telegram API Hash" />
          </div>
          <button className="button secondary" disabled={busy === "account"} type="submit">{busy === "account" ? <LoaderCircle className="spin" size={17} /> : <UserPlus size={17} />}{tr("添加并扫码连接", "Add and connect by QR")}</button>
        </form>

        <form className="coordination-pane" onSubmit={(event) => { event.preventDefault(); void run("bot", async () => { await api("/api/admin/helper-bot", { method: "PUT", body: JSON.stringify({ token: botToken }) }); setBotToken(""); }); }}>
          <h3><Bot size={18} />{tr("辅助 Bot", "Helper bot")}</h3>
          <p>{settings.helper_bot.configured ? `${tr("已连接", "Connected")} @${settings.helper_bot.username || "unknown"}` : tr("尚未配置", "Not configured")}</p>
          <input required type="password" value={botToken} onChange={(e) => setBotToken(e.target.value)} placeholder={settings.helper_bot.token || "BotFather token"} />
          <button className="button secondary" disabled={busy === "bot"} type="submit">{busy === "bot" ? <LoaderCircle className="spin" size={17} /> : <Bot size={17} />}{tr("验证并保存", "Verify and save")}</button>
        </form>

        <div className="coordination-pane">
          <h3><Copy size={18} />{tr("绑定邀请码", "Binding invite code")}</h3>
          <select value={selectedAccount} onChange={(e) => setSelectedAccount(e.target.value)}>
            {settings.accounts.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.state}</option>)}
          </select>
          <button className="button secondary" disabled={!selectedAccount || busy === "invite"} onClick={() => void run("invite", async () => { const result = await api<{ code: string }>(`/api/admin/accounts/${encodeURIComponent(selectedAccount)}/invites`, { method: "POST" }); setInviteCode(result.code); })} type="button">{tr("生成 24 小时邀请码", "Generate 24-hour invite")}</button>
          {inviteCode && <code className="invite-code">/bind {inviteCode}</code>}
        </div>
      </div>

      <div className="coordination-lists">
        <div>
          <h3>{tr("托管账号", "Managed accounts")}</h3>
          {settings.accounts.length === 0 ? <p className="muted">{tr("请先添加账号", "Add an account first")}</p> : settings.accounts.map((item) => (
            <div className="coordination-row" key={item.id}>
              <span><strong>{item.label}</strong><small>{item.state}{item.error ? ` · ${item.error}` : ""}</small></span>
              {item.state !== "authenticated" && (
                <button className="button secondary" disabled={busy === `login-${item.id}`} onClick={() => void beginAccountLogin(item.id)} type="button">
                  {busy === `login-${item.id}` ? <LoaderCircle className="spin" size={17} /> : <KeyRound size={17} />}{tr("扫码连接", "Connect by QR")}
                </button>
              )}
            </div>
          ))}
        </div>
        <div><h3>{tr("提交者绑定", "Submitter bindings")}</h3>{settings.bindings.length === 0 ? <p className="muted">{tr("暂无绑定", "No bindings")}</p> : settings.bindings.map((item) => <div className="coordination-row" key={item.telegram_user_id}><span><strong>{item.telegram_user_id}</strong><small>{item.account_id}</small></span><button className="icon-button" title={tr("撤销绑定", "Revoke binding")} aria-label={tr("撤销绑定", "Revoke binding")} onClick={() => void run(`binding-${item.telegram_user_id}`, () => api("/api/admin/bindings", { method: "DELETE", body: JSON.stringify({ submitter_id: item.telegram_user_id }) }))} type="button"><Trash2 size={17} /></button></div>)}</div>
        <details className="ingest-jobs">
          <summary>
            <span>{tr("最近入库任务", "Recent ingest jobs")} <small>{settings.ingest_jobs.length}</small></span>
            <ChevronDown size={18} aria-hidden="true" />
          </summary>
          <div className="ingest-jobs-list">
            {settings.ingest_jobs.length === 0 ? <p className="muted">{tr("暂无任务", "No jobs")}</p> : settings.ingest_jobs.slice(0, 12).map((job) => <div className="coordination-row" key={job.id}><span><strong>#{job.id} · {job.status}</strong><small>{job.account_id}{job.error ? ` · ${job.error}` : ""}</small></span>{["failed", "delivered", "retry_wait"].includes(job.status) && <button className="icon-button" title={tr("重试", "Retry")} aria-label={tr("重试任务", "Retry job")} onClick={() => void run(`job-${job.id}`, () => api(`/api/admin/ingest/jobs/${job.id}/retry`, { method: "POST" }))} type="button"><RotateCcw size={17} /></button>}</div>)}
          </div>
        </details>
      </div>
      {loginAccountId && loginStatus && (
        <div className="viewer-backdrop" role="dialog" aria-modal="true" aria-labelledby="account-login-title">
          <div className="viewer account-login-dialog">
            <div className="viewer-topbar">
              <h2 id="account-login-title">{tr("连接 Telegram 账号", "Connect Telegram account")}</h2>
              <button className="icon-button" title={tr("关闭", "Close")} aria-label={tr("关闭", "Close")} onClick={() => void cancelLogin()} type="button"><X size={18} /></button>
            </div>
            <div className="account-login-body">
              {loginStatus.qr_url && loginStatus.state === "qr_login" ? <canvas ref={loginCanvasRef} aria-label={tr("Telegram 登录二维码", "Telegram login QR code")} /> : null}
              <p className="status-line waiting"><span />{loginStatus.state === "authenticated" ? tr("连接成功", "Connected") : loginStatus.state === "error" ? (loginStatus.error || tr("连接失败", "Connection failed")) : tr("请使用 Telegram 扫描二维码", "Scan the QR code with Telegram")}</p>
              {loginStatus.state !== "authenticated" && <button className="button secondary" onClick={() => void cancelLogin()} type="button">{tr("取消", "Cancel")}</button>}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function StatusPill({ auth }: { auth: TelegramAuthStatus }) {
  return (
    <span className={`status-pill ${auth.authenticated ? "success" : "warning"}`}>
      <span aria-hidden="true" />
      {auth.authenticated ? translateNow("已连接", "Connected") : telegramStateLabel(auth.state)}
    </span>
  );
}

function AdminMediaThumbnail({ item }: { item: MediaItem }) {
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  const handleThumbnailError = useCallback(() => setThumbnailFailed(true), []);
  return (
    <div className={`admin-media-thumb kind-${item.kind}`}>
      {item.thumbnail_url && !thumbnailFailed ? (
        <ThumbnailImage
          key={item.thumbnail_url}
          src={item.thumbnail_url}
          alt=""
          onError={handleThumbnailError}
        />
      ) : (
        <FileKindIcon kind={item.kind} mime={item.mime_type} />
      )}
    </div>
  );
}

function isAdminSessionError(reason: unknown): boolean {
  return reason instanceof ApiError && reason.status === 401;
}

function telegramStateLabel(state: string): string {
  const labels: Record<string, string> = {
    authenticated: translateNow("已连接", "Connected"),
    unauthenticated: translateNow("需要登录", "Sign-in required"),
    waiting_for_scan: translateNow("等待扫码", "Waiting for scan"),
    password_required: translateNow("需要两步验证", "Two-step verification required"),
    qr_expired: translateNow("二维码已过期", "QR code expired"),
    configuration_required: translateNow("缺少配置", "Configuration missing"),
    error: translateNow("连接异常", "Connection error"),
  };
  return labels[state] || translateNow("未连接", "Not connected");
}

function formatCacheLimit(gigabytes: number): string {
  return `${gigabytes.toLocaleString("zh-CN", { maximumFractionDigits: 1 })} GB`;
}

function formatRate(bytesPerSecond: number): string {
  return `${formatTrafficBytes(Math.max(0, bytesPerSecond))}/s`;
}

function formatTrafficBytes(bytes: number): string {
  if (bytes < 1000) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1000;
  let index = 0;
  while (value >= 1000 && index < units.length - 1) {
    value /= 1000;
    index += 1;
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

function cachePercent(settings: AdminSettings): string {
  if (settings.cache_max_gb <= 0) return "0.0";
  return Math.min(100, (settings.cache_bytes / (settings.cache_max_gb * 1024 ** 3)) * 100).toFixed(1);
}

function formatAdminMediaMeta(item: MediaItem): string {
  const date = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(new Date(item.date));
  return `${date} · ${formatBytes(item.size)} · ${item.kind.toUpperCase()}`;
}

function mediaKey(item: MediaItem): string {
  return `${item.account_id}:${item.id}`;
}
