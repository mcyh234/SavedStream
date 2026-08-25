// @vitest-environment jsdom
import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GalleryPage from "./GalleryPage";
import { I18nProvider } from "./I18n";
import type { MediaItem } from "./types";

const apiMock = vi.hoisted(() => {
  const calls: string[] = [];
  const item: MediaItem = {
    account_id: "alpha",
    id: 77,
    kind: "file",
    mime_type: "application/octet-stream",
    size: 12,
    filename: "public.bin",
    original_title: "Public file",
    local_title: null,
    title: "Public file",
    caption: "",
    date: "2026-08-25T12:00:00Z",
    duration: null,
    width: null,
    height: null,
    has_thumbnail: false,
    thumbnail_url: null,
    stream_url: "/api/media/77/stream?account=alpha",
    visibility: "public",
    like_count: 3,
    liked_by_me: false,
    owned_by_me: false,
  };
  const api = vi.fn(async (url: string) => {
    calls.push(url);
    if (url.startsWith("/api/accounts")) return { items: [{ id: "alpha", label: "Alpha", state: "authenticated" }], default_account: "alpha" };
    if (url.startsWith("/api/folders")) return { items: [] };
    if (url.startsWith("/api/notifications/unread-count")) return { count: 0 };
    if (url.startsWith("/api/notifications")) return { items: [], next_cursor: null, has_more: false, unread: 0 };
    if (url.startsWith("/api/media/timeline")) return { years: [], index: { status: "ready" } };
    if (url.startsWith("/api/media")) return { items: [item], next_cursor: null, has_more: false };
    return { ok: true };
  });
  return { api, calls };
});

vi.mock("./api", () => ({
  api: apiMock.api,
  browserId: () => "browser-test",
  errorMessage: (reason: unknown) => reason instanceof Error ? reason.message : String(reason),
}));

vi.mock("./MediaCrypto", () => ({
  useMediaCrypto: () => ({
    status: "ready",
    mode: "session",
    sessionId: "session",
    hasStoredKey: false,
    fingerprint: "fingerprint",
    error: "",
    prepare: vi.fn(),
    unlock: vi.fn(),
    reset: vi.fn(),
    fetchAndDecrypt: vi.fn(),
  }),
}));

function dispatchFileDrag(type: "dragenter" | "drop", files: File[]) {
  const event = new Event(type, { bubbles: true, cancelable: true }) as DragEvent;
  Object.defineProperty(event, "dataTransfer", {
    value: { types: ["Files"], files, dropEffect: "none" },
  });
  window.dispatchEvent(event);
}

describe("gallery upload and square interactions", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(async () => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    window.localStorage.clear();
    apiMock.calls.length = 0;
    apiMock.api.mockClear();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root.render(<I18nProvider><GalleryPage /></I18nProvider>);
      await Promise.resolve();
    });
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.clearAllTimers();
  });

  it("shows a full-page drop affordance and defaults every dropped file to private", async () => {
    const files = [
      new File(["one"], "one.bin", { type: "application/octet-stream" }),
      new File(["two"], "two.jpg", { type: "image/jpeg" }),
    ];
    await act(async () => dispatchFileDrag("dragenter", files));
    expect(container.querySelector(".upload-drop-overlay")?.textContent).toMatch(/松手即可添加文件|Drop files to add them/);

    await act(async () => dispatchFileDrag("drop", files));
    const dialog = container.querySelector(".upload-dialog");
    expect(dialog?.textContent).toContain("one.bin");
    expect(dialog?.textContent).toContain("two.jpg");
    const selects = [...container.querySelectorAll<HTMLSelectElement>(".web-upload-row select")];
    expect(selects.map((select) => select.value)).toEqual(["private", "private"]);

    const publicButton = [...container.querySelectorAll<HTMLButtonElement>("button")].find((button) => /全部公开|All public/.test(button.textContent || ""));
    await act(async () => publicButton?.click());
    expect(selects.map((select) => select.value)).toEqual(["public", "public"]);
  });

  it("switches to the square view and exposes like/report actions", async () => {
    const squareButton = [...container.querySelectorAll<HTMLButtonElement>(".sidebar-view-nav button")].find((button) => /广场|Square/.test(button.textContent || ""));
    await act(async () => {
      squareButton?.click();
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });
    expect(apiMock.calls.some((url) => url.startsWith("/api/media?") && url.includes("view=square"))).toBe(true);
    const reportButton = [...container.querySelectorAll<HTMLButtonElement>(".media-card-social button")].find((button) => /举报|Report/.test(button.textContent || ""));
    expect(reportButton).toBeTruthy();
    await act(async () => reportButton?.click());
    expect(container.querySelector(".report-dialog")?.textContent).toMatch(/举报资源|Report media/);
    expect(container.querySelector(".media-card-social")?.textContent).toContain("3");
  });
});
