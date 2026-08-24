# SavedStream 功能迭代变更记录

> 本文档汇总最近一次功能迭代的全部变更：媒体库视图与管理员操作、文件夹、信箱通知、管理后台分页、主题切换，以及随后的修复与运维能力（缩略图流量优化、部署备份管理、存储感知告警）。
> 项目整体结构、架构与原有功能说明见 [PROJECT.md](PROJECT.md)。

## 1. 功能总览

| # | 功能 | 说明 | 主要位置 |
| --- | --- | --- | --- |
| 1 | 媒体库多视图 + 管理员操作 | 网格/列表视图切换；管理员可多选、批量设公开/私有/隐藏、删除 | `GalleryPage.tsx`、`main.py`、`database.py` |
| 2 | 多级文件夹 | 文件夹树、子文件夹、批量移动文件、按文件夹过滤 | `media_folders`/`media_folder_items` 表、`GalleryPage.tsx` |
| 3 | 信箱通知 | 审核/删除/可见性变更自动通知、管理员定向/广播发信、未读红点 | `notifications` 表、`MailboxBell`、`MailboxAdminPanel` |
| 4 | 管理后台分页化 | 11 个页签、右侧悬浮保存按钮 | `AdminPage.tsx` |
| 5 | 用户界面主题切换 | 主题选择器下放到普通用户顶栏 | `GalleryPage.tsx`、`ThemeSelector.tsx` |
| 6 | 列表视图无限刷新修复 | 缩略图回调引用不稳定导致的死循环 | `GalleryPage.tsx`、`AdminPage.tsx` |
| 7 | 缩略图流量优化 | 浏览器 immutable 缓存 + 前端内存 LRU + 视口懒加载 | `main.py`、`MediaCrypto.tsx` |
| 8 | 部署备份管理 | 管理台查看/删除部署备份、保留策略、deploy 自动轮转 | `backups.py`、`BackupAdminPanel`、`deploy.ps1` |
| 9 | 存储感知 | 磁盘快照、分级警报、优化建议、自动通知管理员 | `storage.py`、`StorageAdminPanel` |

## 2. 功能明细

### 2.1 媒体库视图与管理员操作

- 视图模式：**网格视图**（按日期分组）与**列表视图**（缩略图 + 标题/类型/大小/日期/可见性列），切换选择保存在 `localStorage`（`savedstream-view-mode`）。
- 管理员多选（网格卡片与列表行均可勾选），批量工具栏支持：
  - 设为公开 / 设为私有 / **设为隐藏**（新增第三可见性状态，仅管理员可见）；
  - 批量删除（同时删除 Telegram 消息、缓存与本地索引）；
  - 移动到文件夹。
- 列表视图每行提供独立的可见性下拉与删除按钮。
- 数据库：`media_index` 新增 `hidden` 列（自动迁移），隐藏行在列表、时间线、所有者查询与缩略图/流接口全部对非管理员不可见；审核通过或删除时自动清除隐藏标记。

### 2.2 多级文件夹

- 新表 `media_folders`（`parent_id` 树形、同级名称唯一）与 `media_folder_items`（一个文件可属于多个文件夹）。
- 前端侧边栏“文件夹”树：展开/收起、面包屑标题；管理员可新建根/子文件夹、重命名、删除（递归删除子树与关联项）。
- `GET /api/media?folder_id=N` 按文件夹过滤，可见性规则照常生效；非管理员只能看到自己可见媒体的计数。

### 2.3 信箱通知系统

- 新表 `notifications`（每用户一行，广播按用户展开写入）。
- 用户侧：分页列表、未读数（红点，25 秒轮询）、标记已读、删除；面板按分类（审核/资源管理/系统通知）展示。
- 管理侧：向指定用户或全部用户发送；发送记录（收件人/已读状态）。
- 自动钩子：审核通过/未通过（含理由）、资源被管理员删除、可见性被改为公开/私有/隐藏时，按 `submitter_telegram_user_id` 映射到注册用户并投递通知。

### 2.4 管理后台分页化与悬浮保存

- `AdminPage` 重构为 11 个页签：仪表盘 / Telegram 与多账号 / 审核队列 / 用户管理 / 公开相册 / 媒体库 / 上传 / 流量限额 / 本地缓存 / Bot 限流 / 站内信（页签选择持久化）。
- 各配置页保存按钮统一为**右侧悬浮动作条**（`.admin-sticky-actions`），滚动页面时保持可触及。
- 媒体库页签新增删除与隐藏操作、全部相册（scope=all）浏览。

### 2.5 用户界面主题切换

- `ThemeSelector`（跟随系统 / 午夜深色 / 石墨深色 / 纸张浅色 / 暖沙浅色）从管理后台扩展到普通用户顶栏，选择写入 `savedstream-theme` 全局生效。

### 2.6 修复：列表视图无限刷新

