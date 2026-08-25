import { FormEvent, useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import {
  CheckCircle2,
  Bot,
  Clock3,
  ExternalLink,
  KeyRound,
  LoaderCircle,
  LogIn,
  LogOut,
  LockKeyhole,
  QrCode,
  RefreshCw,
  ShieldCheck,
  UserPlus,
} from "lucide-react";
import { api, errorMessage } from "./api";
import { LanguageSelector, translateNow, useI18n } from "./I18n";
import type { TelegramAuthStatus } from "./types";

interface GateProps {
  onAuthenticated: () => void;
}

export function AdminKeyGate({ onAuthenticated }: GateProps) {
  const { tr } = useI18n();
  const [key, setKey] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      await api("/api/admin/login", {
        method: "POST",
        body: JSON.stringify({ key }),
      });
      setKey("");
      onAuthenticated();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }

  return (
    <CenterShell icon={<ShieldCheck size={30} />} title={tr("管理员验证", "Administrator verification")}>
      <form className="auth-form" onSubmit={submit}>
        <label htmlFor="admin-key">{tr("管理员密钥", "Administrator key")}</label>
        <div className="input-with-icon">
          <KeyRound size={18} aria-hidden="true" />
          <input
            id="admin-key"
            type="password"
            autoComplete="current-password"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            required
            autoFocus
          />
        </div>
        {error && <p className="form-error" role="alert">{error}</p>}
        <button className="button primary wide" disabled={loading} type="submit">
          {loading ? <LoaderCircle className="spin" size={18} /> : <LockKeyhole size={18} />}
          {tr("验证", "Verify")}
        </button>
      </form>
    </CenterShell>
  );
}

export type AccessStatus = "unauthenticated" | "pending" | "approved" | "disabled" | "denied" | "admin";
export type BindingSyncStatus = "pending" | "ready" | "error" | "not_required" | null;

interface AccountAuthGateProps extends GateProps {
  registrationEnabled: boolean;
  approvalRequired: boolean;
}

interface AuthLoginResponse {
  ok: boolean;
  status: AccessStatus;
  requires_device: boolean;
  challenge_id?: string;
  expires_at?: string;
  telegram_bot_link?: string | null;
}

interface ChallengeResponse {
  challenge_id: string;
  expires_at: string;
  telegram_bot_link?: string | null;
}

interface ChallengeState {
  kind: "register" | "device_verify";
  challengeId: string;
  expiresAt: string;
  telegramBotLink: string | null;
}

interface ChallengeStatusResponse {
  status: "pending" | "bound" | "verified";
  authenticated?: boolean;
}

export function accountStatusMessage(
  status: AccessStatus,
  bindingSyncStatus: BindingSyncStatus = null,
  publicAlbumEnabled = true,
  serviceUnavailable = false,
  approvalRequired = true,
): string {
  if (serviceUnavailable) return translateNow("媒体服务当前未连接 Telegram，请稍后重试或联系管理员。", "The media service is not currently connected to Telegram. Try again later or contact the administrator.");
  if (status === "pending" && approvalRequired) return translateNow("账号已完成 Telegram 身份确认，正在等待管理员审核。", "Your Telegram identity is confirmed and is waiting for administrator approval.");
  if (status === "pending") return translateNow("账号身份已确认。请在 Telegram 辅助 Bot 中使用管理员提供的邀请码执行 /bind；绑定托管账号后会自动获得访问权限。", "Your identity is confirmed. Use the administrator's invite code with /bind in the Telegram helper bot; access is granted automatically after the managed account is linked.");
  if (status === "disabled") return translateNow("该账号的媒体访问已被禁用，请联系管理员。", "Media access for this account has been disabled. Contact the administrator.");
  if (status === "denied") return translateNow("管理员未批准该账号访问媒体库。", "The administrator did not approve this account.");
  if (status === "approved" && bindingSyncStatus === "error") return translateNow("账号已获批准，但 Telegram 绑定同步失败，请联系管理员。", "Your account is approved, but Telegram binding synchronization failed. Contact the administrator.");
  if (status === "approved" && bindingSyncStatus !== "ready") return translateNow("账号已获批准，正在同步媒体账号绑定。", "Your account is approved and its media-account binding is being synchronized.");
  if (status === "approved" && !publicAlbumEnabled) return translateNow("账号已获批准，但管理员尚未开放媒体库。", "Your account is approved, but the administrator has not opened the media library yet.");
  return translateNow("当前账号暂时无法进入媒体库，请稍后重试。", "This account cannot enter the media library yet. Try again later.");
}

