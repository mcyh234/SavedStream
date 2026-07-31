import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  ArrowLeft,
  Bot,
  Check,
  ChevronDown,
  CircleAlert,
  Copy,
  Database,
  Eye,
  EyeOff,
  Gauge,
  HardDrive,
  KeyRound,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  Pencil,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  ShieldCheck,
  ShieldOff,
  Trash2,
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
import type {
  AdminSettings,
  MediaItem,
  MediaPage,
  PublicStatus,
  TelegramAuthStatus,
} from "./types";

type AdminPhase = "checking" | "guest" | "ready" | "error";

const MEDIA_PAGE_SIZE = 24;
const MIN_CACHE_GB = 0.5;
const MAX_CACHE_GB = 200;

interface AdminPageProps {
  onSessionChanged?: () => void | Promise<void>;
}

export default function AdminPage({ onSessionChanged }: AdminPageProps) {
  const [phase, setPhase] = useState<AdminPhase>("checking");
  const [bootstrapError, setBootstrapError] = useState("");
  const [settings, setSettings] = useState<AdminSettings | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [telegramLoginOpen, setTelegramLoginOpen] = useState(false);

  const [cacheMaxGb, setCacheMaxGb] = useState(10);
  const [accessRestricted, setAccessRestricted] = useState(false);
  const [viewerKey, setViewerKey] = useState("");
  const [viewerKeyVisible, setViewerKeyVisible] = useState(false);
  const [clearViewerKey, setClearViewerKey] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [settingsNotice, setSettingsNotice] = useState("");

  const [cacheClearing, setCacheClearing] = useState(false);
  const [media, setMedia] = useState<MediaItem[]>([]);
  const [mediaCursor, setMediaCursor] = useState<number | null>(null);
  const [mediaHasMore, setMediaHasMore] = useState(false);
  const [mediaLoading, setMediaLoading] = useState(false);
  const [mediaLoadingMore, setMediaLoadingMore] = useState(false);
  const [mediaError, setMediaError] = useState("");
  const [mediaQuery, setMediaQuery] = useState("");
  const [activeMediaQuery, setActiveMediaQuery] = useState("");
  const [titleDrafts, setTitleDrafts] = useState<Record<number, string>>({});
  const [savingTitleId, setSavingTitleId] = useState<number | null>(null);
  const [savedTitleId, setSavedTitleId] = useState<number | null>(null);

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
          "请先配置 TELEGRAM_API_ID、TELEGRAM_API_HASH 与 ADMIN_KEY，然后重启容器。",
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
    setCacheMaxGb(next.cache_max_gb);
    setAccessRestricted(next.access_restricted);
    setViewerKey("");
    setClearViewerKey(false);
  }

  async function loadDashboard(search = activeMediaQuery) {
    setPageLoading(true);
    setSettingsError("");
    try {
      const next = await api<AdminSettings>("/api/admin/settings");
      applySettings(next);
      if (next.telegram.authenticated) {
        await loadMediaPage(null, search);
      } else {
        setMedia([]);
        setMediaCursor(null);
        setMediaHasMore(false);
        setMediaError("");
      }
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
    setSettingsNotice("");
    try {
      const wasTelegramAuthenticated = settings?.telegram.authenticated ?? false;
      const next = await api<AdminSettings>("/api/admin/settings");
      applySettings(next);
      if (!next.telegram.authenticated) {
        setMedia([]);
        setMediaCursor(null);
        setMediaHasMore(false);
      } else if (!wasTelegramAuthenticated) {
        await loadMediaPage(null);
      }
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

  async function loadMediaPage(cursor: number | null, search = activeMediaQuery) {
    cursor === null ? setMediaLoading(true) : setMediaLoadingMore(true);
    setMediaError("");
    try {
      const params = new URLSearchParams({
        limit: String(MEDIA_PAGE_SIZE),
        order: "newest",
        kind: "all",
        q: search,
      });
      if (cursor !== null) params.set("cursor", String(cursor));
      const page = await api<MediaPage>(`/api/media?${params}`);
      setMedia((current) => cursor === null ? page.items : [...current, ...page.items]);
      setMediaCursor(page.next_cursor);
      setMediaHasMore(page.has_more);
      setTitleDrafts((current) => {
        const next = cursor === null ? {} : { ...current };
        for (const item of page.items) next[item.id] = item.local_title || "";
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

  async function handleAdminAuthenticated() {
    setPhase("ready");
    await syncOuterStatus();
    await loadDashboard("");
  }

  async function handleTelegramAuthenticated() {
    setTelegramLoginOpen(false);
    await syncOuterStatus();
    await loadDashboard(activeMediaQuery);
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
        "重新登录会注销当前 Telegram 会话。确认继续吗？",
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

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextViewerKey = viewerKey.trim();
    const existingKeyWillRemain = Boolean(settings?.viewer_key_configured && !clearViewerKey);
    setSettingsError("");
    setSettingsNotice("");

    if (nextViewerKey && nextViewerKey.length < 8) {
      setSettingsError("访客口令至少需要 8 个字符。");
      return;
    }
    if (accessRestricted && !existingKeyWillRemain && nextViewerKey.length < 8) {
      setSettingsError("开启访问限制前，请设置至少 8 个字符的访客口令。");
      return;
    }

    setSettingsSaving(true);
    try {
      await api<{ ok: boolean }>("/api/admin/settings", {
        method: "PUT",
        body: JSON.stringify({
          cache_max_gb: cacheMaxGb,
          access_restricted: accessRestricted,
          viewer_key: nextViewerKey || null,
          clear_viewer_key: clearViewerKey,
        }),
      });
      const next = await api<AdminSettings>("/api/admin/settings");
      applySettings(next);
      await syncOuterStatus();
      setSettingsNotice("设置已保存。");
    } catch (reason) {
      setSettingsError(errorMessage(reason));
    } finally {
      setSettingsSaving(false);
    }
  }

  async function clearCache() {
    if (!window.confirm("确认清空全部本地媒体缓存吗？正在播放的媒体可能需要重新拉取。")) {
      return;
    }
    setCacheClearing(true);
    setSettingsError("");
    setSettingsNotice("");
    try {
      await api<{ ok: boolean }>("/api/admin/cache", { method: "DELETE" });
      const next = await api<AdminSettings>("/api/admin/settings");
      setSettings(next);
      setSettingsNotice("本地缓存已清空。");
    } catch (reason) {
      setSettingsError(errorMessage(reason));
    } finally {
      setCacheClearing(false);
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
    const title = (titleDrafts[item.id] || "").trim();
    setSavingTitleId(item.id);
    setSavedTitleId(null);
    setMediaError("");
    try {
      await api<{ ok: boolean }>(`/api/admin/media/${item.id}`, {
        method: "PUT",
        body: JSON.stringify({ title }),
      });
      setMedia((current) => current.map((currentItem) => currentItem.id === item.id
        ? { ...currentItem, local_title: title || null, title: title || currentItem.original_title }
        : currentItem));
      setTitleDrafts((current) => ({ ...current, [item.id]: title }));
      setSavedTitleId(item.id);
    } catch (reason) {
      setMediaError(errorMessage(reason));
    } finally {
      setSavingTitleId(null);
    }
  }

  const cacheUsagePercent = useMemo(() => {
    if (!settings || settings.cache_max_gb <= 0) return 0;
    return Math.min(100, (settings.cache_bytes / (settings.cache_max_gb * 1024 ** 3)) * 100);
  }, [settings]);

  if (phase === "checking") {
    return (
      <CenterShell icon={<LoaderCircle className="spin" size={30} />} title="正在验证管理员会话">
        <p className="gate-copy" role="status">正在读取 SavedStream 管理状态…</p>
      </CenterShell>
    );
  }

  if (phase === "error") {
    return (
      <CenterShell icon={<CircleAlert size={30} />} title="无法打开管理后台">
        <p className="form-error" role="alert">{bootstrapError}</p>
        <button className="button secondary wide" onClick={() => void bootstrap()} type="button">
          <RefreshCw size={18} />重新尝试
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
          <ArrowLeft size={18} />返回管理后台
        </button>
        <TelegramLogin onAuthenticated={() => void handleTelegramAuthenticated()} />
      </div>
    );
  }

  if (!settings) {
    return (
      <CenterShell
        icon={settingsError ? <CircleAlert size={30} /> : <LoaderCircle className="spin" size={30} />}
        title={settingsError ? "无法载入管理后台" : "正在载入管理后台"}
      >
        {settingsError ? (
          <>
            <p className="form-error" role="alert">{settingsError}</p>
            <button className="button secondary wide" onClick={() => void loadDashboard()} type="button">
              <RefreshCw size={18} />重新尝试
            </button>
          </>
        ) : (
          <p className="gate-copy" role="status">正在读取缓存、隐私与媒体设置…</p>
        )}
      </CenterShell>
    );
  }

  const telegram = settings.telegram;
  const cacheSummary = `${formatBytes(settings.cache_bytes)} / ${formatCacheLimit(settings.cache_max_gb)}`;

  return (
    <div className="admin-page">
      <header className="admin-topbar">
        <a className="brand" href="/" aria-label="返回 SavedStream 媒体库">
          <span className="brand-mark"><Play size={16} fill="currentColor" /></span>
          <span>SavedStream</span>
        </a>
        <div className="admin-title-block">
          <span>控制台</span>
          <strong>媒体与隐私设置</strong>
        </div>
        <div className="admin-topbar-actions">
          <a className="button secondary" href="/">
            <ArrowLeft size={18} />返回媒体库
          </a>
          <button className="icon-button" onClick={() => void handleAdminLogout()} type="button" title="退出管理员" aria-label="退出管理员">
            <LogOut size={20} />
          </button>
        </div>
      </header>

      <main className="admin-main" id="main-content">
        <div className="admin-heading">
          <div>
            <p className="admin-eyebrow">ADMIN SETTINGS</p>
            <h1>SavedStream 管理后台</h1>
          </div>
          <button className="button secondary" disabled={pageLoading} onClick={() => void refreshSettings()} type="button">
            <RefreshCw className={pageLoading ? "spin" : ""} size={18} />刷新状态
          </button>
        </div>

        <div className="admin-feedback" aria-live="polite">
          {settingsError && <p className="form-error" role="alert"><CircleAlert size={17} />{settingsError}</p>}
          {settingsNotice && <p className="form-success"><Check size={17} />{settingsNotice}</p>}
        </div>

        {!settings.accounts.some((item) => item.state === "authenticated") && (
          <section className="admin-section telegram-section" aria-labelledby="telegram-setup-heading">
            <div className="admin-section-heading">
              <div className="section-icon warning" aria-hidden="true"><KeyRound size={22} /></div>
              <div>
                <h2 id="telegram-setup-heading">{settings.accounts.length === 0 ? "配置 Telegram API" : "连接 Telegram 账号"}</h2>
                <p>{settings.accounts.length === 0 ? "尚未配置托管账号" : "托管账号尚未完成登录"}</p>
              </div>
              <span className="status-pill warning"><span aria-hidden="true" />需要配置</span>
            </div>
            <div className="telegram-status-body">
              <div className="status-detail">
                <strong>{settings.accounts.length === 0 ? "需要 Telegram API ID 和 API Hash" : "需要扫码完成账号登录"}</strong>
                <p>{settings.accounts.length === 0 ? "请在下方“新增托管账号”中填写凭据。凭据将保存在 TeleBox 数据卷中，无需修改 Docker 环境变量。" : "请在下方“托管账号”列表中点击扫码连接。"}</p>
              </div>
              <button className="button primary" onClick={() => document.getElementById("managed-account-form")?.scrollIntoView({ behavior: "smooth", block: "center" })} type="button">
                <KeyRound size={18} />{settings.accounts.length === 0 ? "立即配置" : "前往连接"}
              </button>
            </div>
          </section>
        )}

        {settings.accounts.some((item) => item.state === "authenticated") && (<section className="admin-section telegram-section" aria-labelledby="telegram-heading">
          <div className="admin-section-heading">
            <div className={`section-icon ${telegram.authenticated ? "success" : "warning"}`} aria-hidden="true">
              {telegram.authenticated ? <Wifi size={22} /> : <WifiOff size={22} />}
            </div>
            <div>
              <h2 id="telegram-heading">Telegram 连接</h2>
              <p>收藏夹读取会话</p>
            </div>
            <StatusPill auth={telegram} />
          </div>
          <div className="telegram-status-body">
            <div className="status-detail">
              <span>当前状态</span>
              <strong>{telegramStateLabel(telegram.state)}</strong>
              {telegram.error && <p role="alert">{telegram.error}</p>}
              {telegram.state === "configuration_required" && (
                <p>请先配置 Docker 环境变量 TELEGRAM_API_ID 与 TELEGRAM_API_HASH。</p>
              )}
            </div>
            <button
              className="button primary"
              disabled={pageLoading || telegram.state === "configuration_required"}
              onClick={() => document.getElementById("managed-account-form")?.scrollIntoView({ behavior: "smooth", block: "center" })}
              type="button"
            >
              {telegram.authenticated ? <RefreshCw size={18} /> : <KeyRound size={18} />}
              {telegram.authenticated ? "重新登录" : "扫码登录"}
            </button>
          </div>
        </section>)}

        <CoordinationPanel settings={settings} onRefresh={refreshSettings} />

        <form className="admin-settings-form" onSubmit={saveSettings}>
          <section className="admin-section cache-section" aria-labelledby="cache-heading">
            <div className="admin-section-heading">
              <div className="section-icon" aria-hidden="true"><HardDrive size={22} /></div>
              <div>
                <h2 id="cache-heading">本地缓存</h2>
                <p>限制媒体分块占用的磁盘空间</p>
              </div>
              <span className="metric-value">{cacheSummary}</span>
            </div>

            <div className="cache-meter" aria-label={`缓存已使用 ${cacheUsagePercent.toFixed(0)}%`}>
              <span style={{ width: `${cacheUsagePercent}%` }} />
            </div>
            <div className="cache-stats">
              <span><Database size={16} />{settings.cache_files.toLocaleString("zh-CN")} 个缓存文件</span>
              <span><Gauge size={16} />已使用 {cacheUsagePercent.toFixed(1)}%</span>
            </div>

            <div className="range-field">
              <div className="field-label-row">
                <label htmlFor="cache-limit">缓存上限</label>
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
                  setSettingsNotice("");
                }}
                aria-valuetext={formatCacheLimit(cacheMaxGb)}
              />
              <div className="range-bounds" aria-hidden="true">
                <span>{MIN_CACHE_GB} GB</span>
                <span>{MAX_CACHE_GB} GB</span>
              </div>
            </div>

            <button className="button danger-ghost" disabled={cacheClearing} onClick={() => void clearCache()} type="button">
              {cacheClearing ? <LoaderCircle className="spin" size={18} /> : <Trash2 size={18} />}
              清空缓存
            </button>
          </section>

          <section className="admin-section privacy-section" aria-labelledby="privacy-heading">
            <div className="admin-section-heading">
              <div className={`section-icon ${accessRestricted ? "success" : ""}`} aria-hidden="true">
                {accessRestricted ? <ShieldCheck size={22} /> : <ShieldOff size={22} />}
              </div>
              <div>
                <h2 id="privacy-heading">访问限制</h2>
                <p>保护媒体库、缩略图与播放地址</p>
              </div>
            </div>

            <label className="toggle-control" htmlFor="access-restricted">
              <span>
                <strong>要求访客口令</strong>
                <small>{accessRestricted ? "未登录访客将无法查看媒体" : "任何能访问站点的人都可以浏览媒体"}</small>
              </span>
              <input
                id="access-restricted"
                type="checkbox"
                checked={accessRestricted}
                onChange={(event) => {
                  setAccessRestricted(event.target.checked);
                  setSettingsNotice("");
                }}
              />
              <span className="toggle-track" aria-hidden="true"><span /></span>
            </label>

            <div className="form-field">
              <label htmlFor="viewer-key">
                访客口令
                {settings.viewer_key_configured && !clearViewerKey && <span className="configured-badge">已设置</span>}
              </label>
              <div className="input-with-actions">
                <LockKeyhole size={18} aria-hidden="true" />
                <input
                  id="viewer-key"
                  type={viewerKeyVisible ? "text" : "password"}
                  autoComplete="new-password"
                  value={viewerKey}
                  onChange={(event) => {
                    setViewerKey(event.target.value);
                    setClearViewerKey(false);
                    setSettingsNotice("");
                  }}
                  placeholder={settings.viewer_key_configured ? "留空以保留当前口令" : "至少 8 个字符"}
                  minLength={viewerKey ? 8 : undefined}
                />
                <button
                  className="field-icon-button"
                  type="button"
                  onClick={() => setViewerKeyVisible((visible) => !visible)}
                  aria-label={viewerKeyVisible ? "隐藏访客口令" : "显示访客口令"}
                  title={viewerKeyVisible ? "隐藏口令" : "显示口令"}
                >
                  {viewerKeyVisible ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
              <small className="field-help">新口令保存后，之前的访客登录会话将失效。</small>
            </div>

            {settings.viewer_key_configured && (
              <label className="check-control" htmlFor="clear-viewer-key">
                <input
                  id="clear-viewer-key"
                  type="checkbox"
                  checked={clearViewerKey}
                  onChange={(event) => {
                    setClearViewerKey(event.target.checked);
                    if (event.target.checked) setAccessRestricted(false);
                    setSettingsNotice("");
                  }}
                />
                <span>保存时移除现有访客口令</span>
              </label>
            )}
          </section>

          <div className="admin-form-actions">
            <button className="button primary" disabled={settingsSaving} type="submit">
              {settingsSaving ? <LoaderCircle className="spin" size={18} /> : <Save size={18} />}
              保存设置
            </button>
          </div>
        </form>

        <section className="admin-section media-title-section" aria-labelledby="media-title-heading">
          <div className="admin-section-heading media-title-heading-row">
            <div className="section-icon" aria-hidden="true"><Pencil size={22} /></div>
            <div>
              <h2 id="media-title-heading">本地标题</h2>
              <p>仅保存在 SavedStream，不修改 Telegram 原消息</p>
            </div>
            <form className="admin-media-search" onSubmit={searchMedia} role="search">
              <label className="sr-only" htmlFor="admin-media-query">搜索收藏夹媒体</label>
              <Search size={17} aria-hidden="true" />
              <input
                id="admin-media-query"
                type="search"
                value={mediaQuery}
                onChange={(event) => setMediaQuery(event.target.value)}
                placeholder="搜索媒体"
              />
              <button className="button secondary" disabled={!telegram.authenticated || mediaLoading} type="submit">
                搜索
              </button>
            </form>
          </div>

          {!telegram.authenticated ? (
            <div className="admin-empty-state">
              <WifiOff size={32} />
              <h3>连接 Telegram 后可编辑标题</h3>
              <button className="button primary" onClick={() => document.getElementById("managed-account-form")?.scrollIntoView({ behavior: "smooth", block: "center" })} type="button">
                <KeyRound size={18} />扫码登录
              </button>
            </div>
          ) : mediaLoading ? (
            <div className="admin-loading-row" role="status">
              <LoaderCircle className="spin" size={22} />正在读取收藏夹…
            </div>
          ) : mediaError && media.length === 0 ? (
            <div className="admin-empty-state" role="alert">
              <CircleAlert size={32} />
              <h3>无法读取媒体</h3>
              <p>{mediaError}</p>
              <button className="button secondary" onClick={() => void loadMediaPage(null)} type="button">
                <RefreshCw size={18} />重试
              </button>
            </div>
          ) : media.length === 0 ? (
            <div className="admin-empty-state">
              <Database size={32} />
              <h3>{activeMediaQuery ? "没有匹配的媒体" : "收藏夹中还没有媒体"}</h3>
              {mediaHasMore && mediaCursor !== null && (
                <button
                  className="button secondary"
                  disabled={mediaLoadingMore}
                  onClick={() => void loadMediaPage(mediaCursor)}
                  type="button"
                >
                  {mediaLoadingMore && <LoaderCircle className="spin" size={18} />}
                  继续查找
                </button>
              )}
            </div>
          ) : (
            <>
              {mediaError && <p className="form-error" role="alert">{mediaError}</p>}
              <div className="admin-media-list">
                {media.map((item) => (
                  <form className="admin-media-row" key={item.id} onSubmit={(event) => void saveTitle(event, item)}>
                    <AdminMediaThumbnail item={item} />
                    <div className="admin-media-identity">
                      <strong title={item.original_title}>{item.original_title}</strong>
                      <span>{formatAdminMediaMeta(item)}</span>
                    </div>
                    <div className="admin-title-field">
                      <label className="sr-only" htmlFor={`media-title-${item.id}`}>
                        {item.original_title} 的本地标题
                      </label>
                      <input
                        id={`media-title-${item.id}`}
                        type="text"
                        maxLength={200}
                        value={titleDrafts[item.id] ?? ""}
                        onChange={(event) => {
                          setTitleDrafts((current) => ({ ...current, [item.id]: event.target.value }));
                          if (savedTitleId === item.id) setSavedTitleId(null);
                        }}
                        placeholder="使用 Telegram 原标题"
                      />
                    </div>
                    <button
                      className="button secondary title-save-button"
                      disabled={savingTitleId === item.id}
                      type="submit"
                    >
                      {savingTitleId === item.id
                        ? <LoaderCircle className="spin" size={17} />
                        : savedTitleId === item.id
                          ? <Check size={17} />
                          : <Save size={17} />}
                      {savedTitleId === item.id ? "已保存" : "保存"}
                    </button>
                  </form>
                ))}
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
                    加载更多
                  </button>
                </div>
              )}
            </>
          )}
        </section>
      </main>
    </div>
  );
}

function CoordinationPanel({ settings, onRefresh }: { settings: AdminSettings; onRefresh: () => Promise<void> }) {
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
        <div><h2 id="coordination-heading">多账号协调</h2><p>辅助 Bot、提交绑定与入库队列</p></div>
        <span className="metric-value">{settings.accounts.length} 个账号</span>
      </div>
      {error && <p className="form-error" role="alert"><CircleAlert size={17} />{error}</p>}

      <div className="coordination-grid">
        <form className="coordination-pane" id="managed-account-form" onSubmit={(event) => { void addAccount(event); }}>
          <h3><UserPlus size={18} />新增托管账号</h3>
          <div className="compact-fields">
            <input required value={profileId} onChange={(e) => setProfileId(e.target.value)} placeholder="账号 ID" pattern="[a-zA-Z0-9_-]+" />
            <input required value={profileLabel} onChange={(e) => setProfileLabel(e.target.value)} placeholder="显示名称" />
            <input required inputMode="numeric" value={apiId} onChange={(e) => setApiId(e.target.value)} placeholder="Telegram API ID" />
            <input required type="password" value={apiHash} onChange={(e) => setApiHash(e.target.value)} placeholder="Telegram API Hash" />
          </div>
          <button className="button secondary" disabled={busy === "account"} type="submit">{busy === "account" ? <LoaderCircle className="spin" size={17} /> : <UserPlus size={17} />}添加并扫码连接</button>
        </form>

        <form className="coordination-pane" onSubmit={(event) => { event.preventDefault(); void run("bot", async () => { await api("/api/admin/helper-bot", { method: "PUT", body: JSON.stringify({ token: botToken }) }); setBotToken(""); }); }}>
          <h3><Bot size={18} />辅助 Bot</h3>
          <p>{settings.helper_bot.configured ? `已连接 @${settings.helper_bot.username || "unknown"}` : "尚未配置"}</p>
          <input required type="password" value={botToken} onChange={(e) => setBotToken(e.target.value)} placeholder={settings.helper_bot.token || "BotFather token"} />
          <button className="button secondary" disabled={busy === "bot"} type="submit">{busy === "bot" ? <LoaderCircle className="spin" size={17} /> : <Bot size={17} />}验证并保存</button>
        </form>

        <div className="coordination-pane">
          <h3><Copy size={18} />绑定邀请码</h3>
          <select value={selectedAccount} onChange={(e) => setSelectedAccount(e.target.value)}>
            {settings.accounts.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.state}</option>)}
          </select>
          <button className="button secondary" disabled={!selectedAccount || busy === "invite"} onClick={() => void run("invite", async () => { const result = await api<{ code: string }>(`/api/admin/accounts/${encodeURIComponent(selectedAccount)}/invites`, { method: "POST" }); setInviteCode(result.code); })} type="button">生成 24 小时邀请码</button>
          {inviteCode && <code className="invite-code">/bind {inviteCode}</code>}
        </div>
      </div>

      <div className="coordination-lists">
        <div>
          <h3>托管账号</h3>
          {settings.accounts.length === 0 ? <p className="muted">请先添加账号</p> : settings.accounts.map((item) => (
            <div className="coordination-row" key={item.id}>
              <span><strong>{item.label}</strong><small>{item.state}{item.error ? ` · ${item.error}` : ""}</small></span>
              {item.state !== "authenticated" && (
                <button className="button secondary" disabled={busy === `login-${item.id}`} onClick={() => void beginAccountLogin(item.id)} type="button">
                  {busy === `login-${item.id}` ? <LoaderCircle className="spin" size={17} /> : <KeyRound size={17} />}扫码连接
                </button>
              )}
            </div>
          ))}
        </div>
        <div><h3>提交者绑定</h3>{settings.bindings.length === 0 ? <p className="muted">暂无绑定</p> : settings.bindings.map((item) => <div className="coordination-row" key={item.telegram_user_id}><span><strong>{item.telegram_user_id}</strong><small>{item.account_id}</small></span><button className="icon-button" title="撤销绑定" aria-label="撤销绑定" onClick={() => void run(`binding-${item.telegram_user_id}`, () => api("/api/admin/bindings", { method: "DELETE", body: JSON.stringify({ submitter_id: item.telegram_user_id }) }))} type="button"><Trash2 size={17} /></button></div>)}</div>
        <details className="ingest-jobs">
          <summary>
            <span>最近入库任务 <small>{settings.ingest_jobs.length}</small></span>
            <ChevronDown size={18} aria-hidden="true" />
          </summary>
          <div className="ingest-jobs-list">
            {settings.ingest_jobs.length === 0 ? <p className="muted">暂无任务</p> : settings.ingest_jobs.slice(0, 12).map((job) => <div className="coordination-row" key={job.id}><span><strong>#{job.id} · {job.status}</strong><small>{job.account_id}{job.error ? ` · ${job.error}` : ""}</small></span>{["failed", "delivered", "retry_wait"].includes(job.status) && <button className="icon-button" title="重试" aria-label="重试任务" onClick={() => void run(`job-${job.id}`, () => api(`/api/admin/ingest/jobs/${job.id}/retry`, { method: "POST" }))} type="button"><RotateCcw size={17} /></button>}</div>)}
          </div>
        </details>
      </div>
      {loginAccountId && loginStatus && (
        <div className="viewer-backdrop" role="dialog" aria-modal="true" aria-labelledby="account-login-title">
          <div className="viewer account-login-dialog">
            <div className="viewer-topbar">
              <h2 id="account-login-title">连接 Telegram 账号</h2>
              <button className="icon-button" title="关闭" aria-label="关闭" onClick={() => void cancelLogin()} type="button"><X size={18} /></button>
            </div>
            <div className="account-login-body">
              {loginStatus.qr_url && loginStatus.state === "qr_login" ? <canvas ref={loginCanvasRef} aria-label="Telegram 登录二维码" /> : null}
              <p className="status-line waiting"><span />{loginStatus.state === "authenticated" ? "连接成功" : loginStatus.state === "error" ? (loginStatus.error || "连接失败") : "请使用 Telegram 扫描二维码"}</p>
              {loginStatus.state !== "authenticated" && <button className="button secondary" onClick={() => void cancelLogin()} type="button">取消</button>}
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
      {auth.authenticated ? "已连接" : telegramStateLabel(auth.state)}
    </span>
  );
}

function AdminMediaThumbnail({ item }: { item: MediaItem }) {
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  return (
    <div className={`admin-media-thumb kind-${item.kind}`}>
      {item.thumbnail_url && !thumbnailFailed ? (
        <ThumbnailImage
          src={item.thumbnail_url}
          alt=""
          onError={() => setThumbnailFailed(true)}
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
    authenticated: "已连接",
    unauthenticated: "需要登录",
    waiting_for_scan: "等待扫码",
    password_required: "需要两步验证",
    qr_expired: "二维码已过期",
    configuration_required: "缺少配置",
    error: "连接异常",
  };
  return labels[state] || "未连接";
}

function formatCacheLimit(gigabytes: number): string {
  return `${gigabytes.toLocaleString("zh-CN", { maximumFractionDigits: 1 })} GB`;
}

function formatAdminMediaMeta(item: MediaItem): string {
  const date = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(new Date(item.date));
  return `${date} · ${formatBytes(item.size)} · ${item.kind.toUpperCase()}`;
}
