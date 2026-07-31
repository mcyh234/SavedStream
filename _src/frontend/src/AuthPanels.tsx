import { FormEvent, useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import {
  CheckCircle2,
  KeyRound,
  LoaderCircle,
  LockKeyhole,
  QrCode,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { api, errorMessage } from "./api";
import type { TelegramAuthStatus } from "./types";

interface GateProps {
  onAuthenticated: () => void;
}

export function AdminKeyGate({ onAuthenticated }: GateProps) {
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
    <CenterShell icon={<ShieldCheck size={30} />} title="管理员验证">
      <form className="auth-form" onSubmit={submit}>
        <label htmlFor="admin-key">管理员密钥</label>
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
          验证
        </button>
      </form>
    </CenterShell>
  );
}

export function ViewerGate({ onAuthenticated }: GateProps) {
  const [key, setKey] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      await api("/api/access/login", {
        method: "POST",
        body: JSON.stringify({ key }),
      });
      onAuthenticated();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }

  return (
    <CenterShell icon={<LockKeyhole size={30} />} title="私人媒体库">
      <form className="auth-form" onSubmit={submit}>
        <label htmlFor="viewer-key">访问口令</label>
        <div className="input-with-icon">
          <KeyRound size={18} aria-hidden="true" />
          <input
            id="viewer-key"
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
          {loading ? <LoaderCircle className="spin" size={18} /> : <ShieldCheck size={18} />}
          进入媒体库
        </button>
      </form>
    </CenterShell>
  );
}

export function TelegramLogin({ onAuthenticated }: GateProps) {
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
    <CenterShell icon={<QrCode size={30} />} title="连接 Telegram">
      <div className="telegram-login">
        {waiting && qrUrl ? (
          <div className="qr-frame">
            <canvas ref={canvasRef} aria-label="Telegram 登录二维码" />
            <span className="status-line waiting"><span />等待扫码确认</span>
          </div>
        ) : needsPassword ? (
          <form className="auth-form" onSubmit={submitPassword}>
            <label htmlFor="telegram-password">Telegram 两步验证密码</label>
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
              登录
            </button>
          </form>
        ) : (
          <button className="button primary wide" disabled={loading} onClick={startQr} type="button">
            {loading ? <LoaderCircle className="spin" size={18} /> : expired ? <RefreshCw size={18} /> : <QrCode size={18} />}
            {expired ? "刷新二维码" : "生成登录二维码"}
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
  return (
    <main className="gate-page">
      <a className="brand gate-brand" href="/" aria-label="SavedStream 首页">
        <span className="brand-mark">S</span>
        <span>SavedStream</span>
      </a>
      <section className="gate-panel" aria-labelledby="gate-title">
        <div className="gate-icon" aria-hidden="true">{icon}</div>
        <h1 id="gate-title">{title}</h1>
        {children}
      </section>
    </main>
  );
}

