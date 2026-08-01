import { createContext, FormEvent, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { KeyRound, LoaderCircle, ShieldCheck, Trash2 } from "lucide-react";
import { api, errorMessage } from "./api";
import { CenterShell } from "./AuthPanels";

const DB_NAME = "savedstream-security";
const STORE_NAME = "device-keys";
const RECORD_KEY = "primary";
const PBKDF2_ITERATIONS = 310_000;

export interface StoredDeviceKey {
  id: string; publicKeySpki: string; encryptedPrivateKey: string; salt: string; iv: string; fingerprint: string;
}
interface DeviceRegistration { registered: boolean; fingerprint: string; }
interface MediaCryptoContextValue {
  status: "checking" | "locked" | "ready";
  hasStoredKey: boolean;
  unlock: (password: string) => Promise<void>;
  reset: () => Promise<void>;
  fetchAndDecrypt: (url: string) => Promise<{ data: ArrayBuffer; headers: Headers }>;
}
const MediaCryptoContext = createContext<MediaCryptoContextValue | null>(null);

export function MediaCryptoProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<MediaCryptoContextValue["status"]>("checking");
  const [hasStoredKey, setHasStoredKey] = useState(false);
  const [privateKey, setPrivateKey] = useState<CryptoKey | null>(null);
  const [fingerprint, setFingerprint] = useState("");

  useEffect(() => {
    void readStoredDevice().then((stored) => { setHasStoredKey(Boolean(stored)); setStatus("locked"); });
  }, []);

  const unlock = useCallback(async (password: string) => {
    const existing = await readStoredDevice();
    const unlocked = existing ? await unlockExistingDevice(existing, password) : await createDevice(password);
    const registration = await api<DeviceRegistration>("/api/security/device-key", {
      method: "POST",
      body: JSON.stringify({ device_public_key: unlocked.stored.publicKeySpki, key_format: "spki-rsa-oaep-v1" }),
    });
    await writeStoredDevice({ ...unlocked.stored, fingerprint: registration.fingerprint });
    setPrivateKey(unlocked.privateKey); setFingerprint(registration.fingerprint); setHasStoredKey(true); setStatus("ready");
  }, []);

  const reset = useCallback(async () => {
    if (fingerprint) await api("/api/security/device-key", { method: "DELETE", headers: { "X-SavedStream-Device-Key": fingerprint } }).catch(() => undefined);
    await deleteStoredDevice(); setPrivateKey(null); setFingerprint(""); setHasStoredKey(false); setStatus("locked");
  }, [fingerprint]);

  const fetchAndDecrypt = useCallback(async (url: string) => {
    if (!privateKey || !fingerprint) throw new Error("Media key is locked");
    const response = await fetch(url, { credentials: "same-origin", headers: { "X-SavedStream-Device-Key": fingerprint } });
    if (!response.ok) throw new Error(`Encrypted media request failed (${response.status})`);
    const data = await decryptMediaResponse(privateKey, response);
    return { data, headers: response.headers };
  }, [privateKey, fingerprint]);

  const value = useMemo(() => ({ status, hasStoredKey, unlock, reset, fetchAndDecrypt }), [status, hasStoredKey, unlock, reset, fetchAndDecrypt]);
  return <MediaCryptoContext.Provider value={value}>{children}</MediaCryptoContext.Provider>;
}

export function useMediaCrypto() {
  const value = useContext(MediaCryptoContext);
  if (!value) throw new Error("MediaCryptoProvider is missing");
  return value;
}

export function MediaEncryptionGate({ children }: { children: ReactNode }) {
  const mediaCrypto = useMediaCrypto();
  const [password, setPassword] = useState(""); const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  if (mediaCrypto.status === "ready") return <>{children}</>;
  if (mediaCrypto.status === "checking") return <CenterShell icon={<LoaderCircle className="spin" size={30} />} title="Checking device key"><p className="gate-copy">Reading the encrypted local device key.</p></CenterShell>;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!mediaCrypto.hasStoredKey && password !== confirmation) { setError("The two unlock passwords do not match."); return; }
    setBusy(true); setError("");
    try { await mediaCrypto.unlock(password); setPassword(""); setConfirmation(""); } catch (reason) { setError(errorMessage(reason)); } finally { setBusy(false); }
  }
  return <CenterShell icon={<ShieldCheck size={30} />} title={mediaCrypto.hasStoredKey ? "Unlock media key" : "Protect this device"}>
    <form className="auth-form" onSubmit={submit}>
      <p className="gate-message">{mediaCrypto.hasStoredKey ? "Enter the local unlock password. It is never sent to the server." : "Create a local unlock password. The encrypted private key stays in this browser."}</p>
      <label htmlFor="media-key-password">Local unlock password</label><div className="input-with-icon"><KeyRound size={18} /><input id="media-key-password" type="password" minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} required autoFocus /></div>
      {!mediaCrypto.hasStoredKey && <><label htmlFor="media-key-confirmation">Confirm password</label><div className="input-with-icon"><KeyRound size={18} /><input id="media-key-confirmation" type="password" minLength={10} value={confirmation} onChange={(e) => setConfirmation(e.target.value)} required /></div></>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <button className="button primary wide" disabled={busy} type="submit">{busy ? <LoaderCircle className="spin" size={18} /> : <ShieldCheck size={18} />}{mediaCrypto.hasStoredKey ? "Unlock" : "Create device key"}</button>
      {mediaCrypto.hasStoredKey && <button className="button secondary wide" disabled={busy} onClick={() => void mediaCrypto.reset()} type="button"><Trash2 size={17} />Reset device key</button>}
    </form>
  </CenterShell>;
}

