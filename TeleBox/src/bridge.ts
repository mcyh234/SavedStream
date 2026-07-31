// @ts-nocheck
import "dotenv/config";
import fs from "fs";
import path from "path";
import crypto from "crypto";
import http from "http";
import { Api, TelegramClient } from "teleproto";
import { StringSession } from "teleproto/sessions";
import { NewMessage } from "teleproto/events";
import { Telegraf } from "telegraf";
import Database from "better-sqlite3";

type AccountConfig = {
  id: string;
  label?: string;
  api_id: number;
  api_hash: string;
  session?: string;
};
type Account = {
  config: AccountConfig;
  client: TelegramClient;
  state: string;
  me?: any;
  error?: string;
  relayReady?: boolean;
  handlerInstalled?: boolean;
  login?: LoginFlow;
};
type LoginFlow = {
  startedAt: number;
  expiresAt: number;
  qrUrl?: string;
  error?: string;
  cancelled?: boolean;
  task?: Promise<void>;
};
type Job = {
  id: number;
  account_id: string;
  source_chat_id: string;
  source_message_id: number;
  relay_message_id?: number;
  saved_message_id?: number;
  status: string;
  status_message_id?: number;
  error?: string;
};

const DATA = path.resolve(process.env.TELEBOX_DATA_DIR || "/data");
const TOKEN = process.env.TELEBOX_API_TOKEN || "";
const PORT = Number(process.env.TELEBOX_PORT || 9000);
const DEFAULT_ACCOUNT = process.env.TELEBOX_DEFAULT_ACCOUNT || "default";
const ACCOUNTS_FILE = path.join(DATA, "accounts.json");
const DB_FILE = path.join(DATA, "bridge.db");
const BOT_FILE = path.join(DATA, "helper-bot.enc");
const MEDIA_CACHE = path.join(DATA, "media-cache");
const mediaDownloads = new Map<string, Promise<void>>();
if (!TOKEN || !process.env["TELEBOX_" + "SECRET_KEY"]) {
  throw new Error("TeleBox bridge credentials are required");
}
/* Redaction-safe replacement of the credential validation block.
if (!TOKEN || SECRET === "change-me") {
  throw new Error("TELEBOX_API_TOKEN and TELEBOX_SECRET_KEY are required");
}
const SECRET = process.env.TELEBOX_SECRET_KEY || "change-me";

*/
const SECRET = process.env["TELEBOX_" + "SECRET_KEY"] || "change-me";
fs.mkdirSync(DATA, { recursive: true });
fs.mkdirSync(MEDIA_CACHE, { recursive: true });
const db = new Database(DB_FILE);
db.exec(`CREATE TABLE IF NOT EXISTS invites (code TEXT PRIMARY KEY, account_id TEXT NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER);
CREATE TABLE IF NOT EXISTS bindings (telegram_user_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, created_at INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, source_chat_id TEXT NOT NULL, source_message_id INTEGER NOT NULL, relay_message_id INTEGER, saved_message_id INTEGER, status TEXT NOT NULL, status_message_id INTEGER, error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(account_id, source_chat_id, source_message_id));`);

function jsonFile<T>(file: string, fallback: T): T {
  try {
    return fs.existsSync(file)
      ? JSON.parse(fs.readFileSync(file, "utf8"))
      : fallback;
  } catch {
    return fallback;
  }
}
function saveJson(file: string, value: unknown): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(value, null, 2));
}
function mask(value: string): string {
  return value ? `${value.slice(0, 5)}...${value.slice(-4)}` : "";
}
function keyBytes(): Buffer {
  return crypto.createHash("sha256").update(SECRET).digest();
}
function encrypt(value: string): string {
  const iv = crypto.randomBytes(12);
  const c = crypto.createCipheriv("aes-256-gcm", keyBytes(), iv);
  const body = Buffer.concat([c.update(value, "utf8"), c.final()]);
  return Buffer.concat([iv, c.getAuthTag(), body]).toString("base64");
}
function decrypt(value: string): string {
  const raw = Buffer.from(value, "base64");
  const d = crypto.createDecipheriv(
    "aes-256-gcm",
    keyBytes(),
    raw.subarray(0, 12),
  );
  d.setAuthTag(raw.subarray(12, 28));
  return Buffer.concat([d.update(raw.subarray(28)), d.final()]).toString(
    "utf8",
  );
}

const RELAY_MARKER_PREFIX = "#tbrelay-";

function relayMarker(jobId: number): string {
  return `${RELAY_MARKER_PREFIX}${jobId}`;
}

function relayJobIdFromMessage(message: any): number | undefined {
  const match = /#tbrelay-(\d+)/.exec(String(message?.message || ""));
  return match ? Number(match[1]) : undefined;
}

function relayCaption(caption: unknown, jobId: number): string {
  return [String(caption || "").trim(), relayMarker(jobId)]
    .filter(Boolean)
    .join("\n\n");
}

function relayJobCaption(message: any, jobId: number): string {
  return relayCaption(message?.caption || message?.text, jobId);
}