- 根因：列表缩略图组件向 `ThumbnailImage` 传内联 `onError`，引用每次渲染变化 → 拉取 effect 反复执行、无限重发请求（无报错）。
- 修复（第一轮）：`MediaListThumb`/`AdminMediaThumbnail` 使用 `useCallback`；`ThumbnailImage` 将 `onError` 存入 ref。
- 加固（第二轮，对应线上复现“切到列表视图后界面循环刷新、控制台无报错”）：把 `ThumbnailImage` 的拉取 effect 改为**只依赖一个幂等的 `attempt` 状态**，`src`/`fingerprint`/`fetchAndDecrypt`/`onError` 全部经 ref 读取——即使父组件或加密上下文产生不稳定的函数引用，也无法再触发 effect 循环；缩略图按 `thumbnail_url` 加 `key`，URL 变化时干净重挂载而非复用状态；`MediaListView`/`MediaListRow`/`MediaListThumb` 用 `memo` 收敛重渲染；10 秒静默轮询对未变化的数据保留原数组引用，避免列表持续“闪刷”；另加两处控制台诊断（单次挂载拉取 ≥3 次、同一缩略图 URL 连续 4 次未命中缓存）用于后续定位。新增 `GalleryPage.listview.test.tsx` 回归测试（含“上下文函数身份每次渲染都变化”的对抗用例），`npm test` 17 项全部通过，`npm run build` 通过（顺带修复 `AdminPage.tsx` 缺失的 `useCallback` 导入）。

### 2.7 缩略图流量优化

1. **浏览器 HTTP 缓存**：`/api/media/{id}/encrypted-thumbnail` 新增 `device=<fingerprint>` 缓存键参数（服务端校验与设备头一致），带参响应改为 `Cache-Control: private, max-age=604800, immutable`；设备密钥轮换后 URL 变化自动失效。
2. **前端内存 LRU**：`fetchAndDecrypt` 对缩略图请求做最多 400 条的内存缓存，会话内重复滚动/轮询/视图切换零重复请求。
3. **视口懒加载**：`IntersectionObserver`（rootMargin 600px）按需拉取，首屏只加载可见区域缩略图。

### 2.8 部署备份管理

- 部署备份位于宿主 `/opt/tube/backups`：每次部署生成 `code-<stamp>`（源码）与 `volumes-<stamp>`（4 个数据卷 tgz）。
- 容器挂载：`docker-compose.yml` 新增 `../backups:/backups:rw`；`deploy.ps1` 自动 `chown 10001:10001` + `chmod 700`。
- 管理 API：`GET /api/admin/backups`（列表）、`DELETE /api/admin/backups/{stamp}`（删除，stamp 白名单 + 路径防穿越）、`POST /api/admin/backups/cleanup`（保留策略，支持 dry-run 预览）。
- 管理页“备份管理”页签：备份列表/详情、单份删除、保留 N 份策略（预览 → 执行）。
- `deploy.ps1` 新增 `-KeepBackups`（默认 3）：部署健康检查通过后自动只保留最近 N 份。

### 2.10 视图内文件夹与纵向时间线滚轮（新一轮交互调整）

- **文件夹移出侧边栏，直接进入网格/列表视图**：当前目录的子文件夹以文件夹卡片（网格模式）或文件夹行（列表模式）展示在媒体区顶部；点击进入，侧边栏只保留媒体分类。
- **完整路径展示**：标题上方新增面包屑（全部文件 / 上级 / 当前文件夹），每一级都可点击跳回。
- **管理能力保留**：视图内提供“新建文件夹”（创建于当前目录）、悬停卡片/行上的重命名与删除（递归删除子树）。
- **时间线改为纵向滚轮**：不再使用横向滑块条；在侧边栏与主内容之间新增固定纵向时间轴（桌面端），鼠标滚轮上下滚动切换月份，点击节点直接跳转，每个节点悬浮气泡显示完整日期与数量；当前月份高亮并自动滚动到可见位置。
- **移动端不做时间线滚轮**：≤1023px 隐藏时间轴并还原主内容边距。
- 修复滚轮监听器在时间线异步加载后才渲染时未绑定的问题（监听绑定改为随可见性触发）；新增 3 个 jsdom 回归用例（视图内文件夹/面包屑、列表文件夹行、滚轮切换月份），`npm test` 20 项全部通过。

- `GET /api/admin/storage`：宿主磁盘（经 `/backups` 或 `/data` 探测）、数据卷、备份占用、媒体缓存、数据库大小的完整快照。
- 规则引擎（纯函数，可单测）：
  - 剩余 < 10%（或 < 10 GB）→ `LOW_SPACE`（warning）；< 5%（或 < 5 GB）→ `LOW_SPACE_CRITICAL`（critical）；
  - 备份 > 20 GB 或 > 6 份 → `BACKUPS_LARGE`；
  - 缓存 ≥ 上限 80% 且 > 10 GB → `CACHE_FULL`。
- 优化建议带可执行动作（清理备份 / 清空缓存），前端一键跳转或执行。
- 后台 `storage_watchdog`（延迟 60 秒，每 6 小时检查）：状态变化时通过信箱通知所有已批准管理员（严重不足 / 偏低 / 已恢复三档）。
- 管理页“存储”页签：四张指标卡、警报列表、建议列表，60 秒自动刷新。

