import {
  createContext,
  FormEvent,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { KeyRound, LoaderCircle, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { ApiError, api, errorMessage } from "./api";
import { CenterShell } from "./AuthPanels";
import { translateNow, useI18n } from "./I18n";

const DB_NAME = "savedstream-security";
const STORE_NAME = "device-keys";
const RECORD_KEY = "primary";
const SESSION_RECORD_KEY = "savedstream-media-session-key-v1";
const PBKDF2_ITERATIONS = 310_000;

export type MediaKeyMode = "persistent" | "session";

export interface StoredDeviceKey {
  id: string;
  publicKeySpki: string;
  encryptedPrivateKey: string;
  salt: string;
  iv: string;
  fingerprint: string;
}

export interface StoredSessionDeviceKey {
  id: string;
  sessionId: string;
  publicKeySpki: string;
  encryptedPrivateKey: string;
  wrappingKey: string;
  iv: string;
  fingerprint: string;
}

interface DeviceRegistration {
  registered: boolean;
  fingerprint: string;
}

interface MediaCryptoContextValue {
  status: "idle" | "checking" | "locked" | "ready" | "error";
  mode: MediaKeyMode | null;
  sessionId: string;
  hasStoredKey: boolean;
  fingerprint: string;
  error: string;
  prepare: (mode: MediaKeyMode, sessionId?: string, force?: boolean) => Promise<void>;
  unlock: (password: string) => Promise<void>;
  reset: (revoke?: boolean) => Promise<void>;
  fetchAndDecrypt: (url: string, signal?: AbortSignal) => Promise<{ data: ArrayBuffer; headers: Headers }>;
}

const THUMBNAIL_CACHE_LIMIT = 400;

interface CachedThumbnail {
  data: ArrayBuffer;
  headers: Headers;
}

const MediaCryptoContext = createContext<MediaCryptoContextValue | null>(null);

export function MediaCryptoProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<MediaCryptoContextValue["status"]>("idle");
  const [mode, setMode] = useState<MediaKeyMode | null>(null);
  const [sessionId, setSessionId] = useState("");
  const [hasStoredKey, setHasStoredKey] = useState(false);
  const [privateKey, setPrivateKey] = useState<CryptoKey | null>(null);
  const [fingerprint, setFingerprint] = useState("");
  const [error, setError] = useState("");
  const generationRef = useRef(0);
  const modeRef = useRef<MediaKeyMode | null>(null);
  const sessionIdRef = useRef("");
  const fingerprintRef = useRef("");
  const statusRef = useRef<MediaCryptoContextValue["status"]>("idle");
  // In-memory LRU for decrypted thumbnails.  Chunk/stream URLs are never
  // cached here because they can be large and are fetched sequentially.
  const thumbnailCacheRef = useRef<Map<string, CachedThumbnail>>(new Map());
  // Loop diagnostics: count cache-missing requests per thumbnail URL so a
  // render/effect loop (which bypasses the cache) is visible in the console.
  const thumbnailMissCountRef = useRef<Map<string, number>>(new Map());
  const thumbnailWarnedRef = useRef<Set<string>>(new Set());

  const updateStatus = useCallback((next: MediaCryptoContextValue["status"]) => {
    statusRef.current = next;
    setStatus(next);
  }, []);

  const applyReadyDevice = useCallback((nextPrivateKey: CryptoKey, nextFingerprint: string) => {
    setPrivateKey(nextPrivateKey);
    fingerprintRef.current = nextFingerprint;
    setFingerprint(nextFingerprint);
    setHasStoredKey(true);
    setError("");
    updateStatus("ready");
  }, [updateStatus]);

  const prepare = useCallback(async (nextMode: MediaKeyMode, nextSessionId = "", force = false) => {
    const normalizedSessionId = nextMode === "session" ? nextSessionId.trim() : "";
    if (!force
      && modeRef.current === nextMode
      && sessionIdRef.current === normalizedSessionId
      && statusRef.current !== "idle"
      && statusRef.current !== "error") {
      return;
    }

    const generation = ++generationRef.current;
    modeRef.current = nextMode;
    sessionIdRef.current = normalizedSessionId;
    setMode(nextMode);
    setSessionId(normalizedSessionId);
    setPrivateKey(null);
    fingerprintRef.current = "";
    setFingerprint("");
    setHasStoredKey(false);
    setError("");
    updateStatus("checking");

    try {
      if (nextMode === "persistent") {
        const stored = await readStoredDevice();
        if (generation !== generationRef.current) return;
        setHasStoredKey(Boolean(stored));
        updateStatus("locked");
        return;
      }

      if (!normalizedSessionId) throw new Error(translateNow("缺少已验证的媒体会话。", "The authenticated media session is missing."));
      let stored = readSessionDevice();
      if (stored && stored.sessionId !== normalizedSessionId) {
        deleteSessionDevice();
        stored = null;
      }

      let unlocked;
      try {
        unlocked = stored
          ? await unlockSessionDevice(stored)
          : await createSessionDevice(normalizedSessionId);
      } catch {
        deleteSessionDevice();
        unlocked = await createSessionDevice(normalizedSessionId);
      }

      let registration: DeviceRegistration;
      try {
        registration = await registerDevice(unlocked.stored.publicKeySpki, "session");
      } catch (reason) {
        if (!(reason instanceof ApiError) || reason.code !== "DEVICE_KEY_REVOKED") throw reason;
        deleteSessionDevice();
        unlocked = await createSessionDevice(normalizedSessionId);
        registration = await registerDevice(unlocked.stored.publicKeySpki, "session");
      }
      if (generation !== generationRef.current) return;
      const registered = { ...unlocked.stored, fingerprint: registration.fingerprint };
      writeSessionDevice(registered);
      applyReadyDevice(unlocked.privateKey, registration.fingerprint);
    } catch (reason) {
      if (generation !== generationRef.current) return;
      setError(errorMessage(reason));
      updateStatus("error");
    }
  }, [applyReadyDevice, updateStatus]);

  const unlock = useCallback(async (password: string) => {
    if (modeRef.current !== "persistent") throw new Error(translateNow("当前未启用持久设备密钥模式。", "Persistent device-key mode is not active."));
    setError("");
    updateStatus("checking");
    try {
      const existing = await readStoredDevice();
      const unlocked = existing ? await unlockExistingDevice(existing, password) : await createDevice(password);
      const registration = await registerDevice(unlocked.stored.publicKeySpki, "persistent");
      const registered = { ...unlocked.stored, fingerprint: registration.fingerprint };
      await writeStoredDevice(registered);
      applyReadyDevice(unlocked.privateKey, registration.fingerprint);
    } catch (reason) {
      setError(errorMessage(reason));
      updateStatus("locked");
      throw reason;
    }
  }, [applyReadyDevice, updateStatus]);

  const reset = useCallback(async (revoke = true) => {
    ++generationRef.current;
    const activeMode = modeRef.current;
    const activeFingerprint = fingerprintRef.current;
    if (revoke && activeFingerprint) {
      await api("/api/security/device-key", {
        method: "DELETE",
        headers: { "X-SavedStream-Device-Key": activeFingerprint },
      }).catch(() => undefined);
    }
    if (activeMode === "session") deleteSessionDevice();
    else await deleteStoredDevice();
    thumbnailCacheRef.current.clear();
    thumbnailMissCountRef.current.clear();
    thumbnailWarnedRef.current.clear();
    setPrivateKey(null);
    fingerprintRef.current = "";
    setFingerprint("");
    setHasStoredKey(false);
    setError("");
    if (activeMode === "session") {
      modeRef.current = null;
      sessionIdRef.current = "";
      setMode(null);
      setSessionId("");
      updateStatus("idle");
    } else {
      updateStatus("locked");
    }
  }, [updateStatus]);

  const fetchAndDecrypt = useCallback(async (url: string, signal?: AbortSignal) => {
    if (!privateKey || !fingerprint) throw new Error(translateNow("媒体密钥尚未解锁", "Media key is locked"));
    const cacheable = url.includes("/encrypted-thumbnail");
    if (cacheable) {
      if (!thumbnailCacheRef.current.has(url)) {
        const misses = (thumbnailMissCountRef.current.get(url) || 0) + 1;
        thumbnailMissCountRef.current.set(url, misses);
        if (misses >= 4 && !thumbnailWarnedRef.current.has(url)) {
          thumbnailWarnedRef.current.add(url);
          console.warn("[savedstream] encrypted thumbnail requested " + misses + " times without a cache hit; possible render loop: " + url);
        }
      }
      const cached = thumbnailCacheRef.current.get(url);
      if (cached) {
        if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
        // The caller only reads the bytes (Blob construction); the cached
        // ArrayBuffer is never mutated.
        return cached;
      }
    }
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { "X-SavedStream-Device-Key": fingerprint },
      signal,
    });
    if (!response.ok) throw new Error(translateNow(`加密媒体请求失败 (${response.status})`, `Encrypted media request failed (${response.status})`));
    const data = await decryptMediaResponse(privateKey, response);
    const result = { data, headers: response.headers };
    if (cacheable) {
      const cache = thumbnailCacheRef.current;
      cache.delete(url);
      cache.set(url, result);
      while (cache.size > THUMBNAIL_CACHE_LIMIT) {
        const oldestKey = cache.keys().next().value;
        if (oldestKey === undefined) break;
        cache.delete(oldestKey);
      }
    }
    return result;
  }, [privateKey, fingerprint]);

  const value = useMemo(() => ({
    status,
    mode,
    sessionId,
    hasStoredKey,
    fingerprint,
    error,
    prepare,
    unlock,
    reset,
    fetchAndDecrypt,
  }), [status, mode, sessionId, hasStoredKey, fingerprint, error, prepare, unlock, reset, fetchAndDecrypt]);
  return <MediaCryptoContext.Provider value={value}>{children}</MediaCryptoContext.Provider>;
}

