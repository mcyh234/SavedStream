import assert from "node:assert/strict";
import test from "node:test";
import Database from "better-sqlite3";
import {
  boundWebLoginIdentity,
  consumeWebLoginCode,
  hashWebLoginCode,
  issueWebLoginCode,
  WEB_LOGIN_TTL_MS,
} from "./web-login";

function database(): Database.Database {
  const db = new Database(":memory:");
  db.exec(`CREATE TABLE web_login_codes(
    code_hash TEXT PRIMARY KEY,
    telegram_user_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    username TEXT,
    display_name TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER
  ); CREATE TABLE bindings(
    telegram_user_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
  )`);
  return db;
}

test("web login codes are hashed, single-use, and preserve identity", () => {
  const db = database();
  const issued = issueWebLoginCode(
    db,
    {
      telegram_user_id: "12345",
      account_id: "family",
      username: "viewer",
      display_name: "Test User",
    },
    1_000,
  );
  assert.equal(issued.expires_at, 1_000 + WEB_LOGIN_TTL_MS);
  const stored = db.prepare("SELECT code_hash FROM web_login_codes").get() as {
    code_hash: string;
  };
  assert.notEqual(stored.code_hash, issued.code);
  assert.equal(stored.code_hash, hashWebLoginCode(issued.code));
  assert.deepEqual(consumeWebLoginCode(db, issued.code, 2_000), {
    telegram_user_id: "12345",
    account_id: "family",
    username: "viewer",
    display_name: "Test User",
    expires_at: 1_000 + WEB_LOGIN_TTL_MS,
  });
  assert.equal(consumeWebLoginCode(db, issued.code, 2_001), null);
  db.close();
});

test("expired and unknown web login codes are rejected", () => {
  const db = database();
  const issued = issueWebLoginCode(
    db,
    {
      telegram_user_id: "7",
      account_id: "default",
      username: null,
      display_name: "User 7",
    },
    10,
  );
  assert.equal(consumeWebLoginCode(db, issued.code, issued.expires_at), null);
  assert.equal(consumeWebLoginCode(db, "unknown", 20), null);
  db.close();
});

test("only enabled bound users can request a web identity", () => {
  const db = database();
  const user = { id: 42, username: "alice", first_name: "Alice", last_name: "Test" };
  assert.equal(boundWebLoginIdentity(db, user), null);
  db.prepare("INSERT INTO bindings VALUES(?,?,?,?)").run("42", "family", 1, 1);
  assert.deepEqual(boundWebLoginIdentity(db, user), {
    telegram_user_id: "42",
    account_id: "family",
    username: "alice",
    display_name: "Alice Test",
  });
  db.prepare("UPDATE bindings SET enabled=0 WHERE telegram_user_id='42'").run();
  assert.equal(boundWebLoginIdentity(db, user), null);
  db.close();
});
