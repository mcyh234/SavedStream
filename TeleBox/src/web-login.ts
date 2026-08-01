import crypto from "node:crypto";

export const WEB_LOGIN_TTL_MS = 10 * 60 * 1000;

export interface WebLoginIdentity {
  telegram_user_id: string;
  account_id: string;
  username: string | null;
  display_name: string;
}

export interface WebLoginRecord extends WebLoginIdentity {
  expires_at: number;
}

export interface TelegramWebUser {
  id: number;
  username?: string;
  first_name: string;
  last_name?: string;
}

export function boundWebLoginIdentity(db: any, user: TelegramWebUser): WebLoginIdentity | null {
  const binding = db
    .prepare("SELECT account_id FROM bindings WHERE telegram_user_id=? AND enabled=1")
    .get(String(user.id)) as { account_id: string } | undefined;
  if (!binding) return null;
  return {
    telegram_user_id: String(user.id),
    account_id: binding.account_id,
    username: user["username"] || null,
    display_name: [user.first_name, user.last_name].filter(Boolean).join(" ") || `Telegram ${user.id}`,
  };
}

export function hashWebLoginCode(code: string): string {
  return crypto.createHash("sha256").update(code, "utf8").digest("hex");
}

export function issueWebLoginCode(
  db: any,
  identity: WebLoginIdentity,
  now = Date.now(),
): { code: string; expires_at: number } {
  const code = crypto.randomBytes(24).toString("base64url");
  const expires_at = now + WEB_LOGIN_TTL_MS;
  db.prepare(
    "INSERT INTO web_login_codes(code_hash,telegram_user_id,account_id,username,display_name,expires_at,used_at) VALUES(?,?,?,?,?,?,NULL)",
  ).run(
    hashWebLoginCode(code),
    identity.telegram_user_id,
    identity.account_id,
    identity.username,
    identity.display_name,
    expires_at,
  );
  return { code, expires_at };
}

export function consumeWebLoginCode(
  db: any,
  code: string,
  now = Date.now(),
): WebLoginRecord | null {
  const codeHash = hashWebLoginCode(code.trim());
  return db.transaction(() => {
    const row = db
      .prepare(
        "SELECT telegram_user_id,account_id,username,display_name,expires_at FROM web_login_codes WHERE code_hash=? AND used_at IS NULL AND expires_at>?",
      )
      .get(codeHash, now) as WebLoginRecord | undefined;
    if (!row) return null;
    const updated = db
      .prepare(
        "UPDATE web_login_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL",
      )
      .run(now, codeHash);
    return updated.changes === 1 ? row : null;
  })();
}