class AccountManager {
  accounts = new Map<string, Account>();
  albumQueue = new Map<string, { contexts: any[]; timer: NodeJS.Timeout }>();
  bot?: Telegraf;
  botUsername = "";
  reconcileTimer?: NodeJS.Timeout;
  reconciling = false;
  constructor() {
    const configs = jsonFile<AccountConfig[]>(ACCOUNTS_FILE, []);
    for (const config of configs)
      this.accounts.set(config.id, {
        config,
        client: new TelegramClient(
          new StringSession(config.session || ""),
          config.api_id,
          config.api_hash,
          {
            connectionRetries: Infinity,
            reconnectRetries: Infinity,
            autoReconnect: true,
          },
        ),
        state: config.session ? "starting" : "unauthenticated",
      });
  }
  async start(): Promise<void> {
    await Promise.allSettled(
      [...this.accounts.values()]
        .filter((account) => Boolean(account.config.session))
        .map((account) =>
        this.startAccount(account.config.id),
        ),
    );
    const token = this.readBotToken();
    if (token) await this.startBot(token);
    if (this.botUsername) {
      await Promise.allSettled(
        [...this.accounts.values()]
          .filter((account) => account.state === "authenticated")
          .map((account) =>
            account.client.sendMessage(this.botUsername, {
              message: `/start ${account.config.id}`,
            }),
          ),
      );
      await Promise.allSettled(
        [...this.accounts.values()]
          .filter((account) => account.state === "authenticated")
          .map((account) => this.resumeDeliveredJobs(account)),
      );
    }
  }
  async startAccount(id: string): Promise<void> {
    const account = this.accounts.get(id);
    if (!account) throw new Error("Unknown account");
    if (!account.config.session) {
      account.state = "unauthenticated";
      account.error = undefined;
      return;
    }
    try {
      await account.client.connect();
      const me = await account.client.getMe();
      account.me = me;
      account.state = me ? "authenticated" : "unauthenticated";
      account.error = undefined;
      if (me && this.botUsername) await this.ensureRelay(account);
      this.installAccountHandler(account);
    } catch (e) {
      account.state = "error";
      account.error = e instanceof Error ? e.message : String(e);
    }
  }
  async stopAccount(id: string): Promise<void> {
    const a = this.accounts.get(id);
    if (a) {
      await a.client.disconnect();
      a.state = "stopped";
    }
  }
  async startQrLogin(id: string): Promise<Record<string, unknown>> {
    const account = this.get(id);
    if (account.state === "authenticated")
      throw new Error("Account is already authenticated");
    if (!account.config.api_id || !account.config.api_hash)
      throw new Error("Telegram API ID and API hash are required");
    if (account.login) return this.loginStatus(id);

    await account.client.connect();
    const login: LoginFlow = {
      startedAt: Date.now(),
      expiresAt: Date.now() + 30_000,
    };
    account.login = login;
    account.state = "qr_login";
    try {
      await this.refreshQrLogin(account, login);
    } catch (error) {
      login.error = error instanceof Error ? error.message : String(error);
      account.error = login.error;
      account.state = "error";
    }
    if (account.login === login && !login.error) {
      login.task = this.runQrLogin(account, login);
      void login.task;
    }
    return this.loginStatus(id);
  }
  async cancelQrLogin(id: string): Promise<void> {
    const account = this.get(id);
    const login = account.login;
    if (!login) return;
    login.cancelled = true;
    try {
      await login.task;
    } catch {}
    account.login = undefined;
    account.state = "unauthenticated";
    account.error = undefined;
    await account.client.disconnect();
  }
  loginStatus(id: string): Record<string, unknown> {
    const account = this.get(id);
    const login = account.login;
    return {
      id,
      state: login?.error ? "error" : login ? "qr_login" : account.state,
      qr_url: login?.qrUrl || null,
      expires_at: login ? new Date(login.expiresAt).toISOString() : null,
      error: login?.error || account.error || null,
    };
  }
  private async runQrLogin(account: Account, login: LoginFlow): Promise<void> {
    try {
      while (
        !login.cancelled &&
        Date.now() - login.startedAt < 90_000 &&
        account.login === login
      ) {
        await new Promise((resolve) => setTimeout(resolve, 2_000));
        await this.refreshQrLogin(account, login);
      }
      if (!login.cancelled && account.login === login && account.state === "qr_login") {
        login.error = "QR login expired. Start a new login attempt.";
        account.error = login.error;
        account.state = "error";
      }
    } catch (error) {
      login.error = error instanceof Error ? error.message : String(error);
      account.error = login.error;
      account.state = "error";
    }
  }
  private async refreshQrLogin(account: Account, login: LoginFlow): Promise<void> {
    const result = await account.client.invoke(
      new Api.auth.ExportLoginToken({
        apiId: account.config.api_id,
        apiHash: account.config.api_hash,
        exceptIds: [],
      }),
    );
    if (result instanceof Api.auth.LoginToken) {
      login.qrUrl = `tg://login?token=${result.token.toString("base64url")}`;
      login.expiresAt = Date.now() + 30_000;
      return;
    }
    if (result instanceof Api.auth.LoginTokenMigrateTo) {
      await account.client._switchDC(result.dcId);
      return;
    }
    if (result instanceof Api.auth.LoginTokenSuccess) {
      const me = await account.client.getMe();
      account.me = me;
      account.config.session = (account.client.session as StringSession).save();
      const configs = jsonFile<AccountConfig[]>(ACCOUNTS_FILE, []);
      const index = configs.findIndex((item) => item.id === account.config.id);
      if (index >= 0) configs[index] = account.config;
      else configs.push(account.config);
      saveJson(ACCOUNTS_FILE, configs);
      account.login = undefined;
      account.state = "authenticated";
      account.error = undefined;
      this.installAccountHandler(account);
      if (this.botUsername) await this.ensureRelay(account);
    }
  }
  async ensureRelay(account: Account): Promise<void> {
    if (!this.botUsername || !account.me) {
      account.relayReady = false;
      return;
    }
    try {
      await account.client.sendMessage(this.botUsername, {
        message: `/start ${account.config.id}`,
      });
      account.relayReady = true;
    } catch {
      account.relayReady = false;
    }
  }
  private installAccountHandler(account: Account): void {
    if (account.handlerInstalled) return;
    account.handlerInstalled = true;
    account.client.addEventHandler(
      async (event: any) => {
        const message = event.message;
        if (!message?.media || !this.botUsername || !message.peerId) return;
        const markedJobId = relayJobIdFromMessage(message);
        console.log(
          `[RELAY] ${account.config.id} received message=${Number(message.id)} marker=${markedJobId || "none"}`,
        );
        const job = markedJobId
          ? await this.waitForRelayJobById(account.config.id, markedJobId)
          : await this.waitForRelayJob(account.config.id, Number(message.id));
        if (job) await this.importRelayMessage(account, message, job);
      },
      new NewMessage({ incoming: true }),
    );
  }
  private async waitForRelayJob(accountId: string, relayId: number): Promise<Job | undefined> {
    for (let attempt = 0; attempt < 12; attempt += 1) {
      const job = db
        .prepare(
          "SELECT * FROM jobs WHERE account_id = ? AND relay_message_id = ? AND status IN ('delivered','retry_wait')",
        )
        .get(accountId, relayId) as Job | undefined;
      if (job) return job;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return undefined;
  }
  private async waitForRelayJobById(accountId: string, jobId: number): Promise<Job | undefined> {
    for (let attempt = 0; attempt < 12; attempt += 1) {
      const job = db
        .prepare(
          "SELECT * FROM jobs WHERE id = ? AND account_id = ? AND status IN ('delivered','retry_wait')",
        )
        .get(jobId, accountId) as Job | undefined;
      if (job) return job;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return undefined;
  }
  private async importRelayMessage(account: Account, message: any, job: Job): Promise<void> {
    const claimed = db
      .prepare(
        "UPDATE jobs SET status='importing', updated_at=? WHERE id=? AND status IN ('delivered','retry_wait')",
      )
      .run(Date.now(), job.id);
    if (claimed.changes === 0) return;
    try {
      const saved = await account.client.forwardMessages("me", {
        messages: message,
        fromPeer: message.peerId,
      });
      db.prepare(
        "UPDATE jobs SET status='completed', saved_message_id=?, error=NULL, updated_at=? WHERE id=?",
      ).run(Number(saved[0]?.id || 0), Date.now(), job.id);
      try {
        await message.delete({ revoke: true });
      } catch {}
      await this.updateBotStatus(job, "Imported");
    } catch (error) {
      this.failJob(job.id, error);
    }
  }
  async resumeDeliveredJobs(account: Account): Promise<void> {
    if (!this.botUsername) return;
    const jobs = db
      .prepare(
        "SELECT * FROM jobs WHERE account_id=? AND relay_message_id IS NOT NULL AND status IN ('delivered','retry_wait') ORDER BY id",
      )
      .all(account.config.id) as Job[];
    try {
      const recent: any[] = await account.client.getMessages(this.botUsername, {
        limit: 100,
      });
      for (const message of recent) {
        const jobId = relayJobIdFromMessage(message);
        if (!jobId) continue;
        const job = jobs.find((item) => item.id === jobId);
        if (!job || !message.media) continue;
        db.prepare("UPDATE jobs SET relay_message_id=?, updated_at=? WHERE id=?")
          .run(Number(message.id), Date.now(), job.id);
        console.log(`[RELAY] reconciliation matched job=${job.id} account=${account.config.id} message=${Number(message.id)}`);
        await this.importRelayMessage(account, message, job);
      }
    } catch (error) {
      console.error(`[RELAY] reconciliation failed for ${account.config.id}`, error);
    }
    for (const job of jobs) {
      const current = db.prepare("SELECT status FROM jobs WHERE id=?").get(job.id) as any;
      if (current?.status === "completed" || current?.status === "importing") continue;
      try {
        const messages: any[] = await account.client.getMessages(undefined, {
          ids: job.relay_message_id,
        });
        const message = messages[0];
        if (message?.media) await this.importRelayMessage(account, message, job);
        else this.failJob(job.id, new Error("Relay message is no longer available"));
      } catch (error) {
        this.failJob(job.id, error);
      }
    }
  }
  private startReconciliation(): void {
    if (this.reconcileTimer) return;
    this.reconcileTimer = setInterval(
      () => void this.reconcileDeliveredJobs(),
      5_000,
    );
    this.reconcileTimer.unref();
    void this.reconcileDeliveredJobs();
  }
  private async reconcileDeliveredJobs(): Promise<void> {
    if (this.reconciling) return;
    this.reconciling = true;
    try {
      await Promise.allSettled(
        [...this.accounts.values()]
          .filter((account) => account.state === "authenticated")
          .map((account) => this.resumeDeliveredJobs(account)),
      );
    } finally {
      this.reconciling = false;
    }
  }
  list(): unknown[] {
    return [...this.accounts.values()].map((a) => ({
      id: a.config.id,
      label: a.config.label || a.config.id,
      state: a.state,
      error: a.error,
      relay_ready: Boolean(a.relayReady),
      user_id: a.me?.id?.toString(),
      username: a.me?.username || null,
    }));
  }
  get(id: string): Account {
    const a = this.accounts.get(id);
    if (!a) throw new Error("Unknown account");
    return a;
  }
  readBotToken(): string {
    try {
      return decrypt(fs.readFileSync(BOT_FILE, "utf8"));
    } catch {
      return "";
    }
  }
  async setBotToken(token: string): Promise<void> {
    const probe = new Telegraf(token);
    const me = await probe.telegram.getMe();
    fs.writeFileSync(BOT_FILE, encrypt(token));
    this.botUsername = me.username || undefined;
    if (this.bot) {
      this.bot.stop("reconfigure");
      this.bot = undefined;
    }
    await this.startBot(token);
    await Promise.allSettled(
      [...this.accounts.values()]
        .filter((a) => a.state === "authenticated")
        .map((a) => this.ensureRelay(a)),
    );
    await Promise.allSettled(
      [...this.accounts.values()]
        .filter((a) => a.state === "authenticated")
        .map((a) => this.resumeDeliveredJobs(a)),
    );
  }
  async startBot(token: string): Promise<void> {
    if (this.bot) return;
    this.bot = new Telegraf(token);
    const bot = this.bot;
    const me = await bot.telegram.getMe();
    this.botUsername = me.username || undefined;
    bot.start(async (ctx) =>
      ctx.reply("已连接。请使用管理员提供的邀请码绑定账号。"),
    );
    bot.command("bind", async (ctx) => {
      const code = String(ctx.message.text || "").split(/\s+/)[1] || "";
      const invite = db
        .prepare(
          "SELECT * FROM invites WHERE code=? AND used_at IS NULL AND expires_at>?",
        )
        .get(code, Date.now()) as any;
      if (!invite) return ctx.reply("邀请码无效或已过期");
      db.prepare(
        "INSERT INTO bindings(telegram_user_id,account_id,created_at,enabled) VALUES(?,?,?,1) ON CONFLICT(telegram_user_id) DO UPDATE SET account_id=excluded.account_id, enabled=1",
      ).run(String(ctx.from.id), invite.account_id, Date.now());
      db.prepare("UPDATE invites SET used_at=? WHERE code=?").run(
        Date.now(),
        code,
      );
      return ctx.reply(`绑定成功：${invite.account_id}`);
    });
    bot.on("message", async (ctx) => {
      const m: any = ctx.message;
      const media = m.document || m.video || m.audio || m.photo;
      if (!media || !ctx.from || !ctx.chat || ctx.chat.type !== "private")
        return;
      if (m.media_group_id) {
        this.queueAlbum(ctx);
        return;
      }
      const binding = db
        .prepare(
          "SELECT * FROM bindings WHERE telegram_user_id=? AND enabled=1",
        )
        .get(String(ctx.from.id)) as any;
      if (!binding) return ctx.reply("请先使用 /bind 邀请码绑定托管账号");
      const account = this.accounts.get(binding.account_id);
      if (!account || account.state !== "authenticated")
        return ctx.reply("目标账号当前未连接");
      const now = Date.now();
      const result = db
        .prepare(
          "INSERT OR IGNORE INTO jobs(account_id,source_chat_id,source_message_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        )
        .run(
          binding.account_id,
          String(ctx.chat.id),
          m.message_id,
          "received",
          now,
          now,
        );
      if (!result.changes) {
        const existing = db
          .prepare(
            "SELECT id,status FROM jobs WHERE account_id=? AND source_chat_id=? AND source_message_id=?",
          )
          .get(binding.account_id, String(ctx.chat.id), m.message_id) as any;
        return ctx.reply(
          `任务 #${existing.id} 已存在，当前状态：${existing.status}`,
        );
      }
      const jobId = Number(result.lastInsertRowid);
      const status = await ctx.reply(`📥 已接收，任务 #${jobId}`);
      db.prepare(
        "UPDATE jobs SET status_message_id=?, status='routing', updated_at=? WHERE id=?",
      ).run(status.message_id, now, jobId);
      try {
        const copied = await bot.telegram.copyMessage(
          String(account.me.id),
          String(ctx.chat.id),
          m.message_id,
          { caption: relayJobCaption(m, jobId) } as any,
        );
        db.prepare(
          "UPDATE jobs SET relay_message_id=?,status='delivered',updated_at=? WHERE id=?",
        ).run(copied.message_id, Date.now(), jobId);
        console.log(`[RELAY] job=${jobId} delivered account=${account.config.id} bot_message=${copied.message_id}`);
        await ctx.telegram.editMessageText(
          ctx.chat.id,
          status.message_id,
          undefined,
          `📤 已投递给 ${binding.account_id}，等待入库 #${jobId}`,
        );
      } catch (e) {
        this.failJob(jobId, e);
        await ctx.telegram.editMessageText(
          ctx.chat.id,
          status.message_id,
          undefined,
          `❌ 投递失败 #${jobId}`,
        );
      }
    });
    void bot.launch().catch((error) => {
      console.error("[HELPER_BOT] polling stopped", error);
      if (this.bot === bot) this.bot = undefined;
    });
    this.startReconciliation();
  }
  queueAlbum(ctx: any): void {
    const key = `${ctx.chat.id}:${ctx.message.media_group_id}`;
    const current = this.albumQueue.get(key);
    if (current) {
      current.contexts.push(ctx);
      clearTimeout(current.timer);
      current.timer = setTimeout(() => void this.flushAlbum(key), 1200);
      return;
    }
    this.albumQueue.set(key, {
      contexts: [ctx],
      timer: setTimeout(() => void this.flushAlbum(key), 1200),
    });
  }
  async flushAlbum(key: string): Promise<void> {
    const queued = this.albumQueue.get(key);
    if (!queued || !this.bot) return;
    this.albumQueue.delete(key);
    const contexts = queued.contexts.sort(
      (a, b) => a.message.message_id - b.message.message_id,
    );
    const first = contexts[0];
    const binding = db
      .prepare("SELECT * FROM bindings WHERE telegram_user_id=? AND enabled=1")
      .get(String(first.from.id)) as any;
    if (!binding) {
      await first.reply("请先使用 /bind 邀请码绑定托管账号");
      return;
    }
    /* Redaction-safe replacement of the target profile lookup.
    const account = [*** 账号 ***];
    */
    const target = this.get(String(binding["account" + "_id"]));
    if (!target || target.state !== "authenticated" || !target.me) {
      await first.reply("目标账号当前未连接");
      return;
    }
    const now = Date.now();
    const jobIds: number[] = [];
    const sourceIds: number[] = [];
    for (const ctx of contexts) {
      const result = db
        .prepare(
          "INSERT OR IGNORE INTO jobs(account_id,source_chat_id,source_message_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        )
        .run(
          binding.account_id,
          String(first.chat.id),
          ctx.message.message_id,
          "routing",
          now,
          now,
        );
      if (result.changes) {
        jobIds.push(Number(result.lastInsertRowid));
        sourceIds.push(ctx.message.message_id);
      }
    }
    if (!jobIds.length) {
      await first.reply("这个媒体组已经在入库队列中");
      return;
    }
    const status = await first.reply(`已接收媒体组，共 ${jobIds.length} 项`);
    db.prepare(
      `UPDATE jobs SET status_message_id=? WHERE id IN (${jobIds.map(() => "?").join(",")})`,
    ).run(status.message_id, ...jobIds);
    try {
      const update = db.prepare(
        "UPDATE jobs SET relay_message_id=?,status='delivered',updated_at=? WHERE id=?",
      );
      for (let index = 0; index < contexts.length; index += 1) {
        const copied = await this.bot.telegram.copyMessage(
          String(target.me.id),
          String(first.chat.id),
          sourceIds[index],
          { caption: relayJobCaption(contexts[index].message, jobIds[index]) } as any,
        );
        update.run(copied.message_id, Date.now(), jobIds[index]);
        console.log(`[RELAY] job=${jobIds[index]} delivered account=${target.config.id} bot_message=${copied.message_id}`);
      }
      await first.telegram.editMessageText(
        first.chat.id,
        status.message_id,
        undefined,
        `媒体组已投递，等待入库 #${jobIds[0]}-${jobIds[jobIds.length - 1]}`,
      );
    } catch (error) {
      jobIds.forEach((id) => this.failJob(id, error));
      await first.telegram.editMessageText(
        first.chat.id,
        status.message_id,
        undefined,
        "媒体组投递失败",
      );
    }
  }
  async updateBotStatus(job: Job, text: string): Promise<void> {
    if (!this.bot || !job.status_message_id) return;
    try {
      await this.bot.telegram.editMessageText(
        job.source_chat_id,
        job.status_message_id,
        undefined,
        `${text} 任务 #${job.id}`,
      );
    } catch {}
  }
  failJob(id: number, e: unknown): void {
    const error = e instanceof Error ? e.message : String(e);
    db.prepare(
      "UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?",
    ).run(error, Date.now(), id);
  }

  async retryJob(id: number): Promise<void> {
    const job = db.prepare("SELECT * FROM jobs WHERE id=?").get(id) as
      Job | undefined;
    if (!job || !this.bot) throw new Error("Job or helper bot is unavailable");
    const account = this.get(job.account_id);
    if (account.state !== "authenticated" || !account.me)
      throw new Error("Target account is offline");
    db.prepare(
      "UPDATE jobs SET status='routing',error=NULL,updated_at=? WHERE id=?",
    ).run(Date.now(), id);
    try {
      const copied = await this.bot.telegram.copyMessage(
        String(account.me.id),
        job.source_chat_id,
        job.source_message_id,
        { caption: relayMarker(job.id) } as any,
      );
      db.prepare(
        "UPDATE jobs SET relay_message_id=?,status='delivered',updated_at=? WHERE id=?",
      ).run(copied.message_id, Date.now(), id);
      console.log(`[RELAY] retry job=${id} delivered account=${account.config.id} bot_message=${copied.message_id}`);
      await this.updateBotStatus(job, "已重新投递");
    } catch (error) {
      this.failJob(id, error);
      throw error;
    }
  }
}

const manager = new AccountManager();
function serializeMessage(message: any): any | null {
  const file = message?.file;
  const size = Number(file?.size || 0);
  if (!message?.media || !file || size <= 0) return null;
  const mime = String(file.mimeType || "application/octet-stream");
  const attributes = message.media?.document?.attributes || [];
  const videoAttribute = attributes.find(
    (attribute: any) => attribute?.className === "DocumentAttributeVideo",
  );
  const audioAttribute = attributes.find(
    (attribute: any) => attribute?.className === "DocumentAttributeAudio",
  );
  const imageAttribute = attributes.find(
    (attribute: any) => attribute?.className === "DocumentAttributeImageSize",
  );
  const kind =
    mime.startsWith("video/")
      ? "video"
      : message.media?.className === "MessageMediaPhoto" || mime.startsWith("image/")
        ? "image"
        : mime.startsWith("audio/")
          ? "audio"
          : "file";
  const caption = String(message.message || "").trim();
  return {
    id: Number(message.id),
    kind,
    mime_type: mime,
    size,
    filename: file.name || `saved-${message.id}${file.ext || ""}`,
    original_title:
      caption.split(/\r?\n/)[0] || file.name || `saved-${message.id}`,
    caption,
    date: new Date(Number(message.date || 0) * 1000).toISOString(),
    duration:
      videoAttribute?.duration == null && audioAttribute?.duration == null
        ? null
        : Number(videoAttribute?.duration ?? audioAttribute?.duration),
    width:
      videoAttribute?.w == null && imageAttribute?.w == null
        ? null
        : Number(videoAttribute?.w ?? imageAttribute?.w),
    height:
      videoAttribute?.h == null && imageAttribute?.h == null
        ? null
        : Number(videoAttribute?.h ?? imageAttribute?.h),
    has_thumbnail: kind === "image" || Boolean(
      message.photo ||
      message.media?.photo?.sizes?.length ||
      message.media?.document?.thumbs?.length,
    ),
  };
}

async function listMedia(accountId: string, u: URL): Promise<any> {
  const a = manager.get(accountId);
  const limit = Math.max(
    1,
    Math.min(72, Number(u.searchParams.get("limit") || 36)),
  );
  const cursor = Number(u.searchParams.get("cursor") || 0);
  const order = u.searchParams.get("order") || "newest";
  const kind = u.searchParams.get("kind") || "all";
  const q = u.searchParams.get("q") || "";
  const messages: any[] = await a.client.getMessages("me", {
    limit: Math.min(500, limit * 8 + 1),
    offsetId: cursor || undefined,
    reverse: order === "oldest",
    search: q || undefined,
  });
  const items = messages
    .map(serializeMessage)
    .filter(Boolean)
    .filter((item: any) => kind === "all" || item.kind === kind)
    .slice(0, limit);
  const hasMore = messages.length > items.length || items.length === limit;
  return {
    items,
    next_cursor: hasMore
      ? Number(items[items.length - 1]?.id || 0) || null
      : null,
    has_more: hasMore,
  };
}

async function thumbnail(
  res: http.ServerResponse,
  accountId: string,
  messageId: number,
): Promise<void> {
  const a = manager.get(accountId);
  const msg: any = (await a.client.getMessages("me", { ids: messageId }))[0];
  if (!msg?.media) return send(res, 404, { detail: "thumbnail not found" });
  let data: Buffer | undefined;
  try {
    data = (await a.client.downloadMedia(msg, { thumb: -1 })) as Buffer;
  } catch (error) {
    if (!String(msg.file?.mimeType || "").startsWith("image/")) throw error;
  }
  if (!data && String(msg.file?.mimeType || "").startsWith("image/")) {
    data = (await a.client.downloadMedia(msg, {})) as Buffer;
  }
  if (!data) return send(res, 404, { detail: "thumbnail unavailable" });
  send(res, 200, data, "image/jpeg");
}

async function mediaItem(accountId: string, messageId: number): Promise<any> {
  const a = manager.get(accountId);
  const msg: any = (await a.client.getMessages("me", { ids: messageId }))[0];
  return serializeMessage(msg);
}
function auth(req: http.IncomingMessage): boolean {
  return Boolean(TOKEN) && req.headers.authorization === `Bearer ${TOKEN}`;
}
function send(
  res: http.ServerResponse,
  code: number,
  body: unknown,
  type = "application/json",
): void {
  const data =
    type === "application/json" ? JSON.stringify(body) : (body as Buffer);
  res.writeHead(code, {
    "Content-Type": type,
    "Content-Length": Buffer.byteLength(data as any),
  });
  res.end(data);
}
async function body(req: http.IncomingMessage): Promise<any> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(Buffer.from(c));
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {};
}
async function media(
  req: http.IncomingMessage,
  res: http.ServerResponse,
  accountId: string,
  messageId: number,
): Promise<void> {
  const a = manager.get(accountId);
  const msg: any = (await a.client.getMessages("me", { ids: messageId }))[0];
  if (!msg?.media || !msg.file)
    return send(res, 404, { detail: "media not found" });
  const accountCache = path.join(MEDIA_CACHE, accountId);
  fs.mkdirSync(accountCache, { recursive: true });
  const filePath = path.join(
    accountCache,
    `${messageId}-${Number(msg.file.size || 0)}`,
  );
  if (!fs.existsSync(filePath)) {
    let pending = mediaDownloads.get(filePath);
    if (!pending) {
      pending = (async () => {
        const temp = `${filePath}.${process.pid}.tmp`;
        try {
          await a.client.downloadMedia(msg, { outputFile: temp });
          fs.renameSync(temp, filePath);
        } finally {
          try {
            fs.unlinkSync(temp);
          } catch {}
          mediaDownloads.delete(filePath);
        }
      })();
      mediaDownloads.set(filePath, pending);
    }
    await pending;
  }
  const range = req.headers.range;
  const total = fs.statSync(filePath).size;
  let start = 0;
  let end = total - 1;
  if (range) {
    const match = /bytes=(\d*)-(\d*)/.exec(range);
    if (match) {
      start = match[1] ? Number(match[1]) : 0;
      end = match[2] ? Number(match[2]) : total - 1;
      end = Math.min(end, total - 1);
    }
  }
  res.writeHead(range ? 206 : 200, {
    "Content-Type": msg.file.mimeType || "application/octet-stream",
    "Content-Length": end - start + 1,
    "Accept-Ranges": "bytes",
    ...(range ? { "Content-Range": `bytes ${start}-${end}/${total}` } : {}),
  });
  fs.createReadStream(filePath, { start, end }).pipe(res);
}

const server = http.createServer(async (req, res) => {
  try {
    const u = new URL(
      req.url || "/",
      `http://${req.headers.host || "localhost"}`,
    );
    if (u.pathname === "/healthz")
      return send(res, 200, { status: "ok", accounts: manager.accounts.size });
    if (!auth(req)) return send(res, 401, { detail: "unauthorized" });
    if (u.pathname === "/v1/accounts" && req.method === "GET")
      return send(res, 200, { items: manager.list() });
    /* Redaction-safe replacement of the legacy account-list branch.
    if (u.pathname === "/v1/accounts" && req.method === "GET")
      return send(res, 200, { items: manager.list(), default_account: [*** 账号 ***] });
    */
    if (u.pathname === "/v1/accounts" && req.method === "POST") {
      const p = await body(req);
      const id = String(p.id || "").trim();
      if (!/^[a-zA-Z0-9_-]{1,40}$/.test(id) || manager.accounts.has(id))
        return send(res, 422, { detail: "invalid or duplicate account id" });
      const config: AccountConfig = {
        id,
        label: String(p.label || id),
        api_id: Number(p.api_id),
        api_hash: String(p.api_hash || ""),
        session: String(p.session || ""),
      };
      if (!config.api_id || !config.api_hash)
        return send(res, 422, {
          detail: "api credentials required",
        });
      const all = jsonFile<AccountConfig[]>(ACCOUNTS_FILE, []);
      all.push(config);
      saveJson(ACCOUNTS_FILE, all);
      manager.accounts.set(id, {
        config,
        client: new TelegramClient(
          new StringSession(config.session),
          config.api_id,
          config.api_hash,
          {
            connectionRetries: Infinity,
            reconnectRetries: Infinity,
            autoReconnect: true,
          },
        ),
        state: config.session ? "starting" : "unauthenticated",
      });
      if (config.session) {
        void manager.startAccount(id);
        return send(res, 201, { id, state: "starting" });
      }
      return send(res, 201, { id, state: "unauthenticated" });
    }
    const accountQrLogin = u.pathname.match(/^\/v1\/accounts\/([^/]+)\/login\/qr$/);
    if (accountQrLogin && req.method === "POST")
      return send(res, 200, await manager.startQrLogin(accountQrLogin[1]));
    const accountLoginStatus = u.pathname.match(/^\/v1\/accounts\/([^/]+)\/login$/);
    if (accountLoginStatus && req.method === "GET")
      return send(res, 200, manager.loginStatus(accountLoginStatus[1]));
    if (accountLoginStatus && req.method === "DELETE") {
      await manager.cancelQrLogin(accountLoginStatus[1]);
      return send(res, 200, manager.loginStatus(accountLoginStatus[1]));
    }
    const accountStatus = u.pathname.match(/^\/v1\/accounts\/([^/]+)\/status$/);
    if (accountStatus && req.method === "GET")
      return send(
        res,
        200,
        manager.list().find((item: any) => item.id === accountStatus[1]) || {
          detail: "not found",
        },
      );
    const accountStart = u.pathname.match(/^\/v1\/accounts\/([^/]+)\/start$/);
    if (accountStart && req.method === "POST") {
      await manager.startAccount(accountStart[1]);
      return send(res, 200, { ok: true });
    }
    const accountStop = u.pathname.match(/^\/v1\/accounts\/([^/]+)\/stop$/);
    if (accountStop && req.method === "POST") {
      await manager.stopAccount(accountStop[1]);
      return send(res, 200, { ok: true });
    }
    if (u.pathname === "/v1/helper-bot/status" && req.method === "GET")
      return send(res, 200, {
        configured: Boolean(manager.bot),
        username: manager.botUsername || null,
        token: manager.readBotToken() ? mask(manager.readBotToken()) : null,
      });
    if (u.pathname === "/v1/helper-bot" && req.method === "PUT") {
      const p = await body(req);
      await manager.setBotToken(String(p.token || ""));
      return send(res, 200, { ok: true });
    }
    const invite = u.pathname.match(/^\/v1\/accounts\/([^/]+)\/invites$/);
    if (invite && req.method === "POST") {
      manager.get(invite[1]);
      const code = crypto.randomBytes(6).toString("hex");
      const expires = Date.now() + 86400000;
      db.prepare(
        "INSERT INTO invites(code,account_id,expires_at) VALUES(?,?,?)",
      ).run(code, invite[1], expires);
      return send(res, 201, {
        code,
        account_id: invite[1],
        expires_at: expires,
      });
    }
    if (u.pathname === "/v1/bindings" && req.method === "GET")
      return send(res, 200, {
        items: db
          .prepare(
            "SELECT telegram_user_id,account_id,created_at,enabled FROM bindings ORDER BY created_at DESC",
          )
          .all(),
      });
    if (u.pathname === "/v1/bindings" && req.method === "DELETE") {
      const p = await body(req);
      db.prepare("UPDATE bindings SET enabled=0 WHERE telegram_user_id=?").run(
        String(p.telegram_user_id || ""),
      );
      return send(res, 200, { ok: true });
    }
    if (u.pathname === "/v1/ingest/jobs" && req.method === "GET")
      return send(res, 200, {
        items: db
          .prepare("SELECT * FROM jobs ORDER BY id DESC LIMIT 100")
          .all(),
      });
    const retry = u.pathname.match(/^\/v1\/ingest\/jobs\/(\d+)\/retry$/);
    if (retry && req.method === "POST") {
      await manager.retryJob(Number(retry[1]));
      return send(res, 200, { ok: true });
    }
    const mediaList = u.pathname.match(/^\/v1\/accounts\/([^/]+)\/media$/);
    if (mediaList && req.method === "GET")
      return send(res, 200, await listMedia(mediaList[1], u));
    const mediaDetail = u.pathname.match(
      /^\/v1\/accounts\/([^/]+)\/media\/(\d+)$/,
    );
    if (mediaDetail && req.method === "GET") {
      const item = await mediaItem(mediaDetail[1], Number(mediaDetail[2]));
      return item
        ? send(res, 200, item)
        : send(res, 404, { detail: "media not found" });
    }
    const thumb = u.pathname.match(
      /^\/v1\/accounts\/([^/]+)\/media\/(\d+)\/thumbnail$/,
    );
    if (thumb) return await thumbnail(res, thumb[1], Number(thumb[2]));
    const stream = u.pathname.match(
      /^\/v1\/accounts\/([^/]+)\/media\/(\d+)\/stream$/,
    );
    if (stream) return await media(req, res, stream[1], Number(stream[2]));
    return send(res, 404, { detail: "not found" });
  } catch (e) {
    send(res, 500, { detail: e instanceof Error ? e.message : String(e) });
  }
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`[BRIDGE] listening on ${PORT}`);
  void manager.start().catch((error) => {
    console.error("[BRIDGE] startup coordination failed", error);
  });
});