export function AccountAuthGate({ registrationEnabled, approvalRequired, onAuthenticated }: AccountAuthGateProps) {
  const { language, tr } = useI18n();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [passwordConfirm, setPasswordConfirm] = useState("");
  const [registrationKey, setRegistrationKey] = useState("");
  const [trustDevice, setTrustDevice] = useState(true);
  const [challenge, setChallenge] = useState<ChallengeState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!challenge) return;
    let stopped = false;
    let polling = false;

    const poll = async () => {
      if (polling || stopped) return;
      polling = true;
      try {
        if (challenge.kind === "register") {
          const next = await api<ChallengeStatusResponse>(`/api/auth/register/status?challenge_id=${encodeURIComponent(challenge.challengeId)}`);
          if (next.status === "bound" && !stopped) {
            const login = await api<AuthLoginResponse>("/api/auth/login", {
              method: "POST",
              body: JSON.stringify({ username: username.trim(), password, trust_device: trustDevice }),
            });
            if (login.requires_device && login.challenge_id && login.expires_at) {
              setChallenge({
                kind: "device_verify",
                challengeId: login.challenge_id,
                expiresAt: login.expires_at,
                telegramBotLink: login.telegram_bot_link || null,
              });
            } else {
              onAuthenticated();
            }
          }
        } else {
          const next = await api<ChallengeStatusResponse>(`/api/auth/device/verify/status?challenge_id=${encodeURIComponent(challenge.challengeId)}`);
          if ((next.authenticated || next.status === "verified") && !stopped) onAuthenticated();
        }
      } catch (reason) {
        if (!stopped) setError(errorMessage(reason));
      } finally {
        polling = false;
      }
    };

    void poll();
    const timer = window.setInterval(() => void poll(), 2200);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [challenge, onAuthenticated, password, trustDevice, username]);

  function switchMode(nextMode: "login" | "register") {
    setMode(nextMode);
    setChallenge(null);
    setError("");
    setPassword("");
    setPasswordConfirm("");
  }

  async function submitLogin(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      const result = await api<AuthLoginResponse>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username: username.trim(), password, trust_device: trustDevice }),
      });
      if (result.requires_device && result.challenge_id && result.expires_at) {
        setChallenge({
          kind: "device_verify",
          challengeId: result.challenge_id,
          expiresAt: result.expires_at,
          telegramBotLink: result.telegram_bot_link || null,
        });
      } else {
        onAuthenticated();
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }

  async function submitRegistration(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (password !== passwordConfirm) {
      setError(tr("两次输入的密码不一致", "The passwords do not match"));
      return;
    }
    setLoading(true);
    try {
      const result = await api<ChallengeResponse>("/api/auth/register/start", {
        method: "POST",
        body: JSON.stringify({
          username: username.trim(),
          password,
          registration_key: registrationKey.trim(),
          trust_device: trustDevice,
        }),
      });
      setChallenge({
        kind: "register",
        challengeId: result.challenge_id,
        expiresAt: result.expires_at,
        telegramBotLink: result.telegram_bot_link || null,
      });
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }

  if (challenge) {
    const expiresAt = new Date(challenge.expiresAt).toLocaleTimeString(language === "zh-CN" ? "zh-CN" : "en-US", { hour: "2-digit", minute: "2-digit" });
    return (
      <CenterShell icon={<Bot size={30} />} title={challenge.kind === "register" ? tr("绑定 Telegram", "Link Telegram") : tr("确认登录设备", "Verify this device")}>
        <div className="auth-challenge">
          <LoaderCircle className="spin" size={28} />
          <p className="gate-message">
            {challenge.kind === "register"
              ? tr("账号资料已提交。请通过 Telegram 完成身份绑定，绑定成功后本页会自动登录。", "Your account details were submitted. Link your Telegram identity and this page will sign you in automatically.")
              : tr("这是一个新浏览器。请通过 Telegram 确认本次登录，确认后会自动进入媒体库。", "This is a new browser. Confirm this sign-in through Telegram and the media library will open automatically.")}
          </p>
          {challenge.telegramBotLink ? (
            <a className="button primary wide" href={challenge.telegramBotLink} target="_blank" rel="noreferrer">
              <ExternalLink size={18} />{tr("打开 Telegram 确认", "Open Telegram to confirm")}
            </a>
          ) : (
            <p className="form-error" role="alert">{tr("辅助 Bot 尚未配置，暂时无法完成身份确认。", "The helper bot is not configured, so identity confirmation is currently unavailable.")}</p>
          )}
          <small>{tr(`确认链接将在 ${expiresAt} 左右失效。`, `The confirmation link expires around ${expiresAt}.`)}</small>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="button secondary wide" onClick={() => { setChallenge(null); setError(""); }} type="button">
            {tr("返回", "Back")}
          </button>
        </div>
      </CenterShell>
    );
  }

  return (
    <CenterShell icon={mode === "login" ? <LogIn size={30} /> : <UserPlus size={30} />} title={mode === "login" ? tr("登录 SavedStream", "Sign in to SavedStream") : tr("创建 SavedStream 账号", "Create a SavedStream account")}>
      {registrationEnabled && (
        <div className="auth-mode-tabs" role="tablist" aria-label={tr("账号操作", "Account action")}>
          <button className={mode === "login" ? "active" : ""} onClick={() => switchMode("login")} role="tab" aria-selected={mode === "login"} type="button">{tr("登录", "Sign in")}</button>
          <button className={mode === "register" ? "active" : ""} onClick={() => switchMode("register")} role="tab" aria-selected={mode === "register"} type="button">{tr("注册", "Register")}</button>
        </div>
      )}
      <form className="auth-form" onSubmit={mode === "login" ? submitLogin : submitRegistration}>
        <p className="gate-message">
          {mode === "login"
            ? tr("使用 SavedStream 用户名和密码登录。新浏览器可能需要通过 Telegram 再确认一次。", "Sign in with your SavedStream username and password. A new browser may require an additional Telegram confirmation.")
            : approvalRequired
              ? tr("注册后需要通过 Telegram 确认身份，并等待管理员审核。", "After registration, confirm your Telegram identity and wait for administrator approval.")
              : tr("注册后通过 Telegram 确认身份；使用 /bind 绑定托管账号后会自动获得访问权限。", "After registration, confirm your Telegram identity; access is granted automatically after you link a managed account with /bind.")}
        </p>
        <label htmlFor="account-username">{tr("用户名", "Username")}</label>
        <div className="input-with-icon">
          <KeyRound size={18} aria-hidden="true" />
          <input id="account-username" type="text" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} minLength={3} maxLength={32} pattern="[A-Za-z0-9_.-]{3,32}" required autoFocus />
        </div>
        <label htmlFor="account-password">{tr("密码", "Password")}</label>
        <div className="input-with-icon">
          <LockKeyhole size={18} aria-hidden="true" />
          <input id="account-password" type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} value={password} onChange={(event) => setPassword(event.target.value)} minLength={mode === "register" ? 12 : 1} maxLength={128} required />
        </div>
        {mode === "register" && (
          <>
            <label htmlFor="account-password-confirm">{tr("确认密码", "Confirm password")}</label>
            <div className="input-with-icon">
              <LockKeyhole size={18} aria-hidden="true" />
              <input id="account-password-confirm" type="password" autoComplete="new-password" value={passwordConfirm} onChange={(event) => setPasswordConfirm(event.target.value)} minLength={12} maxLength={128} required />
            </div>
            <label htmlFor="registration-key">{tr("注册密钥", "Registration key")}</label>
            <div className="input-with-icon">
              <KeyRound size={18} aria-hidden="true" />
              <input id="registration-key" type="password" autoComplete="off" value={registrationKey} onChange={(event) => setRegistrationKey(event.target.value)} required />
            </div>
          </>
        )}
        <label className="auth-checkbox">
          <input type="checkbox" checked={trustDevice} onChange={(event) => setTrustDevice(event.target.checked)} />
          <span>{tr("信任此浏览器 30 天", "Trust this browser for 30 days")}</span>
        </label>
        {!registrationEnabled && mode === "login" && <p className="auth-help">{tr("管理员当前未开放新用户注册。", "New user registration is currently disabled by the administrator.")}</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <button className="button primary wide" disabled={loading} type="submit">
          {loading ? <LoaderCircle className="spin" size={18} /> : mode === "login" ? <LogIn size={18} /> : <UserPlus size={18} />}
          {mode === "login" ? tr("登录", "Sign in") : tr("注册并绑定 Telegram", "Register and link Telegram")}
        </button>
      </form>
    </CenterShell>
  );
}

