import { useCallback, useEffect, useMemo, useState } from "react";
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
  return (
    <button className="media-card" onClick={onOpen} type="button" aria-label={`打开 ${item.title}`}>
      <div className={`media-poster kind-${item.kind}`}>
        {item.thumbnail_url && !thumbnailFailed ? (
          <ThumbnailImage
            src={item.thumbnail_url}
            alt=""
            onError={() => setThumbnailFailed(true)}
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
            <a className="icon-button" href={downloadUrl(item.stream_url)} aria-label="下载文件" title="下载">
              <Download size={20} />
            </a>
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
          {item.kind === "audio" && (
            <div className="audio-player">
              <AudioLines size={64} />
              <audio controls autoPlay src={item.stream_url} />
            </div>
          )}
          {item.kind === "file" && (
            <div className="file-download">
              <FileKindIcon kind={item.kind} mime={item.mime_type} />
              <p>{item.filename}</p>
              <a className="button primary" href={downloadUrl(item.stream_url)}>
                <Download size={18} />下载文件
              </a>
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
  const [loaded, setLoaded] = useState(false);
  useEffect(() => setLoaded(false), [src]);
  return (
    <>
      {!loaded && (
        <span className="thumbnail-loading" aria-label="正在加载缩略图">
          <LoaderCircle className="spin" size={26} />
        </span>
      )}
      <img
        className={loaded ? "thumbnail-ready" : "thumbnail-pending"}
        src={src}
        alt={alt}
        loading="lazy"
        onLoad={() => setLoaded(true)}
        onError={onError}
      />
    </>
  );
}

function ViewerImage({ item }: { item: MediaItem }) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  return (
    <div className="viewer-media-loader">
      {!loaded && !failed && <LoaderCircle className="spin" size={34} aria-label="正在加载图片" />}
      {failed ? (
        <div className="viewer-media-error"><ImageIcon size={42} /><span>图片加载失败</span></div>
      ) : (
        <img src={item.stream_url} alt={item.title} onLoad={() => setLoaded(true)} onError={() => setFailed(true)} />
      )}
    </div>
  );
}

function ViewerVideo({ item }: { item: MediaItem }) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  return (
    <div className="viewer-media-loader">
      {!loaded && !failed && <LoaderCircle className="spin" size={34} aria-label="正在加载视频" />}
      {failed ? (
        <div className="viewer-media-error"><Film size={42} /><span>视频加载失败</span></div>
      ) : (
        <video
          controls
          autoPlay
          poster={item.thumbnail_url || undefined}
          src={item.stream_url}
          onLoadedData={() => setLoaded(true)}
          onError={() => setFailed(true)}
        />
      )}
    </div>
  );
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