export async function createDevice(password: string) {
  const pair = await crypto.subtle.generateKey({ name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["encrypt", "decrypt"]);
  const publicSpki = await crypto.subtle.exportKey("spki", pair.publicKey); const privatePkcs8 = await crypto.subtle.exportKey("pkcs8", pair.privateKey);
  const salt = crypto.getRandomValues(new Uint8Array(16)); const iv = crypto.getRandomValues(new Uint8Array(12));
  const wrappingKey = await deriveWrappingKey(password, salt.buffer as ArrayBuffer);
  const encryptedPrivate = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, wrappingKey, privatePkcs8);
  return { privateKey: pair.privateKey, stored: { id: RECORD_KEY, publicKeySpki: encodeBase64(publicSpki), encryptedPrivateKey: encodeBase64(encryptedPrivate), salt: encodeBase64(salt), iv: encodeBase64(iv), fingerprint: "" } satisfies StoredDeviceKey };
}
export async function unlockExistingDevice(stored: StoredDeviceKey, password: string) {
  try { const key = await deriveWrappingKey(password, decodeBase64(stored.salt)); const pkcs8 = await crypto.subtle.decrypt({ name: "AES-GCM", iv: decodeBase64(stored.iv) }, key, decodeBase64(stored.encryptedPrivateKey)); const privateKey = await crypto.subtle.importKey("pkcs8", pkcs8, { name: "RSA-OAEP", hash: "SHA-256" }, false, ["decrypt"]); return { privateKey, stored }; }
  catch { throw new Error("The local unlock password is incorrect or the device key is damaged."); }
}
async function deriveWrappingKey(password: string, salt: ArrayBuffer) {
  const material = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt, iterations: PBKDF2_ITERATIONS }, material, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
}
export async function decryptMediaResponse(privateKey: CryptoKey, response: Response): Promise<ArrayBuffer> {
  const rawKey = await crypto.subtle.decrypt({ name: "RSA-OAEP" }, privateKey, decodeBase64(requiredHeader(response.headers, "X-SavedStream-Wrapped-Key")));
  const contentKey = await crypto.subtle.importKey("raw", rawKey, { name: "AES-GCM" }, false, ["decrypt"]);
  return crypto.subtle.decrypt(
    { name: "AES-GCM", iv: decodeBase64(requiredHeader(response.headers, "X-SavedStream-Nonce")), additionalData: decodeBase64(requiredHeader(response.headers, "X-SavedStream-AAD")) },
    contentKey,
    await response.arrayBuffer(),
  );
}
function requiredHeader(headers: Headers, name: string) { const value = headers.get(name); if (!value) throw new Error(`Encrypted response is missing ${name}`); return value; }
function encodeBase64(value: ArrayBuffer | Uint8Array) { const bytes = value instanceof Uint8Array ? value : new Uint8Array(value); let binary = ""; for (const byte of bytes) binary += String.fromCharCode(byte); return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
function decodeBase64(value: string) { const normalized = value.replace(/-/g, "+").replace(/_/g, "/"); const binary = atob(normalized + "=".repeat((4 - normalized.length % 4) % 4)); return Uint8Array.from(binary, (c) => c.charCodeAt(0)).buffer; }
function openDatabase(): Promise<IDBDatabase> { return new Promise((resolve, reject) => { const request = indexedDB.open(DB_NAME, 1); request.onupgradeneeded = () => request.result.createObjectStore(STORE_NAME, { keyPath: "id" }); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }); }
export async function readStoredDevice(): Promise<StoredDeviceKey | null> { const database = await openDatabase(); return new Promise<StoredDeviceKey | null>((resolve, reject) => { const request = database.transaction(STORE_NAME, "readonly").objectStore(STORE_NAME).get(RECORD_KEY); request.onsuccess = () => resolve((request.result as StoredDeviceKey | undefined) || null); request.onerror = () => reject(request.error); }).finally(() => database.close()); }
export async function writeStoredDevice(value: StoredDeviceKey) { const database = await openDatabase(); await new Promise<void>((resolve, reject) => { const request = database.transaction(STORE_NAME, "readwrite").objectStore(STORE_NAME).put(value); request.onsuccess = () => resolve(); request.onerror = () => reject(request.error); }); database.close(); }
export async function deleteStoredDevice() { const database = await openDatabase(); await new Promise<void>((resolve, reject) => { const request = database.transaction(STORE_NAME, "readwrite").objectStore(STORE_NAME).delete(RECORD_KEY); request.onsuccess = () => resolve(); request.onerror = () => reject(request.error); }); database.close(); }