interface AccountStateGateProps extends GateProps {
  accessStatus: AccessStatus;
  bindingSyncStatus: BindingSyncStatus;
  publicAlbumEnabled: boolean;
  approvalRequired: boolean;
  serviceUnavailable?: boolean;
}

export function AccountStateGate({ accessStatus, bindingSyncStatus, publicAlbumEnabled, approvalRequired, serviceUnavailable = false, onAuthenticated }: AccountStateGateProps) {
  const { tr } = useI18n();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const waiting = accessStatus === "pending" || (accessStatus === "approved" && bindingSyncStatus === "pending");
  const title = serviceUnavailable
    ? tr("媒体服务暂不可用", "Media service unavailable")
    : accessStatus === "pending" && approvalRequired
      ? tr("等待管理员审核", "Waiting for approval")
      : accessStatus === "pending"
        ? tr("等待账号绑定", "Waiting for account binding")
      : accessStatus === "disabled"
        ? tr("账号已被禁用", "Account disabled")
        : accessStatus === "denied"
          ? tr("访问申请未通过", "Access request denied")
          : bindingSyncStatus === "error"
            ? tr("账号绑定异常", "Account binding error")
            : tr("媒体库尚未就绪", "Media library not ready");

  async function logout() {
    setLoading(true);
    setError("");
    try {
      await api("/api/auth/logout", { method: "POST" });
      onAuthenticated();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }

  return (
    <CenterShell icon={waiting ? <Clock3 size={30} /> : <ShieldCheck size={30} />} title={title}>
      <div className="account-state">
        {waiting && <LoaderCircle className="spin" size={25} />}
        <p className="gate-message">{accountStatusMessage(accessStatus, bindingSyncStatus, publicAlbumEnabled, serviceUnavailable, approvalRequired)}</p>
        {waiting && <small>{tr("状态更新后，本页会自动刷新。", "This page refreshes automatically when the status changes.")}</small>}
        {error && <p className="form-error" role="alert">{error}</p>}
        <button className="button secondary wide" disabled={loading} onClick={() => void logout()} type="button">
          {loading ? <LoaderCircle className="spin" size={18} /> : <LogOut size={18} />}{tr("退出账号", "Sign out")}
        </button>
      </div>
    </CenterShell>
  );
}

export function TelegramLogin({ onAuthenticated }: GateProps) {
  const { tr } = useI18n();
  const [auth, setAuth] = useState<TelegramAuthStatus>({
    state: "unauthenticated",
    authenticated: false,
    expires_at: null,
    error: null,
  });
  const [qrUrl, setQrUrl] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    if (!qrUrl || !canvasRef.current) return;
    QRCode.toCanvas(canvasRef.current, qrUrl, {
      width: 236,
      margin: 2,
      color: { dark: "#111111", light: "#ffffff" },
      errorCorrectionLevel: "M",
    }).catch((reason) => setError(errorMessage(reason)));
  }, [qrUrl]);

  useEffect(() => {
    if (!['waiting_for_scan', 'password_required'].includes(auth.state)) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await api<TelegramAuthStatus>("/api/auth/qr/status");
        setAuth(next);
        if (next.authenticated) {
          window.clearInterval(timer);
          onAuthenticated();
        }
      } catch (reason) {
        setError(errorMessage(reason));
      }
    }, 1800);
    return () => window.clearInterval(timer);
  }, [auth.state, onAuthenticated]);

  async function startQr() {
    setLoading(true);
    setError("");
    try {
      const next = await api<TelegramAuthStatus>("/api/auth/qr", { method: "POST" });
      setAuth(next);
      setQrUrl(next.url || "");
      if (next.authenticated) onAuthenticated();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }

  async function submitPassword(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      const next = await api<TelegramAuthStatus>("/api/auth/password", {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      setAuth(next);
      setPassword("");
      onAuthenticated();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }

  const waiting = auth.state === "waiting_for_scan";
  const expired = auth.state === "qr_expired";
  const needsPassword = auth.state === "password_required";

  return (
    <CenterShell icon={<QrCode size={30} />} title={tr("连接 Telegram", "Connect Telegram")}>
      <div className="telegram-login">
        {waiting && qrUrl ? (
          <div className="qr-frame">
            <canvas ref={canvasRef} aria-label={tr("Telegram 登录二维码", "Telegram login QR code")} />
            <span className="status-line waiting"><span />{tr("等待扫码确认", "Waiting for scan confirmation")}</span>
          </div>
        ) : needsPassword ? (
          <form className="auth-form" onSubmit={submitPassword}>
            <label htmlFor="telegram-password">{tr("Telegram 两步验证密码", "Telegram two-step verification password")}</label>
            <div className="input-with-icon">
              <LockKeyhole size={18} aria-hidden="true" />
              <input
                id="telegram-password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
                autoFocus
              />
            </div>
            <button className="button primary wide" disabled={loading} type="submit">
              {loading ? <LoaderCircle className="spin" size={18} /> : <CheckCircle2 size={18} />}
              {tr("登录", "Sign in")}
            </button>
          </form>
        ) : (
          <button className="button primary wide" disabled={loading} onClick={startQr} type="button">
            {loading ? <LoaderCircle className="spin" size={18} /> : expired ? <RefreshCw size={18} /> : <QrCode size={18} />}
            {expired ? tr("刷新二维码", "Refresh QR code") : tr("生成登录二维码", "Generate login QR code")}
          </button>
        )}
        {(error || auth.error) && <p className="form-error" role="alert">{error || auth.error}</p>}
      </div>
    </CenterShell>
  );
}

interface CenterShellProps {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}

export function CenterShell({ icon, title, children }: CenterShellProps) {
  const { tr } = useI18n();
  return (
    <main className="gate-page">
      <a className="brand gate-brand" href="/" aria-label={tr("SavedStream 首页", "SavedStream home")}>
        <span className="brand-mark">S</span>
        <span>SavedStream</span>
      </a>
      <div className="gate-language"><LanguageSelector compact /></div>
      <section className="gate-panel" aria-labelledby="gate-title">
        <div className="gate-icon" aria-hidden="true">{icon}</div>
        <h1 id="gate-title">{title}</h1>
        {children}
      </section>
    </main>
  );
}
