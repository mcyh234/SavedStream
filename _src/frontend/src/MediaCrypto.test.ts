import "fake-indexeddb/auto";
import { beforeEach, describe, expect, it } from "vitest";
import { createDevice, decryptMediaResponse, deleteStoredDevice, readStoredDevice, unlockExistingDevice, writeStoredDevice } from "./MediaCrypto";

const password = "a-long-local-password";

beforeEach(async () => { await deleteStoredDevice(); });

describe("device key storage", () => {
  it("encrypts the private key and restores it from IndexedDB", async () => {
    const created = await createDevice(password);
    expect(created.stored.encryptedPrivateKey).not.toContain("private");
    await writeStoredDevice(created.stored);
    expect((await readStoredDevice())?.publicKeySpki).toBe(created.stored.publicKeySpki);
    await expect(unlockExistingDevice(created.stored, "wrong-password")).rejects.toThrow();
    const unlocked = await unlockExistingDevice(created.stored, password);
    expect(unlocked.privateKey.algorithm.name).toBe("RSA-OAEP");
  });
});

describe("encrypted media response", () => {
  it("unwraps and decrypts the server response format", async () => {
    const device = await createDevice(password);
    const publicKey = await crypto.subtle.importKey("spki", decode(device.stored.publicKeySpki), { name: "RSA-OAEP", hash: "SHA-256" }, false, ["encrypt"]);
    const contentKey = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, true, ["encrypt", "decrypt"]);
    const rawContentKey = await crypto.subtle.exportKey("raw", contentKey);
    const nonce = crypto.getRandomValues(new Uint8Array(12));
    const aad = new TextEncoder().encode("chunk:default:1:0:7:7");
    const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce, additionalData: aad }, contentKey, new TextEncoder().encode("payload"));
    const wrapped = await crypto.subtle.encrypt({ name: "RSA-OAEP" }, publicKey, rawContentKey);
    const response = new Response(ciphertext, { headers: { "X-SavedStream-Wrapped-Key": encode(wrapped), "X-SavedStream-Nonce": encode(nonce), "X-SavedStream-AAD": encode(aad) } });
    const plain = await decryptMediaResponse(device.privateKey, response);
    expect(new TextDecoder().decode(plain)).toBe("payload");
  });
});

function encode(value: ArrayBuffer | Uint8Array) { const bytes = value instanceof Uint8Array ? value : new Uint8Array(value); let binary = ""; for (const byte of bytes) binary += String.fromCharCode(byte); return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
function decode(value: string) { const normalized = value.replace(/-/g, "+").replace(/_/g, "/"); const binary = atob(normalized + "=".repeat((4 - normalized.length % 4) % 4)); return Uint8Array.from(binary, (character) => character.charCodeAt(0)); }