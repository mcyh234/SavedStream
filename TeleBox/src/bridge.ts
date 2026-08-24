// @ts-nocheck
import "dotenv/config";
import fs from "fs";
import path from "path";
import crypto from "crypto";
import http from "http";
import { pipeline } from "stream/promises";
import { Api, TelegramClient } from "teleproto";
import { StringSession } from "teleproto/sessions";
import { CustomFile } from "teleproto/client/uploads";
import { NewMessage } from "teleproto/events";
import { Telegraf } from "telegraf";
import Database from "better-sqlite3";
import { boundWebLoginIdentity, consumeWebLoginCode, issueWebLoginCode } from "./web-login";
import { decodeBase64UrlHeader, syncPaginationCursor, uploadBodyMatchesLength } from "./bridge-media";

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
  submitter_telegram_user_id?: string;
  source_message_id: number;
  relay_message_id?: number;
  saved_message_id?: number;
  status: string;
  status_message_id?: number;
  error?: string;
  requested_visibility?: "private" | "public";
  review_status?: "not_required" | "pending" | "approved" | "rejected" | "revoked";
  review_reason?: string | null;
  reviewed_at?: number | null;
  reviewed_by?: string | null;
  review_batch_id?: string | null;
  source_file_size?: number;
  source_filename?: string | null;
  source_mime_type?: string | null;
  file_count?: number;
  choice_expires_at?: number | null;
  rate_reservation_key?: string | null;
};

const DATA = path.resolve(process.env.TELEBOX_DATA_DIR || "/data");
const TOKEN = process.env.TELEBOX_API_TOKEN || "";
const PORT = Number(process.env.TELEBOX_PORT || 9000);
const DEFAULT_ACCOUNT = process.env.TELEBOX_DEFAULT_ACCOUNT || "default";
const ACCOUNTS_FILE = path.join(DATA, "accounts.json");
const DB_FILE = path.join(DATA, "bridge.db");
const BOT_FILE = path.join(DATA, "helper-bot.enc");
const MEDIA_CACHE = path.join(DATA, "media-cache");
const UPLOAD_SPOOL = path.join(DATA, "upload-spool");
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
fs.mkdirSync(UPLOAD_SPOOL, { recursive: true, mode: 0o700 });
const db = new Database(DB_FILE);
db.exec(`CREATE TABLE IF NOT EXISTS invites (code TEXT PRIMARY KEY, account_id TEXT NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER);
CREATE TABLE IF NOT EXISTS bindings (telegram_user_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, created_at INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, source_chat_id TEXT NOT NULL, source_message_id INTEGER NOT NULL, relay_message_id INTEGER, saved_message_id INTEGER, status TEXT NOT NULL, status_message_id INTEGER, error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, requested_visibility TEXT NOT NULL DEFAULT 'private', review_status TEXT NOT NULL DEFAULT 'not_required', review_reason TEXT, reviewed_at INTEGER, reviewed_by TEXT, review_batch_id TEXT, source_file_size INTEGER NOT NULL DEFAULT 0, source_filename TEXT, source_mime_type TEXT, file_count INTEGER NOT NULL DEFAULT 1, choice_expires_at INTEGER, rate_reservation_key TEXT, UNIQUE(account_id, source_chat_id, source_message_id));
CREATE TABLE IF NOT EXISTS web_login_codes (code_hash TEXT PRIMARY KEY, telegram_user_id TEXT NOT NULL, account_id TEXT NOT NULL, username TEXT, display_name TEXT NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER);
CREATE TABLE IF NOT EXISTS relay_pairs (account_id TEXT PRIMARY KEY, telegram_user_id TEXT NOT NULL, paired_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS helper_bans (telegram_user_id TEXT PRIMARY KEY, reason TEXT, banned_at INTEGER NOT NULL, banned_by TEXT NOT NULL);`);
let jobColumns = new Set(
  (db.prepare("PRAGMA table_info(jobs)").all() as Array<{ name: string }>).map(
    (column) => column.name,
  ),
);
if (!jobColumns.has("submitter_telegram_user_id")) {
  db.exec("ALTER TABLE jobs ADD COLUMN submitter_telegram_user_id TEXT");
}
const jobMigrations: Array<[string, string]> = [
  ["requested_visibility", "TEXT NOT NULL DEFAULT 'private'"],
  ["review_status", "TEXT NOT NULL DEFAULT 'not_required'"],
  ["review_reason", "TEXT"],
  ["reviewed_at", "INTEGER"],
  ["reviewed_by", "TEXT"],
  ["review_batch_id", "TEXT"],
  ["source_file_size", "INTEGER NOT NULL DEFAULT 0"],
  ["source_filename", "TEXT"],
  ["source_mime_type", "TEXT"],
  ["file_count", "INTEGER NOT NULL DEFAULT 1"],
  ["choice_expires_at", "INTEGER"],
  ["rate_reservation_key", "TEXT"],
];
for (const [name, definition] of jobMigrations) {
  if (!jobColumns.has(name)) db.exec(`ALTER TABLE jobs ADD COLUMN ${name} ${definition}`);
}
jobColumns = new Set(
  (db.prepare("PRAGMA table_info(jobs)").all() as Array<{ name: string }>).map(
    (column) => column.name,
  ),
);
// Helper Bot currently accepts private chats only, therefore legacy
// source_chat_id values are also the submitting Telegram user IDs.
db.prepare(
  "UPDATE jobs SET submitter_telegram_user_id=source_chat_id WHERE submitter_telegram_user_id IS NULL OR submitter_telegram_user_id=''",
).run();
// A process crash between the callback claim and the durable rate
// reservation must not leave the user with a permanently stuck job.
db.prepare(
  "UPDATE jobs SET status='awaiting_choice',error='选择操作被进程中断，请重新选择',updated_at=? WHERE status='rate_checking'",
).run(Date.now());
db.exec(`CREATE INDEX IF NOT EXISTS jobs_review_idx ON jobs(review_status,status,updated_at);
CREATE INDEX IF NOT EXISTS jobs_batch_idx ON jobs(review_batch_id,status);
CREATE INDEX IF NOT EXISTS jobs_submitter_idx ON jobs(submitter_telegram_user_id,status,updated_at);
CREATE TABLE IF NOT EXISTS helper_rate_limit_settings (
  id INTEGER PRIMARY KEY CHECK(id=1),
  per_user_files_24h INTEGER NOT NULL,
  per_user_bytes_24h INTEGER NOT NULL,
  per_user_concurrent INTEGER NOT NULL,
  max_file_bytes INTEGER NOT NULL,
  global_files_per_minute INTEGER NOT NULL,
  max_album_items INTEGER NOT NULL,
  max_album_bytes INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS helper_rate_reservations (
  reservation_key TEXT PRIMARY KEY,
  batch_id TEXT,
  user_id TEXT NOT NULL,
  file_count INTEGER NOT NULL,
  total_bytes INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('reserved','completed','released')),
  reserved_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS helper_rate_reservation_user_idx ON helper_rate_reservations(user_id,status,reserved_at);
CREATE TABLE IF NOT EXISTS helper_rate_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reservation_key TEXT NOT NULL,
  user_id TEXT NOT NULL,
  file_count INTEGER NOT NULL,
  total_bytes INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(reservation_key)
);
CREATE INDEX IF NOT EXISTS helper_rate_events_user_idx ON helper_rate_events(user_id,created_at);
`);
db.prepare(
  "INSERT OR IGNORE INTO helper_rate_limit_settings(" +
  "id,per_user_files_24h,per_user_bytes_24h,per_user_concurrent,max_file_bytes," +
  "global_files_per_minute,max_album_items,max_album_bytes,updated_at) " +
  "VALUES(1,20,10000000000,2,2000000000,30,10,2000000000,?)",
).run(Date.now());

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

