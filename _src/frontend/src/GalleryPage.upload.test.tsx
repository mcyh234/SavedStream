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
    if (url.includes("/like")) return { like_count: 4, liked_by_me: true };
    if (url.startsWith("/api/uploads/")) return { id: "job-test", status: "completed", phase: "completed", progress: 100, requested_visibility: "private", review_status: "not_required" };
    if (url.startsWith("/api/media")) return { items: [item], next_cursor: null, has_more: false };
    return { ok: true };
  });
  return { api, calls };
});

const uploadUrls: string[] = [];
let uploadResponseStatus = 202;

class FakeUploadRequest {
  static readonly DONE = 4;
  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
  withCredentials = false;
  status = 0;
  responseText = "";
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  onload: (() => void) | null = null;
  private url = "";

  open(_method: string, url: string) {
    this.url = url;
    uploadUrls.push(url);
  }

  setRequestHeader() {}

  send(file: File) {
    this.upload.onprogress?.({ lengthComputable: true, loaded: file.size, total: file.size } as ProgressEvent);
    this.status = uploadResponseStatus;
    this.responseText = uploadResponseStatus === 202
      ? JSON.stringify({ id: "job-test", progress: 1 })
      : JSON.stringify({ detail: { message: "Telegram upload failed" } });
    this.onload?.();
  }

  abort() {
    this.onabort?.();
  }
}

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
    uploadUrls.length = 0;
    uploadResponseStatus = 202;
    apiMock.api.mockClear();
    vi.stubGlobal("XMLHttpRequest", FakeUploadRequest);
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
    vi.useRealTimers();
    vi.unstubAllGlobals();
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
    expect(dialog?.textContent).toMatch(/自动分配入库账号|automatically assigns an ingestion account/);

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
    await act(async () => dispatchFileDrag("drop", [new File(["public"], "square.bin")]));
    expect(container.querySelector<HTMLSelectElement>(".web-upload-row select")?.value).toBe("public");
    const closeUpload = container.querySelector<HTMLButtonElement>(".upload-dialog .viewer-topbar .icon-button");
    await act(async () => closeUpload?.click());
    const reportButton = [...container.querySelectorAll<HTMLButtonElement>(".media-card-social button")].find((button) => /举报|Report/.test(button.textContent || ""));
    expect(reportButton).toBeTruthy();
    await act(async () => reportButton?.click());
    expect(container.querySelector(".report-dialog")?.textContent).toMatch(/举报资源|Report media/);
    expect(container.querySelector(".media-card-social")?.textContent).toContain("3");
  });

  it("keeps square, uploader public, and likes visible for administrators", async () => {
    await act(async () => {
      root.render(<I18nProvider><GalleryPage isAdmin /></I18nProvider>);
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });
    const navigation = container.querySelector(".sidebar-view-nav");
    expect(navigation?.textContent).toMatch(/广场|Square/);
    expect(navigation?.textContent).toMatch(/我的公开|My public/);
    expect(navigation?.textContent).toMatch(/我的点赞|My likes/);

    const squareButton = [...container.querySelectorAll<HTMLButtonElement>(".sidebar-view-nav button")]
      .find((button) => /广场|Square/.test(button.textContent || ""));
    await act(async () => {
      squareButton?.click();
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });
    expect(apiMock.calls.some((url) => url.startsWith("/api/media?") && url.includes("view=square"))).toBe(true);
    const likeButton = container.querySelector<HTMLButtonElement>(".media-card-social button");
    expect(likeButton).toBeTruthy();
    await act(async () => likeButton?.click());
    expect(apiMock.calls.some((url) => url === "/api/media/77/like?account=alpha")).toBe(true);
  });

  it("does not query a personal mailbox from a recovery-only administrator session", async () => {
    apiMock.calls.length = 0;
    await act(async () => {
      root.render(<I18nProvider><GalleryPage isAdmin canUsePersonalFeatures={false} /></I18nProvider>);
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });
    expect(apiMock.calls.some((url) => url.startsWith("/api/notifications"))).toBe(false);

    const squareButton = [...container.querySelectorAll<HTMLButtonElement>(".sidebar-view-nav button")]
      .find((button) => /广场|Square/.test(button.textContent || ""));
    await act(async () => {
      squareButton?.click();
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });
    expect(container.querySelector(".media-card-social")).toBeNull();
  });

  it("animates completed rows into a success check and closes after five seconds", async () => {
    vi.useFakeTimers();
    await act(async () => dispatchFileDrag("drop", [new File(["done"], "done.bin")]));
    const start = [...container.querySelectorAll<HTMLButtonElement>(".upload-dialog-actions button")]
      .find((button) => /开始上传|Start upload/.test(button.textContent || ""));
    await act(async () => {
      start?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(uploadUrls[0]).toContain("visibility=private");
    expect(uploadUrls[0]).not.toContain("account=");

    await act(async () => {
      vi.advanceTimersByTime(1200);
      await Promise.resolve();
    });
    expect(container.querySelector(".upload-success-card")?.textContent).toMatch(/上传完成|Upload complete/);
    expect(container.querySelector(".upload-success-card")?.textContent).toMatch(/5 秒|5 seconds/);

    await act(async () => {
      vi.advanceTimersByTime(5000);
      await Promise.resolve();
    });
    expect(container.querySelector(".upload-dialog")).toBeNull();
  });

  it("keeps failed files visible with a red failure icon", async () => {
    uploadResponseStatus = 500;
    await act(async () => dispatchFileDrag("drop", [new File(["bad"], "bad.bin")]));
    const start = [...container.querySelectorAll<HTMLButtonElement>(".upload-dialog-actions button")]
      .find((button) => /开始上传|Start upload/.test(button.textContent || ""));
    await act(async () => {
      start?.click();
      await Promise.resolve();
    });
    expect(container.querySelector(".web-upload-failure-icon")).toBeTruthy();
    expect(container.querySelector(".web-upload-row")?.textContent).toContain("Telegram upload failed");
    expect(container.querySelector(".upload-success-card")).toBeNull();
  });
});