## 3. 新增 / 变更 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/folders` | 文件夹树（含可见计数） |
| POST / PATCH / DELETE | `/api/admin/folders[/{id}]` | 新建 / 重命名·移动 / 删除（递归） |
| PUT / DELETE | `/api/admin/folders/{id}/items` | 批量放入 / 移出文件夹 |
| GET | `/api/media?folder_id=N` | 按文件夹过滤媒体（`scope` 新增 `hidden`） |
| PATCH | `/api/admin/media/{id}/visibility` | 可见性支持 `hidden` |
| POST | `/api/admin/media/visibility` | 批量可见性（含 hidden） |
| GET | `/api/notifications` | 我的通知（分页 + 未读数） |
| GET | `/api/notifications/unread-count` | 未读数（红点轮询） |
| POST | `/api/notifications/read` | 标记已读（ids 或全部） |
| DELETE | `/api/notifications` | 删除通知 |
| POST | `/api/admin/notifications` | 发送通知（指定用户或广播） |
| GET | `/api/admin/notifications` | 发送记录 |
| GET | `/api/admin/backups` | 备份列表（聚合 code/volumes） |
| DELETE | `/api/admin/backups/{stamp}` | 删除某次部署备份 |
| POST | `/api/admin/backups/cleanup` | 保留策略（`dry_run` 预览） |
| GET | `/api/admin/storage` | 存储快照 + 警报 + 建议 |

## 4. 数据库变更

| 变更 | 说明 |
| --- | --- |
| `media_index.hidden` | 新增列（`ALTER TABLE` 自动迁移），隐藏可见性 |
| `media_folders` | 多级文件夹（`parent_id` 树形） |
| `media_folder_items` | 文件夹 ↔ 媒体多对多 |
| `notifications` | 信箱通知（每用户一行） |
| `settings` | 新增 `storage_alert_state`（存储告警状态机） |

## 5. 部署与升级注意事项

1. **需要重新部署一次**（`.\deploy.ps1`）以生效：
   - `docker-compose.yml` 新增 `../backups:/backups:rw` 挂载；
   - 后端新增备份管理、存储感知模块；前端新增页签。
2. 旧安装的 `/opt/tube/backups` 若属主为 root，管理页会提示不可写；执行 `chown 10001:10001 /opt/tube/backups && chmod 700 /opt/tube/backups`（下一次部署自动处理）。
3. `deploy.ps1` 新参数 `-KeepBackups 3`：部署成功后自动只保留最近 3 份备份，缓解磁盘压力。
4. 缩略图缓存依赖前端与后端同步升级：旧前端不带 `device` 参数，自动保持 `no-store` 行为，不破坏兼容。
5. 存储告警使用信箱通知，管理员需要以账号（非仅 ADMIN_KEY cookie）登录管理后台才能看到红点与通知内容。
6. 升级后建议在管理后台依次检查：媒体库列表视图、文件夹、信箱、备份管理、存储页。

## 6. 测试

| 位置 | 新增测试 | 覆盖 |
| --- | --- | --- |
| `_src/backend/tests/test_library_features.py` | 隐藏可见性、文件夹层级与过滤、通知收发、隐藏/文件夹 API |
| `_src/backend/tests/test_backups.py` | 备份聚合、删除与非法 stamp、保留策略 dry-run/执行、管理 API |
| `_src/backend/tests/test_storage.py` | 存储规则引擎（健康/低空间/严重/备份大/缓存满）、快照形状 |
| `_src/frontend/src/GalleryPage.test.ts` | 可见性标签、`encryptedThumbnailUrl` 缓存键 |

## 7. 主要变更文件

| 文件 | 变更 |
| --- | --- |
| `_src/backend/app/database.py` | hidden 迁移、文件夹/通知表与方法、列表过滤 |
| `_src/backend/app/main.py` | 文件夹/通知/备份/存储路由、审核与可见性通知钩子、缩略图缓存头、storage watchdog |
| `_src/backend/app/backups.py` | 新增：备份列表/删除/保留策略 |
| `_src/backend/app/storage.py` | 新增：存储快照、规则引擎、告警 watchdog |
| `_src/backend/app/config.py` | `backups_dir` 配置 |
| `_src/backend/tests/*` | 三个新测试文件 |
| `_src/frontend/src/GalleryPage.tsx` | 视图切换、多选/批量操作、文件夹树、信箱、主题、缩略图优化 |
| `_src/frontend/src/AdminPage.tsx` | 页签化、备份/存储/信箱面板、媒体库管理 |
| `_src/frontend/src/MediaCrypto.tsx` | fingerprint 暴露、缩略图内存 LRU |
| `_src/frontend/src/types.ts` / `api.ts` / `styles.css` | 类型、错误码、样式 |
| `_src/docker-compose.yml` | `/backups` 挂载 |
| `deploy.ps1` | `-KeepBackups`、备份目录权限、自动轮转 |
| `PROJECT.md` | 16.1–16.9 节 |