type RateLimitSettings = {
  per_user_files_24h: number;
  per_user_bytes_24h: number;
  per_user_concurrent: number;
  max_file_bytes: number;
  global_files_per_minute: number;
  max_album_items: number;
  max_album_bytes: number;
};

class HelperRateLimitError extends Error {
  retryAfterSeconds: number;
  constructor(message: string, retryAfterSeconds = 60) {
    super(message);
    this.name = "HelperRateLimitError";
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

function readRateLimitSettings(): RateLimitSettings {
  const row = db.prepare("SELECT * FROM helper_rate_limit_settings WHERE id=1").get() as any;
  return {
    per_user_files_24h: Number(row?.per_user_files_24h || 20),
    per_user_bytes_24h: Number(row?.per_user_bytes_24h || 10_000_000_000),
    per_user_concurrent: Number(row?.per_user_concurrent || 2),
    max_file_bytes: Number(row?.max_file_bytes || 2_000_000_000),
    global_files_per_minute: Number(row?.global_files_per_minute || 30),
    max_album_items: Number(row?.max_album_items || 10),
    max_album_bytes: Number(row?.max_album_bytes || 2_000_000_000),
  };
}

function writeRateLimitSettings(value: RateLimitSettings): RateLimitSettings {
  const integer = (raw: unknown, name: string): number => {
    const number = Number(raw);
    if (!Number.isFinite(number) || number < 1) throw new Error(`${name} must be a positive number`);
    return Math.floor(number);
  };
  const normalized: RateLimitSettings = {
    per_user_files_24h: integer(value.per_user_files_24h, "per_user_files_24h"),
    per_user_bytes_24h: integer(value.per_user_bytes_24h, "per_user_bytes_24h"),
    per_user_concurrent: integer(value.per_user_concurrent, "per_user_concurrent"),
    max_file_bytes: integer(value.max_file_bytes, "max_file_bytes"),
    global_files_per_minute: integer(value.global_files_per_minute, "global_files_per_minute"),
    max_album_items: integer(value.max_album_items, "max_album_items"),
    max_album_bytes: integer(value.max_album_bytes, "max_album_bytes"),
  };
  if (normalized.max_file_bytes > normalized.max_album_bytes)
    throw new Error("max_file_bytes cannot exceed max_album_bytes");
  db.prepare(
    "UPDATE helper_rate_limit_settings SET per_user_files_24h=?,per_user_bytes_24h=?," +
    "per_user_concurrent=?,max_file_bytes=?,global_files_per_minute=?,max_album_items=?," +
    "max_album_bytes=?,updated_at=? WHERE id=1",
  ).run(
    normalized.per_user_files_24h,
    normalized.per_user_bytes_24h,
    normalized.per_user_concurrent,
    normalized.max_file_bytes,
    normalized.global_files_per_minute,
    normalized.max_album_items,
    normalized.max_album_bytes,
    Date.now(),
  );
  return normalized;
}

function cleanupRateReservations(now: number): void {
  db.prepare(
    "UPDATE helper_rate_reservations SET status='released' WHERE status='reserved' AND expires_at<=?",
  ).run(now);
}

function reserveHelperRate(
  userId: string,
  batchId: string,
  jobs: Array<{ id: number; source_file_size?: number | null }>,
): { reservationKey: string; fileCount: number; totalBytes: number } {
  const settings = readRateLimitSettings();
  const fileCount = jobs.length;
  const totalBytes = jobs.reduce((sum, item) => sum + Math.max(0, Number(item.source_file_size || 0)), 0);
  if (fileCount > settings.max_album_items)
    throw new HelperRateLimitError(`单个媒体组最多 ${settings.max_album_items} 个文件`, 900);
  if (totalBytes > settings.max_album_bytes)
    throw new HelperRateLimitError("媒体组大小超过限制", 900);
  if (jobs.some((item) => Number(item.source_file_size || 0) > settings.max_file_bytes))
    throw new HelperRateLimitError("单文件大小超过限制", 900);
  const now = Date.now();
  const reservationKey = `${batchId}-${crypto.randomBytes(8).toString("hex")}`;
  const reserve = db.transaction(() => {
    cleanupRateReservations(now);
    const dayStart = now - 24 * 60 * 60 * 1000;
    const minuteStart = now - 60 * 1000;
    const usage = db.prepare(
      "SELECT COALESCE(SUM(file_count),0) AS files,COALESCE(SUM(total_bytes),0) AS bytes " +
      "FROM helper_rate_events WHERE user_id=? AND created_at>=?",
    ).get(userId, dayStart) as any;
    const activeUser = db.prepare(
      "SELECT COUNT(*) AS tasks FROM helper_rate_reservations WHERE user_id=? AND status='reserved'",
    ).get(userId) as any;
    const globalEvents = db.prepare(
      "SELECT COALESCE(SUM(file_count),0) AS files FROM helper_rate_events WHERE created_at>=?",
    ).get(minuteStart) as any;
    const globalReserved = db.prepare(
      "SELECT COALESCE(SUM(file_count),0) AS files FROM helper_rate_reservations WHERE status='reserved' AND reserved_at>=?",
    ).get(minuteStart) as any;
    if (Number(activeUser?.tasks || 0) >= settings.per_user_concurrent)
      throw new HelperRateLimitError("你的并发入库任务已达到上限", 60);
    if (Number(usage?.files || 0) + fileCount > settings.per_user_files_24h)
      throw new HelperRateLimitError("你在 24 小时内的文件数量已达到上限", 3600);
    if (Number(usage?.bytes || 0) + totalBytes > settings.per_user_bytes_24h)
      throw new HelperRateLimitError("你在 24 小时内的累计大小已达到上限", 3600);
    if (Number(globalEvents?.files || 0) + Number(globalReserved?.files || 0) + fileCount > settings.global_files_per_minute)
      throw new HelperRateLimitError("辅助 Bot 当前请求较多，请稍后再试", 60);
    db.prepare(
      "INSERT INTO helper_rate_reservations(reservation_key,batch_id,user_id,file_count,total_bytes,status,reserved_at,expires_at) " +
      "VALUES(?,?,?,?,?,'reserved',?,?)",
    ).run(reservationKey, batchId, userId, fileCount, totalBytes, now, now + 30 * 60 * 1000);
    db.prepare(
      `UPDATE jobs SET rate_reservation_key=?,status='routing',updated_at=? WHERE id IN (${jobs.map(() => "?").join(",")})`,
    ).run(reservationKey, now, ...jobs.map((item) => item.id));
  });
  reserve();
  return { reservationKey, fileCount, totalBytes };
}

function releaseHelperRate(reservationKey: string): void {
  db.prepare("UPDATE helper_rate_reservations SET status='released' WHERE reservation_key=? AND status='reserved'").run(reservationKey);
}

function completeHelperRateIfFinished(reservationKey: string): void {
  const reservation = db.prepare("SELECT * FROM helper_rate_reservations WHERE reservation_key=?").get(reservationKey) as any;
  if (!reservation || reservation.status !== "reserved") return;
  const pending = db.prepare(
    "SELECT COUNT(*) AS count FROM jobs WHERE rate_reservation_key=? AND status NOT IN ('completed','failed','deleted')",
  ).get(reservationKey) as any;
  if (Number(pending?.count || 0) > 0) return;
  const finish = db.transaction(() => {
    db.prepare("UPDATE helper_rate_reservations SET status='completed' WHERE reservation_key=? AND status='reserved'").run(reservationKey);
    db.prepare(
      "INSERT OR IGNORE INTO helper_rate_events(reservation_key,user_id,file_count,total_bytes,created_at) VALUES(?,?,?,?,?)",
    ).run(reservationKey, reservation.user_id, reservation.file_count, reservation.total_bytes, Date.now());
  });
  finish();
}

function batchStats(job: Job): { fileCount: number; totalBytes: number; completedBytes: number; completedFiles: number } {
  const rows = job.review_batch_id
    ? db.prepare("SELECT * FROM jobs WHERE review_batch_id=? ORDER BY id").all(job.review_batch_id) as Job[]
    : [job];
  return {
    fileCount: rows.length,
    totalBytes: rows.reduce((sum, row) => sum + Number(row.source_file_size || 0), 0),
    completedBytes: rows.filter((row) => row.status === "completed").reduce((sum, row) => sum + Number(row.source_file_size || 0), 0),
    completedFiles: rows.filter((row) => row.status === "completed").length,
  };
}

function formatBytes(value: number): string {
  const bytes = Math.max(0, Number(value || 0));
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let current = bytes;
  let index = -1;
  do {
    current /= 1024;
    index += 1;
  } while (current >= 1024 && index < units.length - 1);
  return `${current.toFixed(current >= 100 ? 0 : current >= 10 ? 1 : 2)} ${units[index]}`;
}

function sourceMediaInfo(message: any): { size: number; filename: string; mimeType: string } {
  const media = message?.document || message?.video || message?.audio;
  if (media) {
    return {
      size: Number(media.file_size || 0),
      filename: String(media.file_name || `telegram-${message.message_id}`),
      mimeType: String(media.mime_type || "application/octet-stream"),
    };
  }
  const photo = Array.isArray(message?.photo) ? message.photo[message.photo.length - 1] : undefined;
  if (photo) {
    return {
      size: Number(photo.file_size || 0),
      filename: `photo-${message.message_id}.jpg`,
      mimeType: "image/jpeg",
    };
  }
  return { size: 0, filename: `telegram-${message.message_id}`, mimeType: "application/octet-stream" };
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
          .map((account) => this.ensureRelay(account)),
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
      const paired = db.prepare("SELECT 1 FROM relay_pairs WHERE account_id=? AND telegram_user_id=?").get(
        account.config.id,
        String(account.me.id),
      );
      account.relayReady = Boolean(paired);
      await account.client.sendMessage(this.botUsername, {
        message: `/start ${account.config.id}`,
      });
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
      if (job.rate_reservation_key) completeHelperRateIfFinished(job.rate_reservation_key);
      try {
        await message.delete({ revoke: true });
      } catch {}
      const latest = db.prepare("SELECT * FROM jobs WHERE id=?").get(job.id) as Job;
      await this.updateBotStatus(latest, "✅ 已入库");
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

  private choiceMarkup(jobId: number): any {
    return {
      reply_markup: {
        inline_keyboard: [[
          { text: "公开可见", callback_data: `tbp:${jobId}` },
          { text: "仅自己可见", callback_data: `tbv:${jobId}` },
        ], [
          { text: "了解平台规则", callback_data: `tbrules:${jobId}` },
        ]],
      },
    };
  }

  private sourceContext(ctx: any): { chatId: string; userId: string; messageId: number } {
    return {
      chatId: String(ctx.chat.id),
      userId: String(ctx.from.id),
      messageId: Number(ctx.message.message_id),
    };
  }

  private async createChoiceJob(ctx: any): Promise<void> {
    const m: any = ctx.message;
    if (!ctx.from || !ctx.chat || ctx.chat.type !== "private") return;
    const binding = db
      .prepare("SELECT * FROM bindings WHERE telegram_user_id=? AND enabled=1")
      .get(String(ctx.from.id)) as any;
    if (!binding) {
      await ctx.reply("请先使用 /bind 邀请码绑定托管账号");
      return;
    }
    const account = this.accounts.get(String(binding.account_id));
    if (!account || account.state !== "authenticated" || !account.me) {
      await ctx.reply("目标账号当前未连接");
      return;
    }
    if (!account.relayReady) {
      await ctx.reply("目标账号正在与辅助 Bot 配对，请稍后重试");
      return;
    }
    const media = sourceMediaInfo(m);
    const existing = db.prepare(
      "SELECT * FROM jobs WHERE account_id=? AND source_chat_id=? AND source_message_id=?",
    ).get(String(binding.account_id), String(ctx.chat.id), Number(m.message_id)) as Job | undefined;
    if (existing) {
      await ctx.reply(`任务 #${existing.id} 已存在，当前状态：${existing.status}`);
      return;
    }
    const now = Date.now();
    const result = db.prepare(
      "INSERT INTO jobs(account_id,source_chat_id,submitter_telegram_user_id,source_message_id,status,created_at,updated_at,requested_visibility,review_status,source_file_size,source_filename,source_mime_type,file_count,choice_expires_at) " +
      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ).run(
      String(binding.account_id),
      String(ctx.chat.id),
      String(ctx.from.id),
      Number(m.message_id),
      "awaiting_choice",
      now,
      now,
      "private",
      "not_required",
      media.size,
      media.filename,
      media.mimeType,
      1,
      now + 15 * 60 * 1000,
    );
    const jobId = Number(result.lastInsertRowid);
    const status = await ctx.reply(
      "你上传的内容要怎么处理呢？",
      this.choiceMarkup(jobId),
    );
    db.prepare("UPDATE jobs SET status_message_id=?,updated_at=? WHERE id=?").run(status.message_id, Date.now(), jobId);
  }

  private async chooseVisibility(ctx: any, jobId: number, visibility: "public" | "private"): Promise<void> {
    const callerId = String(ctx.from?.id || "");
    const job = db.prepare("SELECT * FROM jobs WHERE id=?").get(jobId) as Job | undefined;
    if (!job) {
      await ctx.answerCbQuery("任务不存在", { show_alert: true });
      return;
    }
    if (String(job.submitter_telegram_user_id || job.source_chat_id) !== callerId) {
      await ctx.answerCbQuery("只有原上传者可以选择", { show_alert: true });
      return;
    }
    if (job.status !== "awaiting_choice") {
      await ctx.answerCbQuery("这个任务已经处理过了", { show_alert: true });
      return;
    }
    if (Number(job.choice_expires_at || 0) < Date.now()) {
      db.prepare("UPDATE jobs SET status='failed',error='choice expired',updated_at=? WHERE id=? AND status='awaiting_choice'").run(Date.now(), job.id);
      await ctx.answerCbQuery("选择已过期，请重新发送文件", { show_alert: true });
      return;
    }
    const batchId = job.review_batch_id || String(job.id);
    const batchJobs = db.prepare(
      "SELECT * FROM jobs WHERE (id=? OR review_batch_id=?) AND status='awaiting_choice' ORDER BY id",
    ).all(job.id, job.review_batch_id || "") as Job[];
    if (!batchJobs.length) {
      await ctx.answerCbQuery("任务已经处理过了", { show_alert: true });
      return;
    }
    const now = Date.now();
    db.prepare(
      `UPDATE jobs SET requested_visibility=?,review_status=?,review_reason=NULL,reviewed_at=NULL,reviewed_by=NULL,status='awaiting_choice',updated_at=? WHERE (id=? OR review_batch_id=?) AND status='awaiting_choice'`,
    ).run(visibility, visibility === "public" ? "pending" : "not_required", now, job.id, job.review_batch_id || "");
    const claimed = db.prepare(
      `UPDATE jobs SET status='rate_checking',updated_at=? WHERE id IN (${batchJobs.map(() => "?").join(",")}) AND status='awaiting_choice'`,
    ).run(now, ...batchJobs.map((item) => item.id));
    if (Number(claimed.changes || 0) !== batchJobs.length) {
      await ctx.answerCbQuery("这个任务已经处理过了", { show_alert: true });
      return;
    }
    const selected = db.prepare("SELECT * FROM jobs WHERE id=?").get(job.id) as Job;
    try {
      const reservation = reserveHelperRate(
        callerId,
        batchId,
        batchJobs.map((item) => ({ id: item.id, source_file_size: item.source_file_size })),
      );
      await this.routeChoiceBatch(selected, batchJobs.map((item) => item.id), reservation.reservationKey, ctx);
      await ctx.answerCbQuery(visibility === "public" ? "已选择公开，等待管理员审核" : "已选择仅自己可见");
    } catch (error) {
      const message = error instanceof HelperRateLimitError
        ? `${error.message}，约 ${error.retryAfterSeconds} 秒后可重试`
        : String(error);
      if (!(error instanceof HelperRateLimitError)) {
        db.prepare(
          `UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id IN (${batchJobs.map(() => "?").join(",")})`,
        ).run(message, Date.now(), ...batchJobs.map((item) => item.id));
      } else {
        db.prepare(
          `UPDATE jobs SET status='awaiting_choice',error=?,updated_at=? WHERE id IN (${batchJobs.map(() => "?").join(",")})`,
        ).run(message, Date.now(), ...batchJobs.map((item) => item.id));
      }
      if (selected.status_message_id) {
        if (error instanceof HelperRateLimitError && this.bot) {
          try {
            await this.bot.telegram.editMessageText(
              selected.source_chat_id,
              selected.status_message_id,
              undefined,
              `⚠️ ${message}\n请选择稍后重试，或重新发送文件。任务 #${selected.id}`,
              this.choiceMarkup(selected.id),
            );
          } catch {}
        } else {
          await this.updateBotStatus({ ...selected, error: message }, `⚠️ ${message}`);
        }
      }
      await ctx.answerCbQuery(message, { show_alert: true });
    }
  }

  private async routeChoiceBatch(firstJob: Job, jobIds: number[], reservationKey: string, ctx: any): Promise<void> {
    const target = this.get(firstJob.account_id);
    if (target.state !== "authenticated" || !target.me) throw new Error("目标账号当前未连接");
    if (!target.relayReady) throw new Error("目标账号尚未完成辅助 Bot 配对");
    const update = db.prepare("UPDATE jobs SET relay_message_id=?,status='delivered',updated_at=? WHERE id=?");
    try {
      for (const id of jobIds) {
        const job = db.prepare("SELECT * FROM jobs WHERE id=?").get(id) as Job;
        const copied = await this.bot!.telegram.copyMessage(
          String(target.me.id),
          job.source_chat_id,
          job.source_message_id,
          { caption: relayMarker(job.id) } as any,
        );
        update.run(Number(copied.message_id), Date.now(), id);
      }
      const latest = db.prepare("SELECT * FROM jobs WHERE id=?").get(firstJob.id) as Job;
      await this.updateBotStatus(latest, `📤 已投递给账号 ${firstJob.account_id}，等待入库`);
    } catch (error) {
      releaseHelperRate(reservationKey);
      for (const id of jobIds) this.failJob(id, error);
      throw error;
    }
  }

  private async showRules(ctx: any, jobId: number): Promise<void> {
    const job = db.prepare("SELECT * FROM jobs WHERE id=?").get(jobId) as Job | undefined;
    if (!job || String(job.submitter_telegram_user_id || job.source_chat_id) !== String(ctx.from?.id || "")) {
      await ctx.answerCbQuery("无权查看此任务", { show_alert: true });
      return;
    }
    await ctx.answerCbQuery();
    await ctx.reply(
      "平台规则：请勿上传违反法律法规或 Telegram 平台规则的内容，包括色情、侵权、恶意软件、滥用和其他违法内容。违规内容会被拒绝，账号可能被永久禁用。",
      this.choiceMarkup(jobId),
    );
  }

  private async refreshJobProgress(ctx: any, jobId: number): Promise<void> {
    const job = db.prepare("SELECT * FROM jobs WHERE id=?").get(jobId) as Job | undefined;
    if (!job || String(job.submitter_telegram_user_id || job.source_chat_id) !== String(ctx.from?.id || "")) {
      await ctx.answerCbQuery("无权查看此任务", { show_alert: true });
      return;
    }
    await this.updateBotStatus(job, job.status === "completed" ? "✅ 已入库" : `当前状态：${job.status}`);
    await ctx.answerCbQuery("状态已刷新");
  }

  async startBot(token: string): Promise<void> {
    if (this.bot) return;
    this.bot = new Telegraf(token);
    const bot = this.bot;
    const me = await bot.telegram.getMe();
    this.botUsername = me.username || undefined;
    bot.start(async (ctx) => {
      const telegramUserId = String(ctx.from?.id || "");
      const managed = [...this.accounts.values()].find(
        (account) => account.me && String(account.me.id) === telegramUserId,
      );
      if (managed && ctx.chat?.type === "private") {
        db.prepare(
          "INSERT INTO relay_pairs(account_id,telegram_user_id,paired_at) VALUES(?,?,?) " +
          "ON CONFLICT(account_id) DO UPDATE SET telegram_user_id=excluded.telegram_user_id,paired_at=excluded.paired_at",
        ).run(managed.config.id, telegramUserId, Date.now());
        managed.relayReady = true;
        return ctx.reply(`账号 ${managed.config.label || managed.config.id} 已完成辅助 Bot 配对。`);
      }
      return ctx.reply("已连接。请使用 /bind 邀请码绑定账号；绑定后发送 /web 获取网页登录码。");
    });
    bot.command("bind", async (ctx) => {
      if (!ctx.chat || ctx.chat.type !== "private" || !ctx.from)
        return ctx.reply("请在 Bot 私聊中完成绑定");
      const banned = db
        .prepare("SELECT reason FROM helper_bans WHERE telegram_user_id=?")
        .get(String(ctx.from.id)) as { reason?: string } | undefined;
      if (banned) return ctx.reply("该账号已被禁止使用上传功能，请联系管理员。");
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
      return ctx.reply(`绑定成功：${invite.account_id}\n发送 /web 获取网页登录码。`);
    });
    bot.command("web", async (ctx) => {
      if (!ctx.chat || ctx.chat.type !== "private" || !ctx.from)
        return ctx.reply("请在 Bot 私聊中获取网页登录码");
      const identity = boundWebLoginIdentity(db, ctx.from);
      if (!identity)
        return ctx.reply("你尚未绑定托管账号，请先使用 /bind 邀请码绑定");
      const displayName = [ctx.from.first_name, ctx.from.last_name]
        .filter(Boolean)
        .join(" ") || `Telegram ${ctx.from.id}`;
      const issued = issueWebLoginCode(db, {
        telegram_user_id: String(ctx.from.id),
        account_id: identity.account_id,
        username: ctx.from.username || null,
        display_name: displayName,
      });
      return ctx.reply(
        `SavedStream 网页登录码（10 分钟内有效，仅可使用一次）：\n\n${issued.code}\n\n登录后仍需管理员批准媒体库访问。`,
      );
    });
    bot.on("callback_query", async (ctx: any) => {
      const data = String(ctx.callbackQuery?.data || "");
      const match = /^(tbp|tbv|tbrules|tbr):(\d+)$/.exec(data);
      if (!match) return;
      const jobId = Number(match[2]);
      if (match[1] === "tbp") return this.chooseVisibility(ctx, jobId, "public");
      if (match[1] === "tbv") return this.chooseVisibility(ctx, jobId, "private");
      if (match[1] === "tbrules") return this.showRules(ctx, jobId);
      return this.refreshJobProgress(ctx, jobId);
    });
    bot.on("message", async (ctx) => {
      const m: any = ctx.message;
      const media = m.document || m.video || m.audio || m.photo;
      if (!ctx.from || !ctx.chat || ctx.chat.type !== "private") return;
      if (!media) {
        if (!String(m.text || "").startsWith("/")) await ctx.reply("仅支持私聊发送照片、视频、音频或文件；纯文本不会入库。");
        return;
      }
      if (m.media_group_id) return this.queueAlbum(ctx);
      return this.createChoiceJob(ctx);
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
    const target = this.get(String(binding.account_id));
    if (!target || target.state !== "authenticated" || !target.me) {
      await first.reply("目标账号当前未连接");
      return;
    }
    if (!target.relayReady) {
      await first.reply("目标账号正在与辅助 Bot 配对，请稍后重试");
      return;
    }
    const now = Date.now();
    const batchId = crypto.randomUUID();
    const jobIds: number[] = [];
    for (const ctx of contexts) {
      const media = sourceMediaInfo(ctx.message);
      const duplicate = db.prepare(
        "SELECT id FROM jobs WHERE account_id=? AND source_chat_id=? AND source_message_id=?",
      ).get(String(binding.account_id), String(first.chat.id), Number(ctx.message.message_id)) as any;
      if (duplicate) continue;
      const result = db
        .prepare(
          "INSERT INTO jobs(account_id,source_chat_id,submitter_telegram_user_id,source_message_id,status,created_at,updated_at,requested_visibility,review_status,review_batch_id,source_file_size,source_filename,source_mime_type,file_count,choice_expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        )
        .run(
          String(binding.account_id),
          String(first.chat.id),
          String(first.from.id),
          ctx.message.message_id,
          "awaiting_choice",
          now,
          now,
          "private",
          "not_required",
          batchId,
          media.size,
          media.filename,
          media.mimeType,
          contexts.length,
          now + 15 * 60 * 1000,
        );
      if (result.changes) {
        jobIds.push(Number(result.lastInsertRowid));
      }
    }
    if (!jobIds.length) {
      await first.reply("这个媒体组已经在入库队列中");
      return;
    }
    const totalBytes = jobIds.reduce((sum, id) => {
      const row = db.prepare("SELECT source_file_size FROM jobs WHERE id=?").get(id) as any;
      return sum + Number(row?.source_file_size || 0);
    }, 0);
    const status = await first.reply(
      `你上传的媒体组共 ${jobIds.length} 项（${formatBytes(totalBytes)}），要怎么处理呢？`,
      this.choiceMarkup(jobIds[0]),
    );
    db.prepare(
      `UPDATE jobs SET status_message_id=? WHERE id IN (${jobIds.map(() => "?").join(",")})`,
    ).run(status.message_id, ...jobIds);
    void target;
  }
  async updateBotStatus(job: Job, text: string): Promise<void> {
    if (!this.bot || !job.status_message_id) return;
    const current = (db.prepare("SELECT * FROM jobs WHERE id=?").get(job.id) as Job | undefined) || job;
    const stats = batchStats(current);
    const usage = current.submitter_telegram_user_id
      ? db.prepare(
        "SELECT COALESCE(SUM(file_count),0) AS files,COALESCE(SUM(total_bytes),0) AS bytes " +
        "FROM helper_rate_events WHERE user_id=?",
      ).get(current.submitter_telegram_user_id) as any
      : { files: 0, bytes: 0 };
    const choice = current.requested_visibility === "public" ? "✨ 公开可见" : "🔒 仅自己可见";
    const review = current.requested_visibility === "public"
      ? current.review_status === "approved"
        ? "审核通过，所有公开用户可见"
        : current.review_status === "rejected" || current.review_status === "revoked"
          ? "审核未通过，仅自己可见"
          : "等待管理员审核"
      : "仅自己可见";
    const suffix =
      `\n\n任务 #${current.id} · 文件 ${stats.fileCount} 个 · 本次 ${formatBytes(stats.totalBytes)}` +
      `\n云端累计 ${formatBytes(Number(usage?.bytes || 0))} · 文件数 ${Number(usage?.files || 0)}` +
      `\n当前选择：${choice}\n当前状态：${text}（${review}）`;
    try {
      await this.bot.telegram.editMessageText(
        job.source_chat_id,
        job.status_message_id,
        undefined,
        `${text}${suffix}`,
        { reply_markup: { inline_keyboard: [[{ text: "刷新审核进度", callback_data: `tbr:${current.id}` }]] } } as any,
      );
    } catch {}
  }
  failJob(id: number, e: unknown): void {
    const error = e instanceof Error ? e.message : String(e);
    const current = db.prepare("SELECT * FROM jobs WHERE id=?").get(id) as Job | undefined;
    if (current?.rate_reservation_key) releaseHelperRate(current.rate_reservation_key);
    db.prepare(
      "UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?",
    ).run(error, Date.now(), id);
    if (current) void this.updateBotStatus({ ...current, status: "failed", error }, `❌ 入库失败：${error}`);
  }

  private async deleteTelegramMessage(account: Account, peer: unknown, messageId: number): Promise<void> {
    if (!messageId) return;
    const messages: any[] = await account.client.getMessages(peer as any, { ids: messageId });
    const message = messages[0];
    if (message) await message.delete({ revoke: true });
  }

  private clearMediaCache(accountId: string, messageId: number): void {
    const directory = path.join(MEDIA_CACHE, accountId);
    if (!fs.existsSync(directory)) return;
    for (const entry of fs.readdirSync(directory)) {
      if (!entry.startsWith(`${messageId}-`)) continue;
      try {
        fs.unlinkSync(path.join(directory, entry));
      } catch {}
    }
  }

  async deleteJob(
    id: number,
    reason: string | null = null,
    deletedBy = "admin",
  ): Promise<Job> {
    const job = db.prepare("SELECT * FROM jobs WHERE id=?").get(id) as Job | undefined;
    if (!job) throw new Error("Ingest job not found");
    if (job.status === "deleted") return job;
    const account = this.get(job.account_id);
    if (account.state !== "authenticated" || !account.me) {
      throw new Error("Target account is offline");
    }
    const cleanReason = String(reason || "违规内容").slice(0, 1000);
    // Remove the final Saved Message first.  Missing Telegram messages are
    // treated as already deleted so retries remain idempotent.
    if (job.saved_message_id) {
      try {
        await this.deleteTelegramMessage(account, "me", Number(job.saved_message_id));
      } catch (error) {
        const text = String(error);
        if (!/message|not found|invalid/i.test(text)) throw error;
      }
    }
    if (job.relay_message_id && this.botUsername) {
      try {
        await this.deleteTelegramMessage(account, this.botUsername, Number(job.relay_message_id));
      } catch (error) {
        const text = String(error);
        if (!/message|not found|invalid/i.test(text)) throw error;
      }
    }
    // Best effort removal of the original media message from the Helper Bot
    // chat.  Telegram may refuse deletion of an incoming user message; the
    // Saved Message and userbot relay are still removed above.
    if (this.bot) {
      try {
        await this.bot.telegram.deleteMessage(job.source_chat_id, job.source_message_id);
      } catch {}
    }
    this.clearMediaCache(job.account_id, Number(job.saved_message_id || 0));
    if (job.rate_reservation_key) releaseHelperRate(job.rate_reservation_key);
    db.prepare(
      "UPDATE jobs SET status='deleted',error=?,review_status='rejected',review_reason=?,reviewed_at=?,reviewed_by=?,updated_at=? WHERE id=?",
    ).run(cleanReason, cleanReason, Date.now(), deletedBy || "admin", Date.now(), id);
    const latest = db.prepare("SELECT * FROM jobs WHERE id=?").get(id) as Job;
    await this.updateBotStatus(latest, `🗑️ 已删除：${cleanReason}`);
    // Keep only the identifiers needed for audit/deduplication and the status
    // message.  File metadata and Telegram message references are no longer
    // useful after a policy deletion and must not remain as a second media
    // record in the TeleBox database.
    db.prepare(
      "UPDATE jobs SET source_file_size=0,source_filename=NULL,source_mime_type=NULL,"
      + "relay_message_id=NULL,saved_message_id=NULL,rate_reservation_key=NULL WHERE id=?",
    ).run(id);
    return db.prepare("SELECT * FROM jobs WHERE id=?").get(id) as Job;
  }

  async deleteMedia(
    accountId: string,
    messageId: number,
    reason: string | null = null,
    deletedBy = "admin",
  ): Promise<Record<string, unknown>> {
    const account = this.get(accountId);
    const jobs = db
      .prepare("SELECT * FROM jobs WHERE account_id=? AND saved_message_id=? ORDER BY id")
      .all(accountId, Number(messageId)) as Job[];
    if (jobs.length) {
      for (const job of jobs) await this.deleteJob(job.id, reason, deletedBy);
      return { ok: true, deleted_jobs: jobs.map((job) => job.id) };
    }
    if (account.state !== "authenticated" || !account.me) throw new Error("Target account is offline");
    try {
      await this.deleteTelegramMessage(account, "me", Number(messageId));
    } catch (error) {
      const text = String(error);
      if (!/message|not found|invalid/i.test(text)) throw error;
    }
    this.clearMediaCache(accountId, Number(messageId));
    return { ok: true, deleted_message_id: Number(messageId) };
  }

  async setBindingStatus(
    telegramUserId: string,
    enabled: boolean,
    banned: boolean,
    reason: string | null = null,
  ): Promise<Record<string, unknown>> {
    const userId = String(telegramUserId);
    if (banned) {
      db.prepare(
        "INSERT INTO helper_bans(telegram_user_id,reason,banned_at,banned_by) VALUES(?,?,?,?) " +
        "ON CONFLICT(telegram_user_id) DO UPDATE SET reason=excluded.reason,banned_at=excluded.banned_at,banned_by=excluded.banned_by",
      ).run(userId, reason ? String(reason).slice(0, 1000) : "管理员禁用", Date.now(), "admin");
      db.prepare("UPDATE bindings SET enabled=0 WHERE telegram_user_id=?").run(userId);
      const active = db
        .prepare(
          "SELECT id FROM jobs WHERE submitter_telegram_user_id=? AND status IN ('awaiting_choice','rate_checking','routing','delivered','importing','retry_wait') ORDER BY id",
        )
        .all(userId) as Array<{ id: number }>;
      for (const job of active) {
        try {
          await this.deleteJob(Number(job.id), reason || "提交者账号已封禁", "admin");
        } catch (error) {
          this.failJob(Number(job.id), error);
        }
      }
    } else {
      db.prepare("DELETE FROM helper_bans WHERE telegram_user_id=?").run(userId);
      db.prepare("UPDATE bindings SET enabled=? WHERE telegram_user_id=?").run(enabled ? 1 : 0, userId);
    }
    const row = db
      .prepare(
        "SELECT b.telegram_user_id,b.account_id,b.created_at,b.enabled,CASE WHEN h.telegram_user_id IS NULL THEN 0 ELSE 1 END AS banned " +
        "FROM bindings b LEFT JOIN helper_bans h ON h.telegram_user_id=b.telegram_user_id WHERE b.telegram_user_id=?",
      )
      .get(userId) as Record<string, unknown> | undefined;
    return row || { telegram_user_id: userId, enabled: false, banned };
  }

  async updateJobReview(
    id: number,
    decision: "approved" | "rejected" | "revoked" | "deleted",
    reason: string | null,
    reviewedBy: string,
  ): Promise<Job> {
    if (decision === "deleted") return this.deleteJob(id, reason, reviewedBy);
    const job = db.prepare("SELECT * FROM jobs WHERE id=?").get(id) as Job | undefined;
    if (!job) throw new Error("Ingest job not found");
    const status = decision;
    db.prepare(
      "UPDATE jobs SET requested_visibility='public',review_status=?,review_reason=?,reviewed_at=?,reviewed_by=?,updated_at=? WHERE id=?",
    ).run(status, reason ? String(reason).slice(0, 1000) : null, Date.now(), reviewedBy || "admin", Date.now(), id);
    const latest = db.prepare("SELECT * FROM jobs WHERE id=?").get(id) as Job;
    const reviewLabel = decision === "approved"
      ? "✅ 审核通过"
      : decision === "rejected"
        ? "❌ 审核未通过"
        : "⚠️ 公开状态已撤销";
    await this.updateBotStatus(latest, `${reviewLabel}${reason ? `：${String(reason).slice(0, 200)}` : ""}`);
    return latest;
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

async function syncMedia(accountId: string, u: URL): Promise<any> {
  const a = manager.get(accountId);
  const limit = Math.max(1, Math.min(500, Number(u.searchParams.get("limit") || 200)));
  const mode = u.searchParams.get("mode") === "full" ? "full" : "incremental";
  const cursor = Number(u.searchParams.get("cursor") || 0);
  const afterId = Number(u.searchParams.get("after_id") || 0);
  const options: any = { limit };
  if (mode === "full") {
    if (cursor) options.offsetId = cursor;
  } else {
    if (afterId) options.minId = afterId;
    options.reverse = true;
  }
  const messages: any[] = await a.client.getMessages("me", options);
  const items = messages
    .map(serializeMessage)
    .filter(Boolean)
    .sort((left: any, right: any) => Number(left.id) - Number(right.id));
  const rawIds = messages.map((message: any) => Number(message?.id || 0)).filter(Boolean);
  const nextCursor = syncPaginationCursor(mode, rawIds, limit);
  return {
    items,
    message_ids: rawIds,
    next_cursor: nextCursor,
    has_more: Boolean(nextCursor),
    mode,
  };
}

async function uploadMedia(
  req: http.IncomingMessage,
  res: http.ServerResponse,
  accountId: string,
): Promise<void> {
  const account = manager.get(accountId);
  if (account.state !== "authenticated" || !account.me)
    return send(res, 409, { detail: "account is not authenticated" });
  const total = Number(req.headers["content-length"] || 0);
  if (!Number.isSafeInteger(total) || total <= 0)
    return send(res, 411, { detail: "content-length is required" });
  const filename = decodeBase64UrlHeader(String(req.headers["x-upload-filename"] || "")) || `upload-${Date.now()}`;
  const mimeType = String(req.headers["x-upload-mime"] || "application/octet-stream").slice(0, 200);
  const caption = decodeBase64UrlHeader(String(req.headers["x-upload-caption"] || ""));
  const spool = path.join(UPLOAD_SPOOL, `${crypto.randomBytes(16).toString("hex")}-${path.basename(filename)}`);
  try {
    await pipeline(req, fs.createWriteStream(spool, { mode: 0o600 }));
    const actualSize = fs.statSync(spool).size;
    if (!uploadBodyMatchesLength(actualSize, total)) throw new Error("uploaded body length does not match content-length");
    const message: any = await account.client.sendFile("me", {
      file: new CustomFile(filename, actualSize, spool),
      caption,
      forceDocument: false,
      supportsStreaming: mimeType.startsWith("video/"),
      attributes: mimeType ? undefined : [],
    } as any);
    const item = serializeMessage(message);
    if (!item) throw new Error("Telegram returned a message without supported media");
    return send(res, 201, item);
  } finally {
    try {
      fs.unlinkSync(spool);
    } catch {}
  }
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
  if ((!data || data.length === 0) && String(msg.file?.mimeType || "").startsWith("image/")) {
    data = (await a.client.downloadMedia(msg, {})) as Buffer;
  }
  if (!data || data.length === 0) return send(res, 404, { detail: "thumbnail unavailable" });
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
    if (u.pathname === "/v1/helper-bot/rate-limit" && req.method === "GET")
      return send(res, 200, readRateLimitSettings());
    if (u.pathname === "/v1/helper-bot/rate-limit" && req.method === "PUT") {
      const p = await body(req);
      try {
        return send(res, 200, writeRateLimitSettings({
          per_user_files_24h: Number(p.per_user_files_24h),
          per_user_bytes_24h: Number(p.per_user_bytes_24h),
          per_user_concurrent: Number(p.per_user_concurrent),
          max_file_bytes: Number(p.max_file_bytes),
          global_files_per_minute: Number(p.global_files_per_minute),
          max_album_items: Number(p.max_album_items),
          max_album_bytes: Number(p.max_album_bytes),
        }));
      } catch (error) {
        return send(res, 422, { detail: error instanceof Error ? error.message : String(error) });
      }
    }
    if (u.pathname === "/v1/web-login/consume" && req.method === "POST") {
      const p = await body(req);
      const code = String(p.code || "").trim();
      if (!code) return send(res, 422, { detail: "web login code required" });
      const identity = consumeWebLoginCode(db, code);
      if (!identity)
        return send(res, 401, { detail: "web login code is invalid, expired, or already used" });
      return send(res, 200, identity);
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
            "SELECT b.telegram_user_id,b.account_id,b.created_at,b.enabled,CASE WHEN h.telegram_user_id IS NULL THEN 0 ELSE 1 END AS banned " +
            "FROM bindings b LEFT JOIN helper_bans h ON h.telegram_user_id=b.telegram_user_id ORDER BY b.created_at DESC",
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
    const bindingStatus = u.pathname.match(/^\/v1\/bindings\/([^/]+)$/);
    if (bindingStatus && req.method === "PUT") {
      const p = await body(req);
      const result = await manager.setBindingStatus(
        decodeURIComponent(bindingStatus[1]),
        Boolean(p.enabled),
        Boolean(p.banned),
        p.reason == null ? null : String(p.reason),
      );
      return send(res, 200, result);
    }
    if (u.pathname === "/v1/ingest/jobs" && req.method === "GET") {
      const statusFilter = String(u.searchParams.get("status") || "").trim();
      const allowedStatuses = new Set([
        "awaiting_choice",
        "rate_checking",
        "received",
        "routing",
        "delivered",
        "importing",
        "completed",
        "failed",
        "retry_wait",
        "deleted",
      ]);
      if (statusFilter && !allowedStatuses.has(statusFilter))
        return send(res, 422, { detail: "invalid ingest job status" });
      const limit = Math.max(
        1,
        Math.min(500, Number(u.searchParams.get("limit") || 100) || 100),
      );
      const updatedAfter = Math.max(
        0,
        Number(u.searchParams.get("updated_after") || 0) || 0,
      );
      const afterJobId = Math.max(
        0,
        Number(u.searchParams.get("after_job_id") || 0) || 0,
      );
      const incremental = Boolean(
        statusFilter || updatedAfter || afterJobId || u.searchParams.has("limit"),
      );
      if (!incremental) {
        return send(res, 200, {
          items: db.prepare("SELECT * FROM jobs ORDER BY id DESC LIMIT 100").all(),
          has_more: false,
        });
      }
      const clauses: string[] = [];
      const params: Array<string | number> = [];
      if (statusFilter) {
        clauses.push("status=?");
        params.push(statusFilter);
      }
      clauses.push("(updated_at>? OR (updated_at=? AND id>?))");
      params.push(updatedAfter, updatedAfter, afterJobId);
      const rows = db
        .prepare(
          `SELECT * FROM jobs WHERE ${clauses.join(" AND ")} ORDER BY updated_at ASC,id ASC LIMIT ?`,
        )
        .all(...params, limit + 1) as Job[];
      const hasMore = rows.length > limit;
      const items = rows.slice(0, limit);
      const last = items[items.length - 1];
      return send(res, 200, {
        items,
        has_more: hasMore,
        next_updated_after: last?.updated_at || updatedAfter,
        next_job_id: last?.id || afterJobId,
      });
    }
    const retry = u.pathname.match(/^\/v1\/ingest\/jobs\/(\d+)\/retry$/);
    if (retry && req.method === "POST") {
      await manager.retryJob(Number(retry[1]));
      return send(res, 200, { ok: true });
    }
    const deleteJob = u.pathname.match(/^\/v1\/ingest\/jobs\/(\d+)$/);
    if (deleteJob && req.method === "DELETE") {
      const p = await body(req);
      const job = await manager.deleteJob(
        Number(deleteJob[1]),
        p.reason == null ? null : String(p.reason),
        String(p.deleted_by || "admin"),
      );
      return send(res, 200, job);
    }
    const review = u.pathname.match(/^\/v1\/ingest\/jobs\/(\d+)\/review$/);
    if (review && req.method === "PATCH") {
      const p = await body(req);
      const decision = String(p.decision || "");
      if (!["approved", "rejected", "revoked", "deleted"].includes(decision))
        return send(res, 422, { detail: "invalid review decision" });
      const job = await manager.updateJobReview(
        Number(review[1]),
        decision as "approved" | "rejected" | "revoked" | "deleted",
        p.reason == null ? null : String(p.reason),
        String(p.reviewed_by || "admin"),
      );
      return send(res, 200, job);
    }
    const mediaList = u.pathname.match(/^\/v1\/accounts\/([^/]+)\/media$/);
    if (mediaList && req.method === "GET")
      return send(res, 200, await listMedia(mediaList[1], u));
    const mediaSync = u.pathname.match(/^\/v1\/accounts\/([^/]+)\/media\/sync$/);
    if (mediaSync && req.method === "GET")
      return send(res, 200, await syncMedia(mediaSync[1], u));
    const mediaUpload = u.pathname.match(/^\/v1\/accounts\/([^/]+)\/upload$/);
    if (mediaUpload && req.method === "POST")
      return await uploadMedia(req, res, mediaUpload[1]);
    const mediaDetail = u.pathname.match(
      /^\/v1\/accounts\/([^/]+)\/media\/(\d+)$/,
    );
    if (mediaDetail && req.method === "DELETE") {
      const p = await body(req);
      return send(
        res,
        200,
        await manager.deleteMedia(
          mediaDetail[1],
          Number(mediaDetail[2]),
          p.reason == null ? null : String(p.reason),
          String(p.deleted_by || "admin"),
        ),
      );
    }
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