export function useMediaCrypto() {
  const value = useContext(MediaCryptoContext);
  if (!value) throw new Error("MediaCryptoProvider is missing");
  return value;
}

export function MediaEncryptionGate({
  children,
  mode = "persistent",
  sessionId = "",
}: {
  children: ReactNode;
  mode?: MediaKeyMode;
  sessionId?: string;
}) {
  const mediaCrypto = useMediaCrypto();
  const { tr } = useI18n();
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const modeMatches = mediaCrypto.mode === mode && (mode === "persistent" || mediaCrypto.sessionId === sessionId);
  const prepareMediaCrypto = mediaCrypto.prepare;

  useEffect(() => {
    if (modeMatches) return;
    void prepareMediaCrypto(mode, sessionId);
  }, [mode, modeMatches, prepareMediaCrypto, sessionId]);

  if (modeMatches && mediaCrypto.status === "ready") return <>{children}</>;

  if (mode === "session") {
    const failed = modeMatches && mediaCrypto.status === "error";
    return (
      <CenterShell
        icon={failed ? <RefreshCw size={30} /> : <LoaderCircle className="spin" size={30} />}
        title={failed ? tr("无法建立安全媒体会话", "Unable to create a secure media session") : tr("正在准备安全媒体会话", "Preparing secure media session")}
      >
        <p className={failed ? "form-error" : "gate-copy"} role={failed ? "alert" : "status"}>
          {failed
            ? mediaCrypto.error
            : tr("媒体密钥仅保留在本次 /web 登录会话中，退出或会话失效后会自动清除。", "The media key is kept only for this /web login session and is cleared after logout or session expiry.")}
        </p>
        {failed && (
          <button className="button secondary wide" onClick={() => void mediaCrypto.prepare("session", sessionId, true)} type="button">
            <RefreshCw size={18} />{tr("重新建立", "Try again")}
          </button>
        )}
      </CenterShell>
    );
  }

  if (!modeMatches || mediaCrypto.status === "idle" || mediaCrypto.status === "checking") {
    return (
      <CenterShell icon={<LoaderCircle className="spin" size={30} />} title={tr("正在检查设备密钥", "Checking device key")}>
        <p className="gate-copy">{tr("正在读取浏览器中加密保存的设备密钥。", "Reading the encrypted device key stored in this browser.")}</p>
      </CenterShell>
    );
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!mediaCrypto.hasStoredKey && password !== confirmation) {
      setFormError(tr("两次输入的解锁密码不一致。", "The two unlock passwords do not match."));
      return;
    }
    setBusy(true);
    setFormError("");
    try {
      await mediaCrypto.unlock(password);
      setPassword("");
      setConfirmation("");
    } catch (reason) {
      setFormError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  const activeError = formError || mediaCrypto.error;
  return (
    <CenterShell
      icon={<ShieldCheck size={30} />}
      title={mediaCrypto.hasStoredKey ? tr("解锁媒体密钥", "Unlock media key") : tr("保护此设备", "Protect this device")}
    >
      <form className="auth-form" onSubmit={submit}>
        <p className="gate-message">
          {mediaCrypto.hasStoredKey
            ? tr("请输入本地解锁密码。密码不会发送到服务器。", "Enter the local unlock password. It is never sent to the server.")
            : tr("创建本地解锁密码。加密后的私钥只保存在此浏览器中。", "Create a local unlock password. The encrypted private key stays in this browser.")}
        </p>
        <label htmlFor="media-key-password">{tr("本地解锁密码", "Local unlock password")}</label>
        <div className="input-with-icon">
          <KeyRound size={18} />
          <input id="media-key-password" type="password" minLength={10} value={password} onChange={(event) => setPassword(event.target.value)} required autoFocus />
        </div>
        {!mediaCrypto.hasStoredKey && (
          <>
            <label htmlFor="media-key-confirmation">{tr("确认密码", "Confirm password")}</label>
            <div className="input-with-icon">
              <KeyRound size={18} />
              <input id="media-key-confirmation" type="password" minLength={10} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} required />
            </div>
          </>
        )}
        {activeError && <p className="form-error" role="alert">{activeError}</p>}
        <button className="button primary wide" disabled={busy} type="submit">
          {busy ? <LoaderCircle className="spin" size={18} /> : <ShieldCheck size={18} />}
          {mediaCrypto.hasStoredKey ? tr("解锁", "Unlock") : tr("创建设备密钥", "Create device key")}
        </button>
        {mediaCrypto.hasStoredKey && (
          <button className="button secondary wide" disabled={busy} onClick={() => void mediaCrypto.reset()} type="button">
            <Trash2 size={17} />{tr("重置设备密钥", "Reset device key")}
          </button>
        )}
      </form>
    </CenterShell>
  );
}

