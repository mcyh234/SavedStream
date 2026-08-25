# SavedStream 功能迭代变更记录

> **快速迭代声明：当前程序处于快速迭代期，使用可能会出现各种体验不佳、bug 的情况，可以在 GitHub 提交 issue。**

> 本文档汇总最近一次功能迭代的全部变更：媒体库视图与管理员操作、文件夹、信箱通知、管理后台分页、主题切换，以及随后的修复与运维能力（缩略图流量优化、部署备份管理、存储感知告警）。
> 项目整体结构、架构与原有功能说明见 [PROJECT.md](PROJECT.md)。

## 服务端配置灾备备份

- 新增加密 `.ssbak` 归档、cron 定时上传 Telegram 收藏夹、失败重试和临时文件清理。
- 新增管理员本地上传、Telegram 历史扫描、下载恢复和事务回滚。
- 备份内容覆盖 SavedStream 数据库、媒体索引以及 TeleBox 账号/Bridge/Helper Bot 状态。

## 1. 功能总览

| # | 功能 | 说明 | 主要位置 |
| --- | --- | --- | --- |
| 1 | 媒体库多视图 + 管理员操作 | 网格/列表视图切换；管理员可多选、批量设公开/私有/隐藏、删除 | `GalleryPage.tsx`、`main.py`、`database.py` |
| 2 | 多级文件夹 | 文件夹树、子文件夹、批量移动文件、按文件夹过滤 | `media_folders`/`media_folder_items` 表、`GalleryPage.tsx` |
| 3 | 信箱通知 | 审核/删除/可见性变更自动通知、管理员定向/广播发信、未读红点 | `notifications` 表、`MailboxBell`、`MailboxAdminPanel` |
| 4 | 管理后台分页化 | 当前 14 个页签、右侧悬浮保存按钮 | `AdminPage.tsx` |
| 5 | 用户界面主题切换 | 主题选择器下放到普通用户顶栏 | `GalleryPage.tsx`、`ThemeSelector.tsx` |
| 6 | 列表视图无限刷新修复 | 缩略图回调引用不稳定导致的死循环 | `GalleryPage.tsx`、`AdminPage.tsx` |
| 7 | 缩略图流量优化 | 浏览器 immutable 缓存 + 前端内存 LRU + 视口懒加载 | `main.py`、`MediaCrypto.tsx` |
| 8 | 部署备份管理 | 管理台查看/删除部署备份、保留策略、deploy 自动轮转 | `backups.py`、`BackupAdminPanel`、`deploy.ps1` |
| 9 | 存储感知 | 磁盘快照、分级警报、优化建议、自动通知管理员 | `storage.py`、`StorageAdminPanel` |
| 10 | 新用户登录/注册入口修复 | 首屏改用账号登录与注册；移除对已返回 410 的旧 Telegram 登录码接口调用 | `App.tsx`、`AuthPanels.tsx`、`api.ts` |
| 11 | 新用户审批策略可选 | 管理员可开关人工审批；关闭后有效 `/bind` 用户自动获批，未绑定用户保持等待状态 | `main.py`、`AdminPage.tsx`、`bridge.ts` |
| 12 | WebUI 多文件拖拽上传 | 逐文件公开/私人、原始文件名、任务进度/取消/重试、Telegram 收藏夹直入库 | `GalleryPage.tsx`、`main.py`、`telebox_client.py` |
| 13 | 公开广场、点赞与举报 | 私人相册/广场/我的公开/我的点赞分离，幂等点赞和快速举报 | `database.py`、`main.py`、`GalleryPage.tsx` |
| 14 | 举报受理与组合处罚 | 下架/隐藏/删除、Mute/Ban/举报禁用、站内信、归属内容删除与失败重试 | `AdminPage.tsx`、`main.py`、`bridge.ts` |

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

- `AdminPage` 当前为 14 个页签：仪表盘 / Telegram 与多账号 / 审核队列 / 举报受理 / 用户管理 / 公开相册 / 媒体库 / 上传 / 流量限额 / 本地缓存 / Bot 限流 / 站内信 / 备份 / 存储（页签选择持久化）。
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

