import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownAZ,
  ArrowUpAZ,
  AudioLines,
  Bell,
  CalendarDays,
  Check,
  ChevronRight,
  Download,
  EyeOff,
  FileArchive,
  FileText,
  Film,
  Folder,
  FolderOpen,
  FolderPlus,
  Globe2,
  HardDriveDownload,
  Home,
  Image as ImageIcon,
  LayoutGrid,
  List,
  LoaderCircle,
  LogOut,
  Music2,
  Pencil,
  Play,
  Search,
  Settings,
  Shield,
  Trash2,
  X,
} from "lucide-react";
import { api, errorMessage } from "./api";
import { useMediaCrypto } from "./MediaCrypto";
import { LanguageSelector, translateNow, useI18n } from "./I18n";
import ThemeSelector from "./ThemeSelector";
import type {
  AccountStatus,
  FolderItem,
  MediaItem,
  MediaKind,
  MediaPage,
  MediaVisibility,
  NotificationItem,
  NotificationPage,
  TimelineMonth,
  TimelineResponse,
} from "./types";

const filters: Array<{ value: MediaKind; zh: string; en: string; icon: typeof Home }> = [
  { value: "all", zh: "全部", en: "All", icon: Home },
  { value: "video", zh: "视频", en: "Videos", icon: Film },
  { value: "image", zh: "图片", en: "Images", icon: ImageIcon },
  { value: "audio", zh: "音频", en: "Audio", icon: Music2 },
  { value: "file", zh: "文件", en: "Files", icon: FolderOpen },
];

const VIEW_MODE_KEY = "savedstream-view-mode";

