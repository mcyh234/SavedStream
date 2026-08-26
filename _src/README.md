# SavedStream

> 当前程序处于快速迭代期，使用可能会出现体验不佳或各种 Bug；欢迎提交 Issue。

## Telegram 多账号容灾

管理后台支持为账号设置逻辑账号组和备用容灾角色。复制队列、Telegram FloodWait、进度游标和故障切换状态均由后端持久化；备用账号保持隐藏于普通用户账号选择器之外。

## TeleBox 双容器模式

Compose 会启动 `savedstream` 和内部的 `telebox` Bridge。浏览器只访问 SavedStream；TeleBox 的 9000 端口不会发布到宿主机。

1. 在 `.env` 中设置 `ADMIN_KEY`、`TELEBOX_API_TOKEN` 和 `TELEBOX_SECRET_KEY`。
2. 保证 TeleBox 仓库位于 SavedStream 目录的同级 `../TeleBox`。
3. 执行 `docker compose up -d --build`。
4. 打开 `/admin`，在“多账号协调”中添加账号的 API 凭证和 TeleBox `StringSession`。
5. 填入 BotFather 创建的辅助 Bot token，随后为目标账号生成邀请码。
6. 提交者私聊辅助 Bot 发送 `/bind <邀请码>`，之后转发的媒体会进入绑定账号的 Saved Messages。

每个账号使用独立 session。辅助 Bot token 在 TeleBox 数据卷中使用 `TELEBOX_SECRET_KEY` 加密保存；更换该密钥后需要重新填写 token。

SavedStream 使用 Telegram MTProto 登录当前用户，直接读取 `Saved Messages`（收藏夹）中的媒体和文件，并通过网页按需代理播放。它不会为了播放而转发消息。网页提供按时间排序的媒体库、二维码重新登录、本地标题、访问限制和可调磁盘缓存。

WebUI 上传由服务端根据用户绑定和逻辑账号组的当前活动账号自动路由，不需要选择物理 Telegram 账号。在文件夹内上传会自动归入当前文件夹，归档媒体不重复显示在根级“全部文件”，但搜索会包含文件夹内容；在“广场”上传默认申请公开，普通用户仍必须经过管理员审核。

## 使用前准备

- 一台安装了 Docker Engine 24+ 和 Docker Compose v2 的 Linux 主机
- 从 [my.telegram.org/apps](https://my.telegram.org/apps) 获取的 `api_id` 和 `api_hash`
- 一个足够长且不可猜测的管理员密钥

Telegram 用户会话、SQLite 数据库、缩略图和媒体分块都保存在 Docker 的 `/data` 持久卷中。请把该卷视为敏感数据，其中的 Telegram session 等同于账号登录凭据。

## 快速启动

```bash
cp .env.example .env
nano .env
docker compose up -d --build
```

可以用 `openssl rand -hex 32` 生成 `ADMIN_KEY`。三个必填值为空时，网页只会显示服务器尚未配置，不会尝试连接 Telegram。

浏览器打开 `http://服务器地址:8000`。进入管理页并输入 `.env` 中的 `ADMIN_KEY`，然后生成二维码；在 Telegram 手机客户端中打开“设置 -> 设备 -> 连接桌面设备”完成扫码。如果账号启用了两步验证，网页会继续要求输入 Telegram 两步验证密码。

查看运行状态：

```bash
docker compose ps
docker compose logs -f savedstream
curl -f http://127.0.0.1:8000/healthz
```

## 配置

| 环境变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `TELEGRAM_API_ID` | 是 | `0` | Telegram 应用的数字 ID |
| `TELEGRAM_API_HASH` | 是 | 空 | Telegram 应用的 API hash |
| `ADMIN_KEY` | 是 | 空 | 管理页、二维码登录和设置接口的密钥 |
| `PORT` | 否 | `8000` | Docker 在宿主机上发布的端口 |
| `COOKIE_SECURE` | 否 | `false` | 通过 HTTPS 提供服务时设为 `true` |
| `SESSION_COOKIE_DAYS` | 否 | `30` | 管理员和访问者浏览器登录有效天数 |
| `DATA_DIR` | 否 | `/data` | 容器内数据目录；Compose 已固定为 `/data` |

`TELEGRAM_API_HASH`、`ADMIN_KEY` 和生成的 `.env` 不应提交到版本库或发给他人。

## 隐私与访问控制

首次启动后，媒体库默认没有访问口令。对外开放端口前，请在管理设置中配置至少 8 位的访问密钥，并开启访问限制。管理员登录也可以直接访问受限媒体库。

本地标题只保存在 `/data/savedstream.db`，不会修改或转发 Telegram 原消息。关闭访问限制会允许任何能访问站点的人浏览和请求媒体。生产部署建议放在 HTTPS 反向代理之后，同时设置 `COOKIE_SECURE=true`；代理必须保留 `Range` 请求头，视频拖动播放依赖 HTTP Range 响应。

常见的 Nginx 代理配置核心部分如下：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    client_max_body_size 10g;
    proxy_read_timeout 30m;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_request_buffering off;
}
```

## 缓存与持久化

管理页的缓存滑块支持 0.5 至 200 GiB。缓存以固定媒体分块和缩略图保存；命中缓存时不会再次从 Telegram 拉取。超过上限后，服务优先删除最久未访问的文件。清空缓存不会删除 Telegram session、本地标题或访问设置。

默认命名卷是 `savedstream_savedstream-data`（实际前缀取决于 Compose 项目名）。备份前可以停止服务并导出该卷：

```bash
docker compose stop savedstream
docker run --rm \
  -v savedstream_savedstream-data:/data:ro \
  -v "$PWD":/backup \
  alpine tar czf /backup/savedstream-data.tgz -C /data .
docker compose start savedstream
```

恢复或迁移卷时，应确保容器内文件归属 UID/GID `10001:10001`。如改用宿主机目录绑定挂载，先执行 `sudo chown -R 10001:10001 /你的数据目录`。

## 更新与重建

```bash
docker compose pull --ignore-buildable
docker compose up -d --build
```

重建镜像不会影响 `/data`。如 Telegram 会话过期，可在管理页退出或重置 Telegram session，然后重新扫码。频繁请求可能触发 Telegram `FloodWait`，此时应等待 Telegram 指定的时间，避免反复重启或重新登录。

## 服务端配置灾备备份

管理员后台的“备份”页签同时提供部署备份和服务端配置灾备备份。后者使用管理员设置的密码加密 `.ssbak` 文件，包含 SavedStream 数据库、媒体索引和 TeleBox 账号/Bridge 状态，成功上传到 Telegram 收藏夹后会删除服务端临时文件。备份支持标准五字段 cron 和 IANA 时区（默认 UTC）；管理员也可以扫描 Saved Messages 中的历史备份并事务化恢复。恢复不会带回浏览器会话和活动登录会话，环境变量类配置会提示重新配置或重启。

浏览器是否能直接播放文件取决于编码格式，而不仅是扩展名。当前服务不转码；浏览器不支持的媒体仍可下载，或在后续接入 HLS/转码流程。

## 本地测试

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
```

前端构建可单独验证：

```bash
cd frontend
npm install
npm run build
```