### 2.9 修复：首屏仍进入已弃用 Telegram 身份验证

- 根因：后端已将 `/api/access/telegram`、`/api/access/telegram/status` 与 `/api/public/login` 迁移为 `410 Gone`，但 `App.tsx` 仍渲染旧 `TelegramAccessGate` / `PublicKeyGate`。
- 首次进入主界面现在显示 SavedStream 用户名/密码登录；管理员开启公开注册时同时显示注册表单与注册密钥输入。
- 注册通过 `/api/auth/register/start` 创建挑战，用户点击 Helper Bot 深链完成 Telegram 绑定，前端轮询 `/api/auth/register/status` 后自动建立登录会话。
- 已批准账号在新浏览器登录时使用 `/api/auth/device/verify/status` 完成 Telegram 二次确认；待审核、拒绝、禁用、绑定同步等状态改为独立状态页。
- 所有 API 请求新增持久化 `X-SavedStream-Browser-ID`，避免不同浏览器被误判为同一可信设备。
- 普通用户在 Telegram 托管服务离线时不再被错误引导到管理员密钥验证。

### 2.10 新用户审批策略改为可选

- SQLite 设置新增 `registration_requires_approval`，默认值为 `1`，升级后保持原有“管理员人工审批”行为。
- 管理页“公开相册”新增“新用户需要管理员审批”开关，并补齐注册开关、注册密钥生成/轮换入口。
- 注册密钥生成/轮换支持两种来源：系统安全随机生成，或管理员输入自定义密钥；两种方式都会立即使旧密钥失效并暂时关闭新用户注册。
- 关闭审批后，完成 Telegram 身份确认且存在有效 `/bind` 托管账号绑定的 `pending` 用户自动更新为 `status=approved`、`binding_sync_status=ready`；没有有效绑定时仍保持等待状态，不会错误开放媒体库。
- 用户状态会在注册挑战确认、账号登录、`/api/status` 刷新及策略切换时重新同步；既有已批准用户不会因关闭开关被撤销。
- 用户管理面板改为直接管理 `auth_users`，新增 `PUT /api/admin/users/{user_id}`；旧 `/api/admin/access-users/{telegram_user_id}` 保留兼容。
- Helper Bot `/start <challenge>` 现会调用 SavedStream 内部挑战认领接口；`/web` 不再生成无法消费的一次性登录码。
- 注册时请求信任浏览器会随挑战保存设备哈希，自动审批后无需紧接着再次完成 Telegram 设备确认。

### 2.11 WebUI 多文件拖拽上传与共享限额

- 媒体库增加全页面文件拖拽监听和无障碍“选择文件”按钮；拖入时显示遮罩，松手后以列表展示文件，默认私人，可逐项或批量设为公开/私人。
- `POST /api/uploads` 直接接收原始文件流；文件名通过 Base64URL 请求头传递，服务端只移除路径与控制字符，不添加随机后缀，同名文件仍保持同名。
- 每个文件显示接收、Telegram 上传、索引、待公开审核、完成、失败、取消状态；批次允许部分成功，失败/取消项可单独重试。
- 普通用户固定上传到自己的绑定账号；管理员必须选择一个已连接账号。普通用户公开上传先以私人状态写入并进入审核，管理员公开上传直接公开。
- 上传暂存使用随机磁盘名、目录 `0700` / 文件 `0600`，严格核对 `Content-Length`，终态清理临时文件。
- WebUI 与 Helper Bot 共用 Bridge 的 `helper_rate_reservations` / `helper_rate_events`；预约会把并发中的文件数和字节数一并计入，避免并发超额，失败/取消释放，Telegram 写入成功后记账。管理员绕过个人额度，服务器月度流量仍遵循 `admin_bypass`。

### 2.12 私人相册、公开广场与点赞

- 普通用户侧栏拆分为“我的相册 / 广场 / 我的公开 / 我的点赞”；`GET /api/media` 与时间线新增 `view=private|square|my_public|liked`。
- 我的相册只显示本人最初选择为私人的资源；广场仅显示审核通过、公开且未隐藏资源；我的公开保留待审/公开/驳回/撤回状态；我的点赞自动过滤转私有、隐藏或删除的媒体。
- 新增 `media_likes` 表和幂等 `PUT/DELETE /api/media/{id}/like`；禁止上传者给自己的资源点赞，列表/卡片/播放器显示爱心状态与计数。
- 广场响应只返回展示和社交字段，不返回 Telegram 身份、所有者内部映射、审核理由/批次或上传来源；媒体传输继续使用现有设备密钥和加密分块。