export default function GalleryPage({ isAdmin = false }: { isAdmin?: boolean }) {
  const { tr } = useI18n();
  const mediaCrypto = useMediaCrypto();
  const [accounts, setAccounts] = useState<AccountStatus[]>([]);
  const [account, setAccount] = useState("");
  const [items, setItems] = useState<MediaItem[]>([]);
  const [scope, setScope] = useState<"public" | "private" | "hidden" | "all">(isAdmin ? "all" : "public");
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [dateRange, setDateRange] = useState<{ from: string; to: string } | null>(null);
  const [timelinePosition, setTimelinePosition] = useState(0);
  const [kind, setKind] = useState<MediaKind>("all");
  const [order, setOrder] = useState<"newest" | "oldest">("newest");
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [cursor, setCursor] = useState<string | number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<MediaItem | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);
  const [viewMode, setViewMode] = useState<"grid" | "list">(() => {
    try {
      return window.localStorage.getItem(VIEW_MODE_KEY) === "list" ? "list" : "grid";
    } catch {
      return "grid";
    }
  });
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [folderId, setFolderId] = useState<number | null>(null);
  const [folders, setFolders] = useState<FolderItem[]>([]);
  const [foldersVersion, setFoldersVersion] = useState(0);
  const [bulkBusy, setBulkBusy] = useState(false);

  useEffect(() => setScope(isAdmin ? "all" : "public"), [isAdmin]);

  useEffect(() => {
    try {
      window.localStorage.setItem(VIEW_MODE_KEY, viewMode);
    } catch {
      // Storage can be unavailable in hardened browser contexts.
    }
  }, [viewMode]);

  useEffect(() => {
    void api<{ items: AccountStatus[]; default_account: string }>("/api/accounts").then((result) => {
      const nextAccounts = Array.isArray(result.items) ? result.items : [];
      setAccounts(nextAccounts);
      setAccount((current) => current || "all");
    }).catch((reason) => setError(errorMessage(reason)));
  }, []);

  const loadFolders = useCallback(async () => {
    try {
      const result = await api<{ items: FolderItem[] }>("/api/folders");
      setFolders(Array.isArray(result.items) ? result.items : []);
    } catch {
      setFolders([]);
    }
  }, []);

  useEffect(() => {
    void loadFolders();
  }, [loadFolders, foldersVersion]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 320);
    return () => window.clearTimeout(timer);
  }, [query]);

  const loadMedia = useCallback(async (nextCursor: string | number | null = null, silent = false) => {
    if (nextCursor !== null) setLoadingMore(true);
    else if (!silent) setLoading(true);
    if (!silent) setError("");
    try {
      const params = new URLSearchParams({
        limit: "36",
        kind,
        order,
        q: debouncedQuery,
        scope: isAdmin ? scope : "public",
      });
      if (account && account !== "all") params.set("account", account);
      if (folderId !== null) params.set("folder_id", String(folderId));
      if (nextCursor !== null) params.set("cursor", String(nextCursor));
      if (dateRange) {
        params.set("from", dateRange.from);
        params.set("to", dateRange.to);
      }
      const page = await api<MediaPage>(`/api/media?${params}`);
      const pageItems = Array.isArray(page.items) ? page.items.map(normalizeMediaItem) : [];
      setItems((current) => {
        if (nextCursor !== null) return [...current, ...pageItems];
        // Keep the previous array reference when nothing changed so the
        // silent 10s refresh does not force the grid/list to re-render
        // (and thumbnails to re-mount) over and over.
        if (sameMediaItems(current, pageItems)) return current;
        return pageItems;
      });
      setCursor(page.next_cursor ?? null);
      setHasMore(Boolean(page.has_more));
    } catch (reason) {
      if (!silent) {
        setError(errorMessage(reason));
        if (nextCursor === null) setItems([]);
      }
    } finally {
      if (!silent) setLoading(false);
      setLoadingMore(false);
    }
  }, [account, dateRange, debouncedQuery, folderId, isAdmin, kind, order, scope]);

  const loadTimeline = useCallback(async () => {
    if (!account) return;
    try {
      const params = new URLSearchParams({
        kind,
        q: debouncedQuery,
        scope: isAdmin ? scope : "public",
      });
      if (account && account !== "all") params.set("account", account);
      const result = await api<TimelineResponse>(`/api/media/timeline?${params}`);
      setTimeline(result);
    } catch {
      setTimeline(null);
    }
  }, [account, debouncedQuery, isAdmin, kind, scope]);

  useEffect(() => {
    setCursor(null);
    setSelectedKeys(new Set());
    loadMedia();
  }, [loadMedia]);

  useEffect(() => {
    void loadTimeline();
  }, [loadTimeline]);

  useEffect(() => {
    if (!account) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible" || selected) return;
      void loadMedia(null, true);
      void loadTimeline();
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [account, loadMedia, loadTimeline, selected]);

  const flatMonths = useMemo(
    () => (Array.isArray(timeline?.years) ? timeline.years : [])
      .flatMap((year) => Array.isArray(year.months) ? year.months : [])
      .filter((month) => typeof month.month === "string" && month.month.length >= 7),
    [timeline],
  );
  const groupedItems = useMemo(() => {
    const groups = new Map<string, MediaItem[]>();
    for (const item of items) {
      const day = mediaDay(item.date);
      const group = groups.get(day) || [];
      group.push(item);
      groups.set(day, group);
    }
    return [...groups.entries()];
  }, [items]);

  const itemKey = useCallback((item: MediaItem) => `${item.account_id}:${item.id}`, []);

  const selectedItems = useMemo(
    () => items.filter((item) => selectedKeys.has(itemKey(item))),
    [items, selectedKeys, itemKey],
  );

  const toggleSelect = useCallback((item: MediaItem) => {
    const key = itemKey(item);
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }, [itemKey]);

  async function bulkSetVisibility(visibility: MediaVisibility) {
    if (!selectedItems.length || bulkBusy) return;
    setBulkBusy(true);
    setError("");
    try {
      await api("/api/admin/media/visibility", {
        method: "POST",
        body: JSON.stringify({
          visibility,
          items: selectedItems.map((item) => ({ account_id: item.account_id, message_id: item.id })),
        }),
      });
      setSelectedKeys(new Set());
      await loadMedia(null, true);
      setNotice(tr("批量可见性已更新。", "Bulk visibility updated."));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBulkBusy(false);
    }
  }

  async function bulkDelete() {
    if (!selectedItems.length || bulkBusy) return;
    const confirmed = window.confirm(tr(
      `确认删除选中的 ${selectedItems.length} 项资源吗？该操作会同时删除 Telegram 消息与本地缓存。`,
      `Delete the ${selectedItems.length} selected items? Telegram messages and local cache are also removed.`,
    ));
    if (!confirmed) return;
    setBulkBusy(true);
    setError("");
    try {
      for (const item of selectedItems) {
        await api(`/api/admin/media/${item.id}?account=${encodeURIComponent(item.account_id)}`, {
          method: "DELETE",
          body: JSON.stringify({ reason: tr("管理员从媒体库批量删除", "Removed by administrator from the library") }),
        });
      }
      setSelectedKeys(new Set());
      setFoldersVersion((value) => value + 1);
      await loadMedia(null, true);
      setNotice(tr("批量删除完成。", "Bulk deletion complete."));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBulkBusy(false);
    }
  }

  async function moveSelectedToFolder(targetFolderId: number) {
    if (!selectedItems.length || bulkBusy) return;
    setBulkBusy(true);
    setError("");
    try {
      await api(`/api/admin/folders/${targetFolderId}/items`, {
        method: "PUT",
        body: JSON.stringify({ items: selectedItems.map((item) => ({ account_id: item.account_id, message_id: item.id })) }),
      });
      setSelectedKeys(new Set());
      setFoldersVersion((value) => value + 1);
      setNotice(tr("已移动到文件夹。", "Moved to folder."));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBulkBusy(false);
    }
  }

  const deleteItem = useCallback(async (item: MediaItem) => {
    const confirmed = window.confirm(tr(
      `确认删除「${item.title}」吗？该操作会同时删除 Telegram 消息与本地缓存。`,
      `Delete "${item.title}"? This also removes the Telegram message and local cache.`,
    ));
    if (!confirmed) return;
    setError("");
    try {
      await api(`/api/admin/media/${item.id}?account=${encodeURIComponent(item.account_id)}`, {
        method: "DELETE",
        body: JSON.stringify({ reason: tr("管理员从媒体库删除", "Removed by administrator from the library") }),
      });
      const key = itemKey(item);
      setItems((current) => current.filter((currentItem) => itemKey(currentItem) !== key));
      setSelectedKeys((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
      setFoldersVersion((value) => value + 1);
      setNotice(tr("资源已删除。", "Media deleted."));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }, [itemKey, tr]);

  const setItemVisibility = useCallback(async (item: MediaItem, visibility: MediaVisibility) => {
    setError("");
    try {
      const updated = await api<MediaItem>(`/api/admin/media/${item.id}/visibility?account=${encodeURIComponent(item.account_id)}`, {
        method: "PATCH",
        body: JSON.stringify({ visibility }),
      });
      const key = itemKey(item);
      setItems((current) => current.map((currentItem) => itemKey(currentItem) === key
        ? { ...currentItem, visibility: updated.visibility, hidden: updated.hidden }
        : currentItem));
      setNotice(tr("可见性已更新。", "Visibility updated."));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }, [itemKey, tr]);

  const folderPath = useMemo(() => folderChain(folders, folderId), [folderId, folders]);
  const currentFolderName = folderPath && folderPath.length > 0 ? folderPath[folderPath.length - 1].name : null;

  function selectFolder(nextFolderId: number | null) {
    setFolderId(nextFolderId);
    setDateRange(null);
  }

  const title = useMemo(() => {
    const selectedFilter = filters.find((item) => item.value === kind);
    return selectedFilter ? tr(selectedFilter.zh, selectedFilter.en) : tr("全部", "All");
  }, [kind, tr]);

  const heading = folderPath && folderPath.length
    ? folderPath.map((folder) => folder.name).join(" / ")
    : dateRange
      ? `${dateRange.from.slice(0, 7)} ${tr("时间相册", "Timeline")}`
      : debouncedQuery
        ? `“${debouncedQuery}”`
        : title;

  function chooseMonth(month: TimelineMonth, index?: number) {
    const from = `${month.month}-01`;
    const [year, monthNumber] = month.month.split("-").map(Number);
    const lastDay = new Date(Date.UTC(year, monthNumber, 0)).toISOString().slice(0, 10);
    setDateRange({ from, to: lastDay });
    setTimelinePosition(index ?? Math.max(0, flatMonths.findIndex((candidate) => candidate.month === month.month)));
  }

  async function logoutPublicSession() {
    setLoggingOut(true);
    try {
      await mediaCrypto.reset(true);
      await api<{ ok: boolean }>("/api/access/logout", { method: "POST" });
    } catch {
      // Local session material is already cleared; a reload will reconcile
      // any server-side session that expired concurrently.
    } finally {
      window.location.replace("/");
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label={tr("SavedStream 首页", "SavedStream home")}>
          <span className="brand-mark"><Play size={16} fill="currentColor" /></span>
          <span>SavedStream</span>
        </a>
        <label className="search-field">
          <span className="sr-only">{tr("搜索收藏夹", "Search media")}</span>
          <Search size={18} aria-hidden="true" />
          <input
            type="search"
            placeholder={tr("搜索收藏夹", "Search media")}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {query && (
            <button aria-label={tr("清除搜索", "Clear search")} title={tr("清除", "Clear")} onClick={() => setQuery("")} type="button">
              <X size={17} />
            </button>
          )}
        </label>
        <div className="topbar-actions">
          <ThemeSelector />
          <LanguageSelector compact />
          <MailboxBell />
          <a className="icon-button" href="/admin" aria-label={tr("管理员设置", "Administrator settings")} title={tr("管理员设置", "Administrator settings")}>
            <Settings size={21} />
          </a>
          {!isAdmin && (
            <button className="icon-button" disabled={loggingOut} onClick={() => void logoutPublicSession()} aria-label={tr("退出公开相册", "Sign out of public album")} title={tr("退出", "Sign out")} type="button">
              {loggingOut ? <LoaderCircle className="spin" size={20} /> : <LogOut size={20} />}
            </button>
          )}
        </div>
      </header>

      <aside className="sidebar" aria-label={tr("媒体分类", "Media categories")}>
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
                <span>{tr(filter.zh, filter.en)}</span>
              </button>
            );
          })}
        </nav>
        <div className="sidebar-footer">
          <HardDriveDownload size={18} />
          <span>{tr("按需缓存", "On-demand cache")}</span>
        </div>
      </aside>

      <TimelineRail
        months={flatMonths}
        position={timelinePosition}
        onPositionChange={(position) => {
          setTimelinePosition(position);
          const month = flatMonths[position];
          if (month) chooseMonth(month, position);
        }}
        onClear={() => setDateRange(null)}
      />

      <main className="library" id="main-content">
        <div className="mobile-filters" aria-label={tr("媒体分类", "Media categories")}>
          {filters.map((filter) => (
            <button
              className={kind === filter.value ? "active" : ""}
              key={filter.value}
              onClick={() => setKind(filter.value)}
              type="button"
            >
              {tr(filter.zh, filter.en)}
            </button>
          ))}
        </div>
        <div className="library-heading">
          <div className="library-title-block">
            <FolderBreadcrumb folders={folders} currentId={folderId} onNavigate={selectFolder} />
            <h1>{currentFolderName || heading}</h1>
            {!loading && <span className="result-count">{items.length} {tr("项", "items")}</span>}
          </div>
          <div className="view-mode-toggle" role="group" aria-label={tr("视图模式", "View mode")}>
            <button className={viewMode === "grid" ? "active" : ""} onClick={() => setViewMode("grid")} aria-label={tr("网格视图", "Grid view")} title={tr("网格视图", "Grid view")} type="button">
              <LayoutGrid size={17} />
            </button>
            <button className={viewMode === "list" ? "active" : ""} onClick={() => setViewMode("list")} aria-label={tr("列表视图", "List view")} title={tr("列表视图", "List view")} type="button">
              <List size={17} />
            </button>
          </div>
          {isAdmin && (
            <label className="sort-control album-scope-control">
              <Shield size={17} />
              <select value={scope} onChange={(event) => { setScope(event.target.value as typeof scope); setDateRange(null); }}>
                <option value="all">{tr("全部相册", "All albums")}</option>
                <option value="public">{tr("公开相册", "Public album")}</option>
                <option value="private">{tr("私人相册", "Private album")}</option>
                <option value="hidden">{tr("已隐藏", "Hidden")}</option>
              </select>
            </label>
          )}
          {accounts.length > 0 && (
            <label className="sort-control">
              <span className="sr-only">{tr("托管账号", "Managed account")}</span>
              <select value={account} onChange={(event) => setAccount(event.target.value)}>
                <option value="all">{tr("全部账号", "All accounts")}</option>
                {accounts.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select>
            </label>
          )}
          <label className="sort-control">
            <span className="sr-only">{tr("时间排序", "Sort by time")}</span>
            {order === "newest" ? <ArrowDownAZ size={17} /> : <ArrowUpAZ size={17} />}
            <select value={order} onChange={(event) => setOrder(event.target.value as "newest" | "oldest")}>
              <option value="newest">{tr("最新优先", "Newest first")}</option>
              <option value="oldest">{tr("最早优先", "Oldest first")}</option>
            </select>
          </label>
        </div>

        {isAdmin && selectedKeys.size > 0 && (
          <div className="bulk-toolbar" role="toolbar" aria-label={tr("批量操作", "Bulk actions")}>
            <span className="bulk-toolbar-count">{tr("已选择", "Selected")} {selectedKeys.size} {tr("项", "items")}</span>
            <button className="button secondary" disabled={bulkBusy} onClick={() => void bulkSetVisibility("public")} type="button"><Globe2 size={15} />{tr("设为公开", "Make public")}</button>
            <button className="button secondary" disabled={bulkBusy} onClick={() => void bulkSetVisibility("private")} type="button"><Shield size={15} />{tr("设为私有", "Make private")}</button>
            <button className="button secondary" disabled={bulkBusy} onClick={() => void bulkSetVisibility("hidden")} type="button"><EyeOff size={15} />{tr("设为隐藏", "Hide")}</button>
            <MoveToFolderPicker folders={folders} disabled={bulkBusy} onPick={(id) => void moveSelectedToFolder(id)} />
            <button className="button danger" disabled={bulkBusy} onClick={() => void bulkDelete()} type="button"><Trash2 size={15} />{tr("删除", "Delete")}</button>
            <button className="button ghost" disabled={bulkBusy} onClick={() => setSelectedKeys(new Set())} type="button">{tr("清除选择", "Clear selection")}</button>
          </div>
        )}
        {notice && <p className="gallery-notice" role="status">{notice}</p>}

        <FolderShelf
          folders={folders}
          activeFolderId={folderId}
          admin={isAdmin}
          onSelect={selectFolder}
          onChanged={() => setFoldersVersion((value) => value + 1)}
          viewMode={viewMode}
        />

        {loading ? (
          <MediaSkeleton />
        ) : error ? (
          <div className="state-block" role="alert">
            <FileArchive size={38} />
            <h2>{tr("无法读取收藏夹", "Unable to load the media library")}</h2>
            <p>{error}</p>
            <button className="button secondary" onClick={() => loadMedia()} type="button">{tr("重新加载", "Reload")}</button>
          </div>
        ) : items.length === 0 ? (
          <div className="state-block">
            {timeline?.index?.status === "running" ? <LoaderCircle className="spin" size={38} /> : <FolderOpen size={38} />}
            <h2>{timeline?.index?.status === "running" ? tr("正在建立媒体索引", "Building media index") : folderId !== null ? tr("该文件夹是空的", "This folder is empty") : tr("没有找到媒体", "No media found")}</h2>
            {timeline?.index?.status === "running" && <p>{tr("已索引", "Indexed")} {timeline.index.indexed_count} {tr("项，完成后会自动显示。", "items. They will appear automatically when indexing completes.")}</p>}
            {hasMore && cursor !== null && (
              <button className="button secondary" disabled={loadingMore} onClick={() => loadMedia(cursor)} type="button">
                {loadingMore && <LoaderCircle className="spin" size={18} />}
                {tr("继续查找", "Continue searching")}
              </button>
            )}
          </div>
        ) : viewMode === "list" ? (
          <>
            <MediaListView
              items={items}
              isAdmin={isAdmin}
              selectedKeys={selectedKeys}
              onToggleSelect={toggleSelect}
              onOpen={setSelected}
              onDelete={deleteItem}
              onVisibility={setItemVisibility}
              busy={bulkBusy}
            />
            {hasMore && cursor && (
              <div className="load-more-row">
                <button className="button secondary" disabled={loadingMore} onClick={() => loadMedia(cursor)} type="button">
                  {loadingMore && <LoaderCircle className="spin" size={18} />}
                  {tr("加载更多", "Load more")}
                </button>
              </div>
            )}
          </>
        ) : (
          <>
            <section className="date-album-list" aria-label={tr("按日期分组的收藏夹媒体", "Media grouped by date")}>
              {groupedItems.map(([day, dayItems]) => (
                <section className="date-album" key={day}>
                  <div className="date-album-heading"><CalendarDays size={18} /><h2>{formatDay(day)}</h2><span>{dayItems.length} {tr("项", "items")}</span></div>
                  <div className="media-grid">
                    {dayItems.map((item) => {
                      const key = itemKey(item);
                      return (
                        <MediaCard
                          item={item}
                          key={key}
                          onOpen={() => setSelected(item)}
                          isAdmin={isAdmin}
                          selectable={isAdmin}
                          selected={selectedKeys.has(key)}
                          onToggleSelect={() => toggleSelect(item)}
                        />
                      );
                    })}
                  </div>
                </section>
              ))}
            </section>
            {hasMore && cursor && (
              <div className="load-more-row">
                <button className="button secondary" disabled={loadingMore} onClick={() => loadMedia(cursor)} type="button">
                  {loadingMore && <LoaderCircle className="spin" size={18} />}
                  {tr("加载更多", "Load more")}
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


function folderChain(folders: FolderItem[], folderId: number | null): FolderItem[] {
  if (folderId === null) return [];
  const byId = new Map(folders.map((folder) => [folder.id, folder]));
  const chain: FolderItem[] = [];
  let currentId: number | null = folderId;
  let guard = 0;
  while (currentId !== null && guard < 64) {
    const folder = byId.get(currentId);
    if (!folder) break;
    chain.unshift(folder);
    currentId = folder.parent_id || null;
    guard += 1;
  }
  return chain;
}

function FolderBreadcrumb({
  folders,
  currentId,
  onNavigate,
}: {
  folders: FolderItem[];
  currentId: number | null;
  onNavigate: (id: number | null) => void;
}) {
  const { tr } = useI18n();
  if (folders.length === 0 && currentId === null) return null;
  const chain = folderChain(folders, currentId);
  return (
    <nav className="folder-breadcrumb" aria-label={tr("文件夹路径", "Folder path")}>
      <button type="button" className={currentId === null ? "active" : ""} onClick={() => onNavigate(null)}>
        <Home size={12} />{tr("全部文件", "All files")}
      </button>
      {chain.map((folder) => (
        <Fragment key={folder.id}>
          <ChevronRight size={12} className="folder-breadcrumb-sep" aria-hidden="true" />
          <button
            type="button"
            className={currentId === folder.id ? "active" : ""}
            onClick={() => onNavigate(folder.id)}
            title={folder.name}
          >
            {folder.name}
          </button>
        </Fragment>
      ))}
    </nav>
  );
}

function FolderShelf({
  folders,
  activeFolderId,
  admin,
  onSelect,
  onChanged,
  viewMode,
}: {
  folders: FolderItem[];
  activeFolderId: number | null;
  admin: boolean;
  onSelect: (id: number | null) => void;
  onChanged: () => void;
  viewMode: "grid" | "list";
}) {
  const { tr } = useI18n();
  const parentId = activeFolderId ?? 0;
  const children = useMemo(
    () => folders.filter((folder) => folder.parent_id === parentId),
    [folders, parentId],
  );
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [nameDraft, setNameDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (!admin && children.length === 0) return null;

  async function createFolder() {
    const name = nameDraft.trim();
    if (!name || busy) return;
    setBusy(true);
    setError("");
    try {
      await api("/api/admin/folders", { method: "POST", body: JSON.stringify({ name, parent_id: parentId }) });
      setNameDraft("");
      setCreating(false);
      onChanged();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function renameFolder(id: number) {
    const name = nameDraft.trim();
    if (!name || busy) return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/admin/folders/${id}`, { method: "PATCH", body: JSON.stringify({ name }) });
      setNameDraft("");
      setEditingId(null);
      onChanged();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function deleteFolder(id: number) {
    const folder = folders.find((item) => item.id === id);
    const confirmed = window.confirm(tr(
      `确认删除文件夹「${folder?.name || id}」吗？其中的子文件夹也会一并删除，文件本身保留在媒体库中。`,
      `Delete the folder "${folder?.name || id}"? Subfolders are also removed; the files themselves stay in the library.`,
    ));
    if (!confirmed || busy) return;
    setBusy(true);
    setError("");
    try {
      await api(`/api/admin/folders/${id}`, { method: "DELETE" });
      if (activeFolderId === id) onSelect(null);
      onChanged();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  const headVisible = admin || children.length > 0;
  return (
    <section className="folder-shelf" aria-label={tr("文件夹", "Folders")}>
      {headVisible && (
        <div className="folder-shelf-head">
          <span className="folder-shelf-title"><Folder size={15} />{tr("文件夹", "Folders")}{children.length > 0 && <small>{children.length}</small>}</span>
          {admin && (
            <button
              className="button secondary folder-shelf-add"
              disabled={busy}
              onClick={() => { setCreating((current) => !current); setEditingId(null); setNameDraft(""); }}
              type="button"
            >
              <FolderPlus size={15} />{tr("新建文件夹", "New folder")}
            </button>
          )}
        </div>
      )}
      {error && <p className="form-error folder-error" role="alert">{error}</p>}
      {admin && creating && (
        <form className="folder-inline-form folder-shelf-form" onSubmit={(event) => { event.preventDefault(); void createFolder(); }}>
          <input value={nameDraft} maxLength={120} placeholder={tr("文件夹名称", "Folder name")} onChange={(event) => setNameDraft(event.target.value)} autoFocus aria-label={tr("文件夹名称", "Folder name")} />
          <button className="button secondary" disabled={busy} type="submit" aria-label={tr("创建", "Create")}><Check size={13} /></button>
          <button className="button ghost" onClick={() => setCreating(false)} type="button" aria-label={tr("取消", "Cancel")}><X size={13} /></button>
        </form>
      )}
      {children.length === 0 ? (
        admin ? <p className="muted folder-shelf-empty">{tr("还没有文件夹，点击上方新建。", "No folders here yet. Use the new-folder button.")}</p> : null
      ) : viewMode === "list" ? (
        <div className="folder-list">
          {children.map((folder) => (
            <div className="folder-list-row" key={folder.id}>
              {editingId === folder.id ? (
                <form className="folder-inline-form" onSubmit={(event) => { event.preventDefault(); void renameFolder(folder.id); }}>
                  <input value={nameDraft} maxLength={120} onChange={(event) => setNameDraft(event.target.value)} autoFocus aria-label={tr("文件夹名称", "Folder name")} />
                  <button className="button secondary" disabled={busy} type="submit" aria-label={tr("保存", "Save")}><Check size={13} /></button>
                  <button className="button ghost" onClick={() => setEditingId(null)} type="button" aria-label={tr("取消", "Cancel")}><X size={13} /></button>
                </form>
              ) : (
                <>
                  <button className="folder-list-open" onClick={() => onSelect(folder.id)} type="button" title={folder.name}>
                    <Folder size={18} />
                    <span className="folder-list-name"><strong title={folder.name}>{folder.name}</strong><small>{folder.item_count} {tr("项", "items")}</small></span>
                  </button>
                  {admin && (
                    <span className="folder-card-actions">
                      <button title={tr("重命名", "Rename")} aria-label={tr("重命名", "Rename")} onClick={() => { setEditingId(folder.id); setCreating(false); setNameDraft(folder.name); }} type="button"><Pencil size={14} /></button>
                      <button className="folder-delete" title={tr("删除文件夹", "Delete folder")} aria-label={tr("删除文件夹", "Delete folder")} onClick={() => void deleteFolder(folder.id)} type="button"><Trash2 size={14} /></button>
                    </span>
                  )}
                </>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="folder-grid">
          {children.map((folder) => (
            <div className="folder-card" key={folder.id}>
              {editingId === folder.id ? (
                <form className="folder-inline-form folder-card-form" onSubmit={(event) => { event.preventDefault(); void renameFolder(folder.id); }}>
                  <input value={nameDraft} maxLength={120} onChange={(event) => setNameDraft(event.target.value)} autoFocus aria-label={tr("文件夹名称", "Folder name")} />
                  <button className="button secondary" disabled={busy} type="submit" aria-label={tr("保存", "Save")}><Check size={13} /></button>
                  <button className="button ghost" onClick={() => setEditingId(null)} type="button" aria-label={tr("取消", "Cancel")}><X size={13} /></button>
                </form>
              ) : (
                <>
                  <button className="folder-card-open" onClick={() => onSelect(folder.id)} type="button" title={folder.name}>
                    <Folder size={30} strokeWidth={1.7} />
                    <strong title={folder.name}>{folder.name}</strong>
                    <small>{folder.item_count} {tr("项", "items")}</small>
                  </button>
                  {admin && (
                    <span className="folder-card-actions">
                      <button title={tr("重命名", "Rename")} aria-label={tr("重命名", "Rename")} onClick={() => { setEditingId(folder.id); setCreating(false); setNameDraft(folder.name); }} type="button"><Pencil size={14} /></button>
                      <button className="folder-delete" title={tr("删除文件夹", "Delete folder")} aria-label={tr("删除文件夹", "Delete folder")} onClick={() => void deleteFolder(folder.id)} type="button"><Trash2 size={14} /></button>
                    </span>
                  )}
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function MoveToFolderPicker({ folders, disabled, onPick }: { folders: FolderItem[]; disabled: boolean; onPick: (id: number) => void }) {
  const { tr } = useI18n();
  const [open, setOpen] = useState(false);
  const byId = useMemo(() => new Map(folders.map((folder) => [folder.id, folder])), [folders]);
  const depthOf = (folder: FolderItem): number => {
    let depth = 0;
    let current = folder;
    while (current.parent_id && depth < 64) {
      const parent = byId.get(current.parent_id);
      if (!parent) break;
      current = parent;
      depth += 1;
    }
    return depth;
  };
  return (
    <div className="folder-picker">
      <button className="button secondary" disabled={disabled} onClick={() => setOpen((current) => !current)} type="button"><FolderPlus size={15} />{tr("移动到文件夹", "Move to folder")}</button>
      {open && (
        <div className="folder-picker-menu" role="menu" aria-label={tr("选择目标文件夹", "Choose a destination folder")}>
          <div className="folder-picker-head">
            <span>{tr("选择目标文件夹", "Choose a destination folder")}</span>
            <button onClick={() => setOpen(false)} type="button" aria-label={tr("关闭", "Close")}><X size={14} /></button>
          </div>
          {folders.length === 0 ? (
            <p className="muted">{tr("还没有文件夹，请先在侧边栏创建。", "No folders yet. Create one in the sidebar first.")}</p>
          ) : (
            folders.slice().sort((left, right) => left.id - right.id).map((folder) => (
              <button key={folder.id} style={{ paddingLeft: 10 + depthOf(folder) * 14 }} onClick={() => { setOpen(false); onPick(folder.id); }} type="button" role="menuitem">
                <Folder size={14} />{folder.name}<small>{folder.item_count}</small>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function MailboxBell() {
  const { tr } = useI18n();
  const [open, setOpen] = useState(false);
  const [unread, setUnread] = useState(0);
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const refreshUnread = useCallback(async () => {
    try {
      const result = await api<{ count: number }>("/api/notifications/unread-count");
      setUnread(result.count);
    } catch {
      // The mailbox is only available after signing in; keep the last count.
    }
  }, []);

  useEffect(() => {
    void refreshUnread();
    const timer = window.setInterval(() => void refreshUnread(), 25_000);
    return () => window.clearInterval(timer);
  }, [refreshUnread]);

  const load = useCallback(async (cursor: number | null) => {
    setLoading(true);
    setError("");
    try {
      const page = await api<NotificationPage>(`/api/notifications?limit=30${cursor ? `&cursor=${cursor}` : ""}`);
      setItems((current) => cursor ? [...current, ...page.items] : page.items);
      setNextCursor(page.next_cursor);
      setHasMore(page.has_more);
      if (!cursor && page.unread > 0) {
        await api("/api/notifications/read", { method: "POST", body: JSON.stringify({ all: true }) });
        setUnread(0);
        setItems((current) => current.map((item) => ({ ...item, is_read: true })));
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) void load(null);
  }, [open, load]);

  async function remove(id: number) {
    try {
      await api("/api/notifications", { method: "DELETE", body: JSON.stringify({ ids: [id] }) });
      setItems((current) => current.filter((item) => item.id !== id));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }

  return (
    <div className="mailbox">
      <button
        className={`icon-button mailbox-trigger${unread > 0 ? " has-unread" : ""}`}
        onClick={() => setOpen((current) => !current)}
        type="button"
        aria-label={tr("信箱", "Mailbox")}
        title={tr("信箱", "Mailbox")}
      >
        <Bell size={20} />
        {unread > 0 && <span className="mailbox-badge">{unread > 99 ? "99+" : unread}</span>}
      </button>
      {open && (
        <div className="mailbox-panel" role="dialog" aria-label={tr("信箱", "Mailbox")}>
          <div className="mailbox-head">
            <strong>{tr("信箱", "Mailbox")}</strong>
            <span className="mailbox-unread-hint">{unread > 0 ? `${unread} ${tr("条未读", "unread")}` : tr("全部已读", "All read")}</span>
            <button className="mailbox-close icon-button" onClick={() => setOpen(false)} type="button" aria-label={tr("关闭", "Close")}><X size={16} /></button>
          </div>
          {error && <p className="form-error" role="alert">{error}</p>}
          {loading && !items.length && <p className="muted mailbox-loading"><LoaderCircle className="spin" size={18} />{tr("正在读取通知", "Loading notifications")}</p>}
          {!loading && !error && items.length === 0 && <p className="muted mailbox-empty">{tr("没有通知", "No notifications")}</p>}
          <div className="mailbox-list">
            {items.map((item) => (
              <div className={`mailbox-item${item.is_read ? "" : " unread"}`} key={item.id}>
                <div className="mailbox-item-head">
                  <strong>{notificationKindLabel(item.kind, tr)} · {item.title}</strong>
                  <span className="mailbox-item-actions">
                    <time dateTime={item.created_at}>{formatMailboxTime(item.created_at)}</time>
                    <button onClick={() => void remove(item.id)} title={tr("删除", "Delete")} aria-label={tr("删除通知", "Delete notification")} type="button"><X size={13} /></button>
                  </span>
                </div>
                <p>{item.body}</p>
              </div>
            ))}
          </div>
          {hasMore && nextCursor && (
            <button className="button secondary wide" onClick={() => void load(nextCursor)} type="button">{tr("加载更多", "Load more")}</button>
          )}
        </div>
      )}
    </div>
  );
}

function notificationKindLabel(kind: string, tr: (zh: string, en: string) => string): string {
  if (kind === "review") return tr("审核", "Review");
  if (kind === "media") return tr("资源管理", "Media");
  if (kind === "system") return tr("系统通知", "System");
  return tr("通知", "Notice");
}

function formatMailboxTime(value: string): string {
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(translateNow("zh-CN", "en-US"), {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  } catch {
    return value;
  }
}

export function visibilityLabel(visibility: MediaVisibility, tr: (zh: string, en: string) => string): string {
  if (visibility === "public") return tr("公开", "Public");
  if (visibility === "hidden") return tr("隐藏", "Hidden");
  return tr("私有", "Private");
}

const MediaListView = memo(function MediaListView({
  items,
  isAdmin,
  selectedKeys,
  onToggleSelect,
  onOpen,
  onDelete,
  onVisibility,
  busy,
}: {
  items: MediaItem[];
  isAdmin: boolean;
  selectedKeys: Set<string>;
  onToggleSelect: (item: MediaItem) => void;
  onOpen: (item: MediaItem) => void;
  onDelete: (item: MediaItem) => void;
  onVisibility: (item: MediaItem, visibility: MediaVisibility) => void;
  busy: boolean;
}) {
  const { tr } = useI18n();
  return (
    <div className="media-list-view" role="table" aria-label={tr("媒体列表", "Media list")}>
      <div className={`media-list-head${isAdmin ? "" : " no-check"}`} role="row">
        {isAdmin && <span className="media-list-check" />}
        <span>{tr("标题", "Title")}</span>
        <span>{tr("类型", "Kind")}</span>
        <span>{tr("大小", "Size")}</span>
        <span>{tr("日期", "Date")}</span>
        <span>{tr("可见性", "Visibility")}</span>
        {isAdmin && <span className="media-list-actions-head">{tr("操作", "Actions")}</span>}
      </div>
      {items.map((item) => (
        <MediaListRow
          item={item}
          key={`${item.account_id}:${item.id}`}
          isAdmin={isAdmin}
          selected={selectedKeys.has(`${item.account_id}:${item.id}`)}
          onToggleSelect={onToggleSelect}
          onOpen={onOpen}
          onDelete={onDelete}
          onVisibility={onVisibility}
          busy={busy}
        />
      ))}
    </div>
  );
});

const MediaListRow = memo(function MediaListRow({
  item,
  isAdmin,
  selected,
  onToggleSelect,
  onOpen,
  onDelete,
  onVisibility,
  busy,
}: {
  item: MediaItem;
  isAdmin: boolean;
  selected: boolean;
  onToggleSelect: (item: MediaItem) => void;
  onOpen: (item: MediaItem) => void;
  onDelete: (item: MediaItem) => void;
  onVisibility: (item: MediaItem, visibility: MediaVisibility) => void;
  busy: boolean;
}) {
  const { tr } = useI18n();
  return (
    <div className={`media-list-row${selected ? " selected" : ""}${isAdmin ? "" : " no-check"}`} role="row">
      {isAdmin && (
        <label className="media-select-checkbox media-list-check" title={tr("选择媒体", "Select media")} onClick={(event) => event.stopPropagation()}>
          <input type="checkbox" checked={selected} onChange={() => onToggleSelect(item)} aria-label={`${tr("选择", "Select")} ${item.title}`} />
        </label>
      )}
      <button className="media-list-title" onClick={() => onOpen(item)} type="button" title={tr("打开", "Open")}>
        <MediaListThumb item={item} />
        <span className="media-list-title-copy">
          <strong title={item.title}>{item.title}</strong>
          <small>{item.filename}</small>
        </span>
      </button>
      <span className="media-list-kind"><FileKindIcon kind={item.kind} mime={item.mime_type} /><small>{item.kind}</small></span>
      <span className="media-list-size">{formatBytes(item.size)}</span>
      <span className="media-list-date">{formatDate(item.date)}</span>
      <span className="media-list-visibility"><span className={`visibility-pill ${item.visibility}`}>{visibilityLabel(item.visibility, tr)}</span></span>
      {isAdmin && (
        <span className="media-list-actions" onClick={(event) => event.stopPropagation()}>
          <select value={item.visibility} disabled={busy} onChange={(event) => onVisibility(item, event.target.value as MediaVisibility)} aria-label={`${tr("可见性", "Visibility")} ${item.title}`}>
            <option value="public">{tr("公开", "Public")}</option>
            <option value="private">{tr("私有", "Private")}</option>
            <option value="hidden">{tr("隐藏", "Hidden")}</option>
          </select>
          <button className="icon-button list-delete-button" disabled={busy} onClick={() => onDelete(item)} title={tr("删除", "Delete")} aria-label={`${tr("删除", "Delete")} ${item.title}`} type="button"><Trash2 size={16} /></button>
        </span>
      )}
    </div>
  );
});

const MediaListThumb = memo(function MediaListThumb({ item }: { item: MediaItem }) {
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  const handleThumbnailError = useCallback(() => setThumbnailFailed(true), []);
  return (
    <span className={`media-list-thumb kind-${item.kind}`}>
      {item.thumbnail_url && !thumbnailFailed ? (
        <ThumbnailImage key={item.thumbnail_url} src={item.thumbnail_url} alt="" onError={handleThumbnailError} />
      ) : (
        <FileKindIcon kind={item.kind} mime={item.mime_type} />
      )}
    </span>
  );
});

function TimelineRail({
  months,
  position,
  onPositionChange,
  onClear,
}: {
  months: TimelineMonth[];
  position: number;
  onPositionChange: (position: number) => void;
  onClear: () => void;
}) {
  const { tr } = useI18n();
  const railRef = useRef<HTMLDivElement | null>(null);
  const wheelRef = useRef<HTMLDivElement | null>(null);
  // The wheel handler reads the latest values through a ref so the listener
  // is attached exactly once and never re-attaches (loop-proof pattern).
  const stateRef = useRef({ months, position, onPositionChange });
  stateRef.current = { months, position, onPositionChange };

  // The rail only renders once months are available, so key the listener on
  // that visibility boolean instead of mounting once with an empty node.
  const railVisible = months.length > 0;
  useEffect(() => {
    if (!railVisible) return;
    const node = railRef.current;
    if (!node) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const { months: list, position: current, onPositionChange: change } = stateRef.current;
      if (!list.length) return;
      const step = event.deltaY > 0 ? 1 : -1;
      const next = Math.min(list.length - 1, Math.max(0, current + step));
      if (next !== current) change(next);
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [railVisible]);

  useEffect(() => {
    const active = wheelRef.current?.querySelector(".timeline-wheel button.active") as HTMLElement | null;
    if (active && typeof active.scrollIntoView === "function") {
      try {
        active.scrollIntoView({ block: "nearest" });
      } catch {
        // Environments without layout (tests) or exotic browsers.
      }
    }
  }, [position]);

  if (!months.length) return null;

  return (
    <div className="timeline-rail" ref={railRef} aria-label={tr("时间线", "Timeline")}>
      <div className="timeline-rail-top" aria-hidden="true">
        <CalendarDays size={15} />
      </div>
      <div className="timeline-wheel" ref={wheelRef} role="listbox" aria-label={tr("时间线月份", "Timeline months")}>
        {months.map((month, index) => (
          <button
            key={month.month}
            type="button"
            role="option"
            aria-selected={index === position}
            className={index === position ? "active" : ""}
            onClick={() => onPositionChange(index)}
            aria-label={timelineMonthLabel(month, tr)}
          >
            <span className="timeline-node-label">{month.month.slice(5)}</span>
            <span className="timeline-node-mark" />
            <span className="timeline-tip" role="tooltip">{timelineMonthLabel(month, tr)}</span>
          </button>
        ))}
      </div>
      <button className="timeline-rail-clear" type="button" onClick={onClear} title={tr("全部", "All")}>
        {tr("全部", "All")}
      </button>
    </div>
  );
}

function timelineMonthLabel(month: TimelineMonth, tr: (zh: string, en: string) => string): string {
  const [year, monthNumber] = month.month.split("-").map(Number);
  const label = tr(`${year}年${monthNumber}月`, `${year}-${String(monthNumber).padStart(2, "0")}`);
  return `${label} · ${month.count} ${tr("项", "items")}`;
}

function MediaCard({
  item,
  onOpen,
  isAdmin = false,
  selectable = false,
  selected = false,
  onToggleSelect,
}: {
  item: MediaItem;
  onOpen: () => void;
  isAdmin?: boolean;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: () => void;
}) {
  const { tr } = useI18n();
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  const handleThumbnailError = useCallback(() => setThumbnailFailed(true), []);
  return (
    <div className={`media-card-wrap${selected ? " selected" : ""}`}>
      {selectable && (
        <label className="media-select-checkbox" title={tr("选择媒体", "Select media")} onClick={(event) => event.stopPropagation()}>
          <input type="checkbox" checked={selected} onChange={onToggleSelect} aria-label={`${tr("选择", "Select")} ${item.title}`} />
        </label>
      )}
      <button className="media-card" onClick={onOpen} type="button" aria-label={`${tr("打开", "Open")} ${item.title}`}>
        <div className={`media-poster kind-${item.kind}`}>
          {item.thumbnail_url && !thumbnailFailed ? (
            <ThumbnailImage
              key={item.thumbnail_url}
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
          <p><time dateTime={item.date}>{formatDate(item.date)}</time><span> · </span>{formatBytes(item.size)}{isAdmin && <><span> · </span><strong className={`visibility-label ${item.visibility}`}>{visibilityLabel(item.visibility, tr)}</strong></>}</p>
        </div>
      </button>
    </div>
  );
}

function MediaViewer({ item, onClose }: { item: MediaItem; onClose: () => void }) {
  const { tr } = useI18n();
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
          <button className="icon-button" onClick={onClose} aria-label={tr("关闭播放器", "Close viewer")} title={tr("关闭", "Close")} type="button">
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
  const { tr } = useI18n();
  const containerRef = useRef<HTMLSpanElement | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [objectUrl, setObjectUrl] = useState("");
  // attempt is the ONLY dependency of the fetch effect. It starts at 0 and
  // becomes 1 once the thumbnail scrolls into view. Everything else (src,
  // fingerprint, fetchAndDecrypt, onError) is mirrored into refs, so unstable
  // identities from parents or the crypto context can never re-trigger the
  // effect and cause an infinite fetch/render loop (the list-view bug).
  const [attempt, setAttempt] = useState(0);
  const failedRef = useRef(false);
  const fetchCountRef = useRef(0);
  const srcRef = useRef(src);
  srcRef.current = src;
  const fingerprintRef = useRef(mediaCrypto.fingerprint);
  fingerprintRef.current = mediaCrypto.fingerprint;
  const fetchAndDecryptRef = useRef(mediaCrypto.fetchAndDecrypt);
  fetchAndDecryptRef.current = mediaCrypto.fetchAndDecrypt;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const start = useCallback(() => {
    // Idempotent: repeated observer callbacks (or StrictMode double effects)
    // can only ever bump 0 -> 1.
    setAttempt((current) => (current === 0 ? 1 : current));
  }, []);

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") {
      start();
      return;
    }
    const node = containerRef.current;
    if (!node) {
      start();
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer.disconnect();
        start();
      }
    }, { rootMargin: "600px 0px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [start]);

  useEffect(() => {
    if (attempt === 0 || failedRef.current) return;
    let cancelled = false;
    let createdUrl = "";
    const encryptedUrl = encryptedThumbnailUrl(srcRef.current, fingerprintRef.current);
    fetchCountRef.current += 1;
    if (fetchCountRef.current >= 3) {
      // Regression tripwire: with the ref-based design this can only happen
      // if the load effect is accidentally re-wired to unstable deps.
      console.warn(
        "[savedstream] thumbnail " + encryptedUrl + " fetched " + fetchCountRef.current + " times in one mount; possible render loop",
      );
    }
    setLoaded(false);
    setObjectUrl("");
    void fetchAndDecryptRef.current(encryptedUrl).then(({ data, headers }) => {
      if (cancelled) return;
      createdUrl = URL.createObjectURL(new Blob([data], { type: headers.get("X-SavedStream-Mime") || "image/jpeg" }));
      setObjectUrl(createdUrl);
    }).catch((reason) => {
      if (cancelled) return;
      if ((reason as DOMException)?.name === "AbortError") return;
      failedRef.current = true;
      onErrorRef.current();
    });
    return () => {
      cancelled = true;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [attempt]);
  return (
    <span className="thumbnail-container" ref={containerRef}>
      {!loaded && (
        <span className="thumbnail-loading" aria-label={tr("正在加载缩略图", "Loading thumbnail")}>
          <LoaderCircle className="spin" size={26} />
        </span>
      )}
      {objectUrl && (
        <img
          className={loaded ? "thumbnail-ready" : "thumbnail-pending"}
          src={objectUrl}
          alt={alt}
          onLoad={() => setLoaded(true)}
          onError={() => {
            failedRef.current = true;
            onErrorRef.current();
          }}
        />
      )}
    </span>
  );
}

export function encryptedThumbnailUrl(src: string, fingerprint: string): string {
  const encrypted = src.replace("/thumbnail", "/encrypted-thumbnail");
  if (!fingerprint) return encrypted;
  return `${encrypted}${encrypted.includes("?") ? "&" : "?"}device=${encodeURIComponent(fingerprint)}`;
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
  const { tr } = useI18n();
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
      <button className={iconOnly ? "icon-button" : "button primary"} disabled={busy} onClick={() => void download()} aria-label={tr("下载", "Download")} title={tr("下载", "Download")} type="button">
        {busy ? <LoaderCircle className="spin" size={iconOnly ? 20 : 18} /> : <Download size={iconOnly ? 20 : 18} />}
        {!iconOnly && tr("下载文件", "Download file")}
      </button>
      {progress && <DownloadProgressDialog item={item} progress={progress} canceling={canceling} onCancel={cancel} />}
      {error && <span className="form-error" role="alert">{error}</span>}
    </>
  );
}

function DownloadProgressDialog({ item, progress, canceling, onCancel }: { item: MediaItem; progress: DownloadProgress; canceling: boolean; onCancel: () => void }) {
  const { tr } = useI18n();
  const status = canceling
    ? tr("正在取消下载", "Canceling download")
    : progress.phase === "preparing"
      ? tr("正在准备保存位置", "Preparing save location")
      : progress.phase === "finalizing"
        ? tr("正在完成文件写入", "Finishing file write")
        : progress.waiting
          ? `${tr("正在获取分块", "Fetching chunk")} ${progress.chunkIndex} / ${progress.chunkCount}`
          : `${tr("已解密分块", "Decrypted chunk")} ${progress.chunkIndex} / ${progress.chunkCount}`;
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
          <span>{progress.phase === "finalizing" ? tr("下载完成", "Download complete") : progress.speed > 0 ? `${formatBytes(Math.round(progress.speed))}/s` : tr("正在计算速度", "Calculating speed")}</span>
          <span>{progress.phase === "finalizing" ? tr("正在写入磁盘", "Writing to disk") : formatRemainingTime(progress.etaSeconds)}</span>
        </div>
        <button className="button secondary wide" disabled={canceling || progress.phase === "finalizing"} onClick={onCancel} type="button">
          {canceling ? <LoaderCircle className="spin" size={18} /> : <X size={18} />}{canceling ? tr("正在取消", "Canceling") : tr("取消下载", "Cancel download")}
        </button>
      </section>
    </div>
  );
}

export function formatRemainingTime(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return translateNow("剩余时间计算中", "Calculating time remaining");
  if (seconds < 60) return translateNow(`约剩余 ${Math.max(1, Math.ceil(seconds))} 秒`, `About ${Math.max(1, Math.ceil(seconds))} seconds remaining`);
  if (seconds < 3600) return translateNow(`约剩余 ${Math.ceil(seconds / 60)} 分钟`, `About ${Math.ceil(seconds / 60)} minutes remaining`);
  return translateNow(
    `约剩余 ${Math.floor(seconds / 3600)} 小时 ${Math.ceil((seconds % 3600) / 60)} 分钟`,
    `About ${Math.floor(seconds / 3600)} hours ${Math.ceil((seconds % 3600) / 60)} minutes remaining`,
  );
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
  const { tr } = useI18n();
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
      {!source && !error && <LoaderCircle className="spin" size={34} aria-label={tr("正在解密图片", "Decrypting image")} />}
      {error ? (
        <div className="viewer-media-error"><ImageIcon size={42} /><span>{tr("图片解密失败：", "Image decryption failed: ")}{error}</span></div>
      ) : source ? <img src={source} alt={item.title} /> : null}
    </div>
  );
}

function ViewerVideo({ item }: { item: MediaItem }) {
  const mediaCrypto = useMediaCrypto();
  const { tr } = useI18n();
  const [source, setSource] = useState("");
  const [error, setError] = useState("");
  const [progress, setProgress] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let mediaSource: MediaSource | undefined;
    let objectUrl = "";
    async function stream() {
      if (!MediaSource.isTypeSupported(item.mime_type)) {
        throw new Error(tr("当前浏览器不支持该视频格式的加密流播放", "This browser cannot play this encrypted video format"));
      }
      mediaSource = new MediaSource();
      objectUrl = URL.createObjectURL(mediaSource);
      if (!cancelled) setSource(objectUrl);
      await new Promise<void>((resolve, reject) => {
        mediaSource!.addEventListener("sourceopen", () => resolve(), { once: true });
        mediaSource!.addEventListener("error", () => reject(new Error(tr("视频解密流初始化失败", "Failed to initialize the encrypted video stream"))), { once: true });
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
  }, [item, mediaCrypto, tr]);
  return (
    <div className="viewer-media-loader">
      {!source && !error && <LoaderCircle className="spin" size={34} aria-label={`${tr("正在解密视频", "Decrypting video")} ${progress.toFixed(0)}%`} />}
      {source && progress === 0 && !error && <span className="thumbnail-loading"><LoaderCircle className="spin" size={34} aria-label={tr("正在解密第一个视频分块", "Decrypting first video chunk")} /></span>}
      {error ? (
        <div className="viewer-media-error">
          <Film size={42} /><span>{tr("加密视频播放失败：", "Encrypted video playback failed: ")}{error}</span>
          <EncryptedDownloadButton item={item} />
        </div>
      ) : source ? <video controls autoPlay src={source} /> : null}
    </div>
  );
}

function appendBuffer(buffer: SourceBuffer, data: ArrayBuffer): Promise<void> {
  return new Promise((resolve, reject) => {
    const done = () => { buffer.removeEventListener("updateend", done); buffer.removeEventListener("error", failed); resolve(); };
    const failed = () => { buffer.removeEventListener("updateend", done); buffer.removeEventListener("error", failed); reject(new Error(translateNow("视频分块写入失败", "Failed to append a video chunk"))); };
    buffer.addEventListener("updateend", done, { once: true });
    buffer.addEventListener("error", failed, { once: true });
    buffer.appendBuffer(data);
  });
}
function MediaSkeleton() {
  const { tr } = useI18n();
  return (
    <div className="media-grid" aria-label={tr("正在加载", "Loading")}>
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

function isValidMediaDate(value: unknown): value is string {
  return typeof value === "string" && value.length >= 10 && !Number.isNaN(Date.parse(value));
}

function sameMediaItems(left: MediaItem[], right: MediaItem[]): boolean {
  if (left.length !== right.length) return false;
  for (let index = 0; index < left.length; index += 1) {
    const a = left[index];
    const b = right[index];
    if (a.account_id !== b.account_id || a.id !== b.id) return false;
    if (a.date !== b.date || a.title !== b.title || a.filename !== b.filename) return false;
    if (a.caption !== b.caption || a.local_title !== b.local_title) return false;
    if (a.visibility !== b.visibility || a.hidden !== b.hidden) return false;
    if (a.thumbnail_url !== b.thumbnail_url || a.stream_url !== b.stream_url) return false;
    if (a.size !== b.size || a.mime_type !== b.mime_type) return false;
    if (a.duration !== b.duration) return false;
    if (a.review_status !== b.review_status || a.deleted !== b.deleted) return false;
  }
  return true;
}

export function normalizeMediaItem(item: MediaItem): MediaItem {
  const legacy = item as MediaItem & { message_date?: unknown };
  const date = isValidMediaDate(item.date)
    ? item.date
    : isValidMediaDate(legacy.message_date)
      ? legacy.message_date
      : "";
  return { ...item, date };
}

export function mediaDay(value: unknown): string {
  return isValidMediaDate(value) ? value.slice(0, 10) : "unknown";
}

function formatDate(value: unknown): string {
  if (!isValidMediaDate(value)) return translateNow("日期未知", "Unknown date");
  return new Intl.DateTimeFormat(translateNow("zh-CN", "en-US"), { month: "short", day: "numeric", year: "numeric" }).format(new Date(value));
}

function formatDay(value: string): string {
  if (value === "unknown" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return translateNow("日期未知", "Unknown date");
  return new Intl.DateTimeFormat(translateNow("zh-CN", "en-US"), {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "short",
  }).format(new Date(`${value}T00:00:00Z`));
}

function formatDateTime(value: unknown): string {
  if (!isValidMediaDate(value)) return translateNow("日期未知", "Unknown date");
  return new Intl.DateTimeFormat(translateNow("zh-CN", "en-US"), {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}