async function registerDevice(publicKeySpki: string, persistence: MediaKeyMode): Promise<DeviceRegistration> {
  return api<DeviceRegistration>("/api/security/device-key", {
    method: "POST",
    body: JSON.stringify({
      device_public_key: publicKeySpki,
      key_format: "spki-rsa-oaep-v1",
      persistence,
    }),
  });
}

export async function createDevice(password: string) {
  const pair = await crypto.subtle.generateKey(
    { name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["encrypt", "decrypt"],
  );
  const publicSpki = await crypto.subtle.exportKey("spki", pair.publicKey);
  const privatePkcs8 = await crypto.subtle.exportKey("pkcs8", pair.privateKey);
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const wrappingKey = await deriveWrappingKey(password, salt.buffer as ArrayBuffer);
  const encryptedPrivate = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, wrappingKey, privatePkcs8);
  return {
    privateKey: pair.privateKey,
    stored: {
      id: RECORD_KEY,
      publicKeySpki: encodeBase64(publicSpki),
      encryptedPrivateKey: encodeBase64(encryptedPrivate),
      salt: encodeBase64(salt),
      iv: encodeBase64(iv),
      fingerprint: "",
    } satisfies StoredDeviceKey,
  };
}

export async function unlockExistingDevice(stored: StoredDeviceKey, password: string) {
  try {
    const key = await deriveWrappingKey(password, decodeBase64(stored.salt));
    const pkcs8 = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: decodeBase64(stored.iv) },
      key,
      decodeBase64(stored.encryptedPrivateKey),
    );
    const privateKey = await crypto.subtle.importKey(
      "pkcs8",
      pkcs8,
      { name: "RSA-OAEP", hash: "SHA-256" },
      false,
      ["decrypt"],
    );
    return { privateKey, stored };
  } catch {
    throw new Error(translateNow("本地解锁密码不正确，或设备密钥已损坏。", "The local unlock password is incorrect or the device key is damaged."));
  }
}