### 2.13 举报受理、处罚与归属内容删除

- 广场新增举报弹窗：违法危险、色情露骨、版权侵权、恶意软件、垃圾滥用、隐私侵犯和其他，可填写补充说明；禁止自举报和重复未完结举报。
- 后台新增“举报受理”页签，按媒体聚合开放/失败举报，支持忽略、下架（转私人）、隐藏（仅管理员）、删除 Telegram 原文件并重试失败处置。
- 新增 `user_sanctions`：`upload_mute`、`login_ban`、`report_mute` 可组合、独立理由、预设/自定义时长、永久与提前解除；登录和受限操作返回结构化理由、解除时间与永久标记。
- `login_ban` 立即撤销会话/可信设备；上传处罚会取消仍在运行的 WebUI/Helper Bot 入库任务。普通管理员只能处罚普通用户，超级管理员受自罚与最后一个有效超级管理员保护。
- 新增 `content_deletion_jobs` / `content_deletion_job_items`，只选择能由 `owner_user_id`/Telegram 身份和 WebUI/Helper Bot 来源确认归属的内容；逐项删除 Telegram、缓存和索引，失败保留原因并可重试。
- 举报完结向举报者发送感谢站内信；下架/隐藏/删除及处罚向上传者发送站内信，不泄露举报者身份。

### 2.14 视图内文件夹与纵向时间线滚轮（新一轮交互调整）

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
| POST / GET / DELETE | `/api/uploads[/{job_id}]` | 普通用户/管理员原始流上传、所有者任务查询与取消 |
| GET | `/api/media?view=private\|square\|my_public\|liked` | 四种互相隔离的媒体集合 |
| PUT / DELETE | `/api/media/{id}/like` | 幂等点赞/取消点赞 |
| POST | `/api/media/{id}/reports` | 提交媒体举报 |
| GET / POST | `/api/admin/reports[/{report_id}/resolve]` | 举报聚合与受理 |
| GET / POST / DELETE | `/api/admin/users/{user_id}/sanctions[/{sanction_id}]` | 处罚历史、创建和提前解除 |
| POST / GET | `/api/admin/users/{user_id}/content-deletion`、`/api/admin/content-deletion-jobs/{id}` | 归属内容删除任务、进度与重试 |

## 4. 数据库变更

| 变更 | 说明 |
| --- | --- |
| `media_index.hidden` | 新增列（`ALTER TABLE` 自动迁移），隐藏可见性 |
| `media_folders` | 多级文件夹（`parent_id` 树形） |
| `media_folder_items` | 文件夹 ↔ 媒体多对多 |
| `notifications` | 信箱通知（每用户一行） |
| `settings` | 新增 `storage_alert_state`（存储告警状态机） |
| `media_likes` | 用户对公开媒体的幂等点赞 |
| `media_reports` | 举报理由、说明、媒体快照与受理状态 |
| `user_sanctions` | 可过期、可组合、可提前解除的细粒度处罚 |
| `content_deletion_jobs` / `content_deletion_job_items` | 归属内容异步删除进度与失败项 |
| `media_index` / `upload_jobs` | 新增所有者、Telegram 身份、上传来源、请求可见性和批次字段 |

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
| `_src/backend/tests/test_square_upload_moderation.py` | 四视图、点赞/举报、WebUI 上传隔离、处罚和归属删除 |
| `_src/frontend/src/GalleryPage.upload.test.tsx` | 拖拽遮罩、多文件默认/批量可见性、广场导航和举报入口 |
| `_src/frontend/src/AdminPage.sanctions.test.ts` | 处罚预设、永久和自定义解除时间校验 |
| `TeleBox/src/bridge-media.test.ts` | 原始文件名、长度核对、共享配额字节分配和处罚提示 |

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
