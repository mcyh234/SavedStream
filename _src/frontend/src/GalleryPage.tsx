import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownAZ,
  ArrowUpAZ,
  AudioLines,
  Download,
  FileArchive,
  FileText,
  Film,
  FolderOpen,
  HardDriveDownload,
  Home,
  Image as ImageIcon,
  LoaderCircle,
  Music2,
  Play,
  Search,
  Settings,
  X,
} from "lucide-react";
import { api, errorMessage } from "./api";
import { useMediaCrypto } from "./MediaCrypto";
import type { AccountStatus, MediaItem, MediaKind, MediaPage } from "./types";

const filters: Array<{ value: MediaKind; label: string; icon: typeof Home }> = [
  { value: "all", label: "全部", icon: Home },
  { value: "video", label: "视频", icon: Film },
  { value: "image", label: "图片", icon: ImageIcon },
  { value: "audio", label: "音频", icon: Music2 },
  { value: "file", label: "文件", icon: FolderOpen },
];

export default function GalleryPage() {
  const [accounts, setAccounts] = useState<AccountStatus[]>([]);
  const [account, setAccount] = useState("");
  const [items, setItems] = useState<MediaItem[]>([]);
  const [kind, setKind] = useState<MediaKind>("all");
  const [order, setOrder] = useState<"newest" | "oldest">("newest");
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [cursor, setCursor] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<MediaItem | null>(null);

  useEffect(() => {
    void api<{ items: AccountStatus[]; default_account: string }>("/api/accounts").then((result) => {
      setAccounts(result.items);
      setAccount((current) => current || result.default_account || result.items[0]?.id || "");
    }).catch((reason) => setError(errorMessage(reason)));
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 320);
    return () => window.clearTimeout(timer);
  }, [query]);

  const loadMedia = useCallback(async (nextCursor: number | null = null) => {
    if (!account) return;
    nextCursor ? setLoadingMore(true) : setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({
        limit: "36",
        kind,
        order,
        q: debouncedQuery,
        account,
      });
      if (nextCursor) params.set("cursor", String(nextCursor));
      const page = await api<MediaPage>(`/api/media?${params}`);
      setItems((current) => nextCursor ? [...current, ...page.items] : page.items);
      setCursor(page.next_cursor);
      setHasMore(page.has_more);
    } catch (reason) {
      setError(errorMessage(reason));
      if (!nextCursor) setItems([]);
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [account, debouncedQuery, kind, order]);

  useEffect(() => {
    setCursor(null);
    loadMedia();
  }, [loadMedia]);

  const title = useMemo(() => filters.find((item) => item.value === kind)?.label || "全部", [kind]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="SavedStream 首页">
          <span className="brand-mark"><Play size={16} fill="currentColor" /></span>
          <span>SavedStream</span>
        </a>
        <label className="search-field">
          <span className="sr-only">搜索收藏夹</span>
          <Search size={18} aria-hidden="true" />
          <input
            type="search"
            placeholder="搜索收藏夹"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {query && (
            <button aria-label="清除搜索" title="清除" onClick={() => setQuery("")} type="button">
              <X size={17} />
            </button>
          )}
        </label>
        <a className="icon-button" href="/admin" aria-label="管理员设置" title="管理员设置">
          <Settings size={21} />
        </a>
      </header>

      <aside className="sidebar" aria-label="媒体分类">
        <nav>
          {filters.map((filter) => {
            const Icon = filter.icon;
            return (
              <button
                className={kind === filter.value ? "active" : ""}
                key={filter.value}
                onClick={() => setKind(filter.value)}
                type="button"
              >
                <Icon size={20} />
                <span>{filter.label}</span>
              </button>
            );
          })}
        </nav>
        <div className="sidebar-footer">
          <HardDriveDownload size={18} />
          <span>按需缓存</span>
        </div>
      </aside>

      <main className="library" id="main-content">
        <div className="mobile-filters" aria-label="媒体分类">
          {filters.map((filter) => (
            <button
              className={kind === filter.value ? "active" : ""}
              key={filter.value}
              onClick={() => setKind(filter.value)}
              type="button"
            >
              {filter.label}
            </button>
          ))}
        </div>
        <div className="library-heading">
          <div>
            <h1>{debouncedQuery ? `“${debouncedQuery}”` : title}</h1>
            {!loading && <span className="result-count">{items.length} 项</span>}
          </div>
          {accounts.length > 1 && (
            <label className="sort-control">
              <span className="sr-only">托管账号</span>
              <select value={account} onChange={(event) => setAccount(event.target.value)}>
                {accounts.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select>
            </label>
          )}
          <label className="sort-control">
            <span className="sr-only">时间排序</span>
            {order === "newest" ? <ArrowDownAZ size={17} /> : <ArrowUpAZ size={17} />}
            <select value={order} onChange={(event) => setOrder(event.target.value as "newest" | "oldest")}>
              <option value="newest">最新优先</option>
              <option value="oldest">最早优先</option>
            </select>
          </label>
        </div>

        {loading ? (
          <MediaSkeleton />
        ) : error ? (
          <div className="state-block" role="alert">
            <FileArchive size={38} />
            <h2>无法读取收藏夹</h2>
            <p>{error}</p>
            <button className="button secondary" onClick={() => loadMedia()} type="button">重新加载</button>
          </div>
        ) : items.length === 0 ? (
          <div className="state-block">
            <FolderOpen size={38} />
            <h2>没有找到媒体</h2>
            {hasMore && cursor && (
              <button className="button secondary" disabled={loadingMore} onClick={() => loadMedia(cursor)} type="button">
                {loadingMore && <LoaderCircle className="spin" size={18} />}
                继续查找
              </button>
            )}
          </div>
        ) : (
          <>
            <section className="media-grid" aria-label="收藏夹媒体">
              {items.map((item) => (
                <MediaCard item={item} key={item.id} onOpen={() => setSelected(item)} />
              ))}
            </section>
            {hasMore && cursor && (
              <div className="load-more-row">
                <button className="button secondary" disabled={loadingMore} onClick={() => loadMedia(cursor)} type="button">
                  {loadingMore && <LoaderCircle className="spin" size={18} />}
                  加载更多
                </button>
              </div>
            )}
          </>
        )}
      </main>
      {selected && <MediaViewer item={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function MediaCard({ item, onOpen }: { item: MediaItem; onOpen: () => void }) {
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  const handleThumbnailError = useCallback(() => setThumbnailFailed(true), []);
  return (
    <button className="media-card" onClick={onOpen} type="button" aria-label={`打开 ${item.title}`}>
      <div className={`media-poster kind-${item.kind}`}>
        {item.thumbnail_url && !thumbnailFailed ? (
          <ThumbnailImage
            src={item.thumbnail_url}
            alt=""
            onError={handleThumbnailError}
          />
        ) : (
          <FileKindIcon kind={item.kind} mime={item.mime_type} />
        )}
        {item.kind === "video" && <span className="play-overlay"><Play size={22} fill="currentColor" /></span>}
        {item.duration != null && <span className="duration">{formatDuration(item.duration)}</span>}
      </div>
      <div className="media-copy">
        <h2 title={item.title}>{item.title}</h2>
        <p><time dateTime={item.date}>{formatDate(item.date)}</time><span> · </span>{formatBytes(item.size)}</p>
      </div>
    </button>
  );
}

function MediaViewer({ item, onClose }: { item: MediaItem; onClose: () => void }) {
  useEffect(() => {
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.body.classList.add("modal-open");
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.classList.remove("modal-open");
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [onClose]);

  return (
    <div className="viewer-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="viewer" role="dialog" aria-modal="true" aria-labelledby="viewer-title">
        <div className="viewer-topbar">
          <h2 id="viewer-title">{item.title}</h2>
          <div>
            <EncryptedDownloadButton item={item} iconOnly />
            <button className="icon-button" onClick={onClose} aria-label="关闭播放器" title="关闭" type="button">
              <X size={22} />
            </button>
          </div>
        </div>
        <div className={`viewer-stage kind-${item.kind}`}>
          {item.kind === "video" && (
            <ViewerVideo item={item} />
          )}
          {item.kind === "image" && <ViewerImage item={item} />}
          {item.kind === "audio" && <ViewerAudio item={item} />}
          {item.kind === "file" && (
            <div className="file-download">
              <FileKindIcon kind={item.kind} mime={item.mime_type} />
              <p>{item.filename}</p>
              <EncryptedDownloadButton item={item} />
            </div>
          )}
        </div>
        <div className="viewer-meta">
          <time dateTime={item.date}>{formatDateTime(item.date)}</time>
          <span>{formatBytes(item.size)}</span>
          <span>{item.mime_type}</span>
        </div>
      </section>
    </div>
  );
}

export function FileKindIcon({ kind, mime }: { kind: MediaItem["kind"]; mime: string }) {
  if (kind === "video") return <Film className="file-kind-icon" size={48} strokeWidth={1.6} />;
  if (kind === "image") return <ImageIcon className="file-kind-icon" size={48} strokeWidth={1.6} />;
  if (kind === "audio") return <Music2 className="file-kind-icon" size={48} strokeWidth={1.6} />;
  if (mime.includes("zip") || mime.includes("archive") || mime.includes("compressed")) {
    return <FileArchive className="file-kind-icon" size={48} strokeWidth={1.6} />;
  }
  return <FileText className="file-kind-icon" size={48} strokeWidth={1.6} />;
}

export function ThumbnailImage({
  src,
  alt,
  onError,
}: {
  src: string;
  alt: string;
  onError: () => void;
}) {
  const mediaCrypto = useMediaCrypto();
  const [loaded, setLoaded] = useState(false);
  const [objectUrl, setObjectUrl] = useState("");
  useEffect(() => {
    let cancelled = false;
    let createdUrl = "";
    const encryptedUrl = src.replace("/thumbnail", "/encrypted-thumbnail");
    setLoaded(false);
    setObjectUrl("");
    void mediaCrypto.fetchAndDecrypt(encryptedUrl).then(({ data, headers }) => {
      if (cancelled) return;
      createdUrl = URL.createObjectURL(new Blob([data], { type: headers.get("X-SavedStream-Mime") || "image/jpeg" }));
      setObjectUrl(createdUrl);
    }).catch(() => {
      if (!cancelled) onError();
    });
    return () => {
      cancelled = true;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [src, mediaCrypto, onError]);
  return (
    <>
      {!loaded && (
        <span className="thumbnail-loading" aria-label="正在加载缩略图">
          <LoaderCircle className="spin" size={26} />
        </span>
      )}
      {objectUrl && (
        <img
          className={loaded ? "thumbnail-ready" : "thumbnail-pending"}
          src={objectUrl}
          alt={alt}
          onLoad={() => setLoaded(true)}
          onError={onError}
        />
      )}
    </>
  );
}

const DOWNLOAD_CHUNK_SIZE = 512 * 1024;

interface DownloadProgress {
  phase: "preparing" | "downloading" | "finalizing";
  loaded: number;
  total: number;
  percent: number;
  speed: number;
  etaSeconds: number | null;
  chunkIndex: number;
  chunkCount: number;
  waiting: boolean;
}

interface TransferSample { at: number; loaded: number; }

export async function readEncryptedChunks(
  item: MediaItem,
  mediaCrypto: ReturnType<typeof useMediaCrypto>,
  consume: (data: ArrayBuffer) => Promise<void> | void,
  signal?: AbortSignal,
  onProgress?: (progress: DownloadProgress) => void,
) {
  const chunkCount = Math.ceil(item.size / DOWNLOAD_CHUNK_SIZE);
  const samples: TransferSample[] = [{ at: performance.now(), loaded: 0 }];
  let loaded = 0;
  for (let offset = 0, chunkIndex = 1; offset < item.size; offset += DOWNLOAD_CHUNK_SIZE, chunkIndex += 1) {
    signal?.throwIfAborted();
    const length = Math.min(DOWNLOAD_CHUNK_SIZE, item.size - offset);
    const before = transferProgress(item.size, loaded, chunkIndex, chunkCount, samples, true);
    onProgress?.(before);
    const waitingTimer = onProgress ? window.setInterval(() => {
      onProgress(transferProgress(item.size, loaded, chunkIndex, chunkCount, samples, true, performance.now()));
    }, 1_000) : undefined;
    let result: Awaited<ReturnType<typeof mediaCrypto.fetchAndDecrypt>>;
    try {
      result = await mediaCrypto.fetchAndDecrypt(encryptedChunkUrl(item, offset, length), signal);
    } finally {
      if (waitingTimer !== undefined) window.clearInterval(waitingTimer);
    }
    signal?.throwIfAborted();
    await consume(result.data);
    loaded += result.data.byteLength;
    samples.push({ at: performance.now(), loaded });
    trimTransferSamples(samples);
    onProgress?.(transferProgress(item.size, loaded, chunkIndex, chunkCount, samples, false));
  }
}

function transferProgress(
  total: number,
  loaded: number,
  chunkIndex: number,
  chunkCount: number,
  samples: TransferSample[],
  waiting: boolean,
  measuredAt?: number,
): DownloadProgress {
  const last = samples[samples.length - 1];
  const first = samples[0];
  const metrics = calculateTransferMetrics(total, loaded, last.loaded - first.loaded, (measuredAt ?? last.at) - first.at);
  return {
    phase: "downloading",
    loaded,
    total,
    percent: metrics.percent,
    speed: metrics.speed,
    etaSeconds: metrics.etaSeconds,
    chunkIndex,
    chunkCount,
    waiting,
  };
}

export function calculateTransferMetrics(total: number, loaded: number, sampledBytes: number, elapsedMs: number) {
  const speed = elapsedMs > 0 ? Math.max(0, sampledBytes / (elapsedMs / 1000)) : 0;
  return {
    percent: total > 0 ? Math.min(100, Math.max(0, (loaded / total) * 100)) : 100,
    speed,
    etaSeconds: speed > 0 ? Math.max(0, (total - loaded) / speed) : null,
  };
}

function trimTransferSamples(samples: TransferSample[]) {
  const cutoff = performance.now() - 15_000;
  while (samples.length > 2 && samples[1].at < cutoff) samples.shift();
}

async function loadEncryptedBlob(
  item: MediaItem,
  mediaCrypto: ReturnType<typeof useMediaCrypto>,
  signal?: AbortSignal,
  onProgress?: (progress: DownloadProgress) => void,
) {
  const chunks: ArrayBuffer[] = [];
  await readEncryptedChunks(item, mediaCrypto, (data) => { chunks.push(data); }, signal, onProgress);
  return new Blob(chunks, { type: item.mime_type });
}

interface FileWriter {
  write(data: Uint8Array): Promise<void>;
  close(): Promise<void>;
  abort(): Promise<void>;
}

interface FilePickerWindow extends Window {
  showSaveFilePicker?: (options: { suggestedName: string }) => Promise<{
    createWritable(): Promise<FileWriter>;
  }>;
}

function encryptedChunkUrl(item: MediaItem, offset: number, length: number) {
  return `/api/media/${item.id}/encrypted-chunk?account=${encodeURIComponent(item.account_id)}&offset=${offset}&length=${length}`;
}

async function downloadEncryptedMedia(
  item: MediaItem,
  mediaCrypto: ReturnType<typeof useMediaCrypto>,
  signal: AbortSignal,
  onProgress: (progress: DownloadProgress) => void,
) {
  const picker = (window as FilePickerWindow).showSaveFilePicker;
  const initial: DownloadProgress = { phase: "preparing", loaded: 0, total: item.size, percent: 0, speed: 0, etaSeconds: null, chunkIndex: 0, chunkCount: Math.ceil(item.size / DOWNLOAD_CHUNK_SIZE), waiting: false };
  onProgress(initial);
  if (picker) {
    const handle = await picker({ suggestedName: item.filename });
    signal.throwIfAborted();
    const writable = await handle.createWritable();
    try {
      await readEncryptedChunks(item, mediaCrypto, (data) => writable.write(new Uint8Array(data)), signal, onProgress);
      onProgress({ ...initial, phase: "finalizing", loaded: item.size, percent: 100 });
      await writable.close();
    } catch (reason) {
      await writable.abort().catch(() => undefined);
      throw reason;
    }
    return;
  }

  const blob = await loadEncryptedBlob(item, mediaCrypto, signal, onProgress);
  signal.throwIfAborted();
  onProgress({ ...initial, phase: "finalizing", loaded: item.size, percent: 100 });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = item.filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

function EncryptedDownloadButton({ item, iconOnly = false }: { item: MediaItem; iconOnly?: boolean }) {
  const mediaCrypto = useMediaCrypto();
  const controllerRef = useRef<AbortController | null>(null);
  const [progress, setProgress] = useState<DownloadProgress | null>(null);
  const [canceling, setCanceling] = useState(false);
  const [error, setError] = useState("");
  const download = useCallback(async () => {
    const controller = new AbortController();
    controllerRef.current = controller;
    setCanceling(false);
    setProgress({ phase: "preparing", loaded: 0, total: item.size, percent: 0, speed: 0, etaSeconds: null, chunkIndex: 0, chunkCount: Math.ceil(item.size / DOWNLOAD_CHUNK_SIZE), waiting: false });
    setError("");
    try {
      await downloadEncryptedMedia(item, mediaCrypto, controller.signal, setProgress);
    } catch (reason) {
      if ((reason as DOMException)?.name !== "AbortError") setError(errorMessage(reason));
    } finally {
      controllerRef.current = null;
      setCanceling(false);
      setProgress(null);
    }
  }, [item, mediaCrypto]);
  const cancel = useCallback(() => {
    setCanceling(true);
    controllerRef.current?.abort();
  }, []);
  const busy = progress !== null;
  return (
    <>
      <button className={iconOnly ? "icon-button" : "button primary"} disabled={busy} onClick={() => void download()} aria-label="下载" title="下载" type="button">
        {busy ? <LoaderCircle className="spin" size={iconOnly ? 20 : 18} /> : <Download size={iconOnly ? 20 : 18} />}
        {!iconOnly && "下载文件"}
      </button>
      {progress && <DownloadProgressDialog item={item} progress={progress} canceling={canceling} onCancel={cancel} />}
      {error && <span className="form-error" role="alert">{error}</span>}
    </>
  );
}

function DownloadProgressDialog({ item, progress, canceling, onCancel }: { item: MediaItem; progress: DownloadProgress; canceling: boolean; onCancel: () => void }) {
  const status = canceling
    ? "正在取消下载"
    : progress.phase === "preparing"
      ? "正在准备保存位置"
      : progress.phase === "finalizing"
        ? "正在完成文件写入"
        : progress.waiting
          ? `正在获取分块 ${progress.chunkIndex} / ${progress.chunkCount}`
          : `已解密分块 ${progress.chunkIndex} / ${progress.chunkCount}`;
  return (
    <div className="download-progress-backdrop" role="presentation">
      <section className="download-progress-dialog" role="dialog" aria-modal="true" aria-labelledby="download-progress-title" aria-live="polite">
        <div className="download-progress-heading">
          <span className="download-progress-icon"><Download size={22} /></span>
          <span><strong id="download-progress-title">{item.filename}</strong><small>{status}</small></span>
          {(progress.phase === "preparing" || progress.waiting) && <LoaderCircle className="spin" size={20} />}
        </div>
        <div className="download-progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress.percent)}>
          <span style={{ width: `${progress.percent}%` }} />
        </div>
        <div className="download-progress-stats">
          <strong>{progress.percent.toFixed(progress.percent < 10 ? 1 : 0)}%</strong>
          <span>{formatBytes(progress.loaded)} / {formatBytes(progress.total)}</span>
          <span>{progress.phase === "finalizing" ? "下载完成" : progress.speed > 0 ? `${formatBytes(Math.round(progress.speed))}/s` : "正在计算速度"}</span>
          <span>{progress.phase === "finalizing" ? "正在写入磁盘" : formatRemainingTime(progress.etaSeconds)}</span>
        </div>
        <button className="button secondary wide" disabled={canceling || progress.phase === "finalizing"} onClick={onCancel} type="button">
          {canceling ? <LoaderCircle className="spin" size={18} /> : <X size={18} />}{canceling ? "正在取消" : "取消下载"}
        </button>
      </section>
    </div>
  );
}

export function formatRemainingTime(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "剩余时间计算中";
  if (seconds < 60) return `约剩余 ${Math.max(1, Math.ceil(seconds))} 秒`;
  if (seconds < 3600) return `约剩余 ${Math.ceil(seconds / 60)} 分钟`;
  return `约剩余 ${Math.floor(seconds / 3600)} 小时 ${Math.ceil((seconds % 3600) / 60)} 分钟`;
}

function ViewerAudio({ item }: { item: MediaItem }) {
  const mediaCrypto = useMediaCrypto();
  const [source, setSource] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    let objectUrl = "";
    void loadEncryptedBlob(item, mediaCrypto).then((blob) => {
      if (cancelled) return;
      objectUrl = URL.createObjectURL(blob);
      setSource(objectUrl);
    }).catch((reason) => { if (!cancelled) setError(errorMessage(reason)); });
    return () => { cancelled = true; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [item, mediaCrypto]);
  return (
    <div className="audio-player">
      <AudioLines size={64} />
      {!source && !error && <LoaderCircle className="spin" size={28} />}
      {error ? <span className="form-error" role="alert">{error}</span> : source ? <audio controls autoPlay src={source} /> : null}
    </div>
  );
}
function ViewerImage({ item }: { item: MediaItem }) {
  const mediaCrypto = useMediaCrypto();
  const [source, setSource] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    void loadEncryptedBlob(item, mediaCrypto).then((blob) => {
      if (!cancelled) setSource(URL.createObjectURL(blob));
    }).catch((reason) => {
      if (!cancelled) setError(errorMessage(reason));
    });
    return () => {
      cancelled = true;
      setSource((current) => {
        if (current) URL.revokeObjectURL(current);
        return "";
      });
    };
  }, [item, mediaCrypto]);
  return (
    <div className="viewer-media-loader">
      {!source && !error && <LoaderCircle className="spin" size={34} aria-label="正在解密图片" />}
      {error ? (
        <div className="viewer-media-error"><ImageIcon size={42} /><span>图片解密失败：{error}</span></div>
      ) : source ? <img src={source} alt={item.title} /> : null}
    </div>
  );
}

function ViewerVideo({ item }: { item: MediaItem }) {
  const mediaCrypto = useMediaCrypto();
  const [source, setSource] = useState("");
  const [error, setError] = useState("");
  const [progress, setProgress] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let mediaSource: MediaSource | undefined;
    let objectUrl = "";
    async function stream() {
      if (!MediaSource.isTypeSupported(item.mime_type)) {
        throw new Error("当前浏览器不支持该视频格式的加密流播放");
      }
      mediaSource = new MediaSource();
      objectUrl = URL.createObjectURL(mediaSource);
      if (!cancelled) setSource(objectUrl);
      await new Promise<void>((resolve, reject) => {
        mediaSource!.addEventListener("sourceopen", () => resolve(), { once: true });
        mediaSource!.addEventListener("error", () => reject(new Error("视频解密流初始化失败")), { once: true });
      });
      const buffer = mediaSource.addSourceBuffer(item.mime_type);
      const chunkSize = 512 * 1024;
      for (let offset = 0; offset < item.size; offset += chunkSize) {
        const length = Math.min(chunkSize, item.size - offset);
        const url = `/api/media/${item.id}/encrypted-chunk?account=${encodeURIComponent(item.account_id)}&offset=${offset}&length=${length}`;
        const result = await mediaCrypto.fetchAndDecrypt(url);
        await appendBuffer(buffer, result.data);
        if (!cancelled) setProgress(Math.min(100, ((offset + length) / item.size) * 100));
      }
      if (!cancelled && mediaSource.readyState === "open") mediaSource.endOfStream();
    }
    void stream().catch((reason) => {
      if (!cancelled) setError(errorMessage(reason));
    });
    return () => {
      cancelled = true;
      if (mediaSource?.readyState === "open" && !mediaSource.sourceBuffers[0]?.updating) {
        try { mediaSource.endOfStream(); } catch { /* source is already closing */ }
      }
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [item, mediaCrypto]);
  return (
    <div className="viewer-media-loader">
      {!source && !error && <LoaderCircle className="spin" size={34} aria-label={`正在解密视频 ${progress.toFixed(0)}%`} />}
      {source && progress === 0 && !error && <span className="thumbnail-loading"><LoaderCircle className="spin" size={34} aria-label="Decrypting first video chunk" /></span>}
      {error ? (
        <div className="viewer-media-error">
          <Film size={42} /><span>加密视频播放失败：{error}</span>
          <EncryptedDownloadButton item={item} />
        </div>
      ) : source ? <video controls autoPlay src={source} /> : null}
    </div>
  );
}

function appendBuffer(buffer: SourceBuffer, data: ArrayBuffer): Promise<void> {
  return new Promise((resolve, reject) => {
    const done = () => { buffer.removeEventListener("updateend", done); buffer.removeEventListener("error", failed); resolve(); };
    const failed = () => { buffer.removeEventListener("updateend", done); buffer.removeEventListener("error", failed); reject(new Error("视频分块写入失败")); };
    buffer.addEventListener("updateend", done, { once: true });
    buffer.addEventListener("error", failed, { once: true });
    buffer.appendBuffer(data);
  });
}
function MediaSkeleton() {
  return (
    <div className="media-grid" aria-label="正在加载">
      {Array.from({ length: 12 }, (_, index) => (
        <div className="media-skeleton" key={index}>
          <div className="skeleton poster" />
          <div className="skeleton line wide" />
          <div className="skeleton line short" />
        </div>
      ))}
    </div>
  );
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

function downloadUrl(streamUrl: string): string {
  return `${streamUrl}${streamUrl.includes("?") ? "&" : "?"}download=true`;
}

function formatDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = Math.floor(seconds % 60);
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${minutes}:${String(rest).padStart(2, "0")}`;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", year: "numeric" }).format(new Date(value));
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}