export async function createSessionDevice(sessionId: string) {
  const pair = await crypto.subtle.generateKey(
    { name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["encrypt", "decrypt"],
  );
  const publicSpki = await crypto.subtle.exportKey("spki", pair.publicKey);
  const privatePkcs8 = await crypto.subtle.exportKey("pkcs8", pair.privateKey);
  const rawWrappingKey = crypto.getRandomValues(new Uint8Array(32));
  const wrappingKey = await crypto.subtle.importKey("raw", rawWrappingKey, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const encryptedPrivate = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, wrappingKey, privatePkcs8);
  return {
    privateKey: pair.privateKey,
    stored: {
      id: RECORD_KEY,
      sessionId,
      publicKeySpki: encodeBase64(publicSpki),
      encryptedPrivateKey: encodeBase64(encryptedPrivate),
      wrappingKey: encodeBase64(rawWrappingKey),
      iv: encodeBase64(iv),
      fingerprint: "",
    } satisfies StoredSessionDeviceKey,
  };
}

export async function unlockSessionDevice(stored: StoredSessionDeviceKey) {
  const wrappingKey = await crypto.subtle.importKey(
    "raw",
    decodeBase64(stored.wrappingKey),
    { name: "AES-GCM" },
    false,
    ["decrypt"],
  );
  const pkcs8 = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: decodeBase64(stored.iv) },
    wrappingKey,
    decodeBase64(stored.encryptedPrivateKey),
  );
  const privateKey = await crypto.subtle.importKey(
    "pkcs8",
    pkcs8,
    { name: "RSA-OAEP", hash: "SHA-256" },
    false,
    ["decrypt"],
  );
  return { privateKey, stored };
}

async function deriveWrappingKey(password: string, salt: ArrayBuffer) {
  const material = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", hash: "SHA-256", salt, iterations: PBKDF2_ITERATIONS },
    material,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

export async function decryptMediaResponse(privateKey: CryptoKey, response: Response): Promise<ArrayBuffer> {
  const rawKey = await crypto.subtle.decrypt(
    { name: "RSA-OAEP" },
    privateKey,
    decodeBase64(requiredHeader(response.headers, "X-SavedStream-Wrapped-Key")),
  );
  const contentKey = await crypto.subtle.importKey("raw", rawKey, { name: "AES-GCM" }, false, ["decrypt"]);
  return crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: decodeBase64(requiredHeader(response.headers, "X-SavedStream-Nonce")),
      additionalData: decodeBase64(requiredHeader(response.headers, "X-SavedStream-AAD")),
    },
    contentKey,
    await response.arrayBuffer(),
  );
}

function requiredHeader(headers: Headers, name: string) {
  const value = headers.get(name);
  if (!value) throw new Error(translateNow(`加密响应缺少 ${name}`, `Encrypted response is missing ${name}`));
  return value;
}

function encodeBase64(value: ArrayBuffer | Uint8Array) {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decodeBase64(value: string) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(normalized + "=".repeat((4 - normalized.length % 4) % 4));
  return Uint8Array.from(binary, (character) => character.charCodeAt(0)).buffer;
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE_NAME, { keyPath: "id" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export async function readStoredDevice(): Promise<StoredDeviceKey | null> {
  const database = await openDatabase();
  return new Promise<StoredDeviceKey | null>((resolve, reject) => {
    const request = database.transaction(STORE_NAME, "readonly").objectStore(STORE_NAME).get(RECORD_KEY);
    request.onsuccess = () => resolve((request.result as StoredDeviceKey | undefined) || null);
    request.onerror = () => reject(request.error);
  }).finally(() => database.close());
}

export async function writeStoredDevice(value: StoredDeviceKey) {
  const database = await openDatabase();
  await new Promise<void>((resolve, reject) => {
    const request = database.transaction(STORE_NAME, "readwrite").objectStore(STORE_NAME).put(value);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
  database.close();
}

export async function deleteStoredDevice() {
  const database = await openDatabase();
  await new Promise<void>((resolve, reject) => {
    const request = database.transaction(STORE_NAME, "readwrite").objectStore(STORE_NAME).delete(RECORD_KEY);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
  database.close();
}

export function readSessionDevice(): StoredSessionDeviceKey | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(SESSION_RECORD_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<StoredSessionDeviceKey>;
    if (!value.sessionId || !value.publicKeySpki || !value.encryptedPrivateKey || !value.wrappingKey || !value.iv) return null;
    return {
      id: RECORD_KEY,
      sessionId: value.sessionId,
      publicKeySpki: value.publicKeySpki,
      encryptedPrivateKey: value.encryptedPrivateKey,
      wrappingKey: value.wrappingKey,
      iv: value.iv,
      fingerprint: value.fingerprint || "",
    };
  } catch {
    return null;
  }
}

export function writeSessionDevice(value: StoredSessionDeviceKey) {
  if (typeof window === "undefined") return;
  try { window.sessionStorage.setItem(SESSION_RECORD_KEY, JSON.stringify(value)); } catch { /* memory-only fallback */ }
}

export function deleteSessionDevice() {
  if (typeof window === "undefined") return;
  try { window.sessionStorage.removeItem(SESSION_RECORD_KEY); } catch { /* storage unavailable */ }
}
