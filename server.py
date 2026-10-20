from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("NEWTONITE_DB", ROOT / "newtonite.db"))
LOCK = threading.RLock()
STATUSES = {"open", "in_progress", "blocked", "resolved"}
PRIORITIES = {"low", "normal", "high", "urgent"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try: return super().__exit__(exc_type, exc, tb)
        finally: self.close()


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, team TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', team TEXT NOT NULL,
          status TEXT NOT NULL, priority TEXT NOT NULL, owner_id TEXT REFERENCES users(id), created_by TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, next_action TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS activities(id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL REFERENCES items(id),
          actor_id TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY, response TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS team_memberships(user_id TEXT NOT NULL REFERENCES users(id), team TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('viewer','member','manager','admin')), PRIMARY KEY(user_id,team));
        CREATE INDEX IF NOT EXISTS idx_items_team_updated ON items(team, updated_at DESC, id);
        CREATE INDEX IF NOT EXISTS idx_items_team_status ON items(team, status, priority);
        CREATE INDEX IF NOT EXISTS idx_activity_item_id ON activities(item_id, id DESC);
        """)
        columns={r[1] for r in db.execute("PRAGMA table_info(items)")}
        if "next_action" not in columns: db.execute("ALTER TABLE items ADD COLUMN next_action TEXT NOT NULL DEFAULT ''")
        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            db.executemany("INSERT INTO users VALUES(?,?,?,?)", [
                ("u1", "Alex Morgan", "admin", "Operations"), ("u2", "Sam Rivera", "member", "Operations"),
                ("u3", "Jordan Lee", "member", "Engineering"), ("u4", "Taylor Chen", "manager", "Engineering")])
        if db.execute("SELECT COUNT(*) FROM team_memberships").fetchone()[0] == 0:
            db.executemany("INSERT INTO team_memberships VALUES(?,?,?)", [
                ("u1","Operations","admin"),("u1","Engineering","admin"),
                ("u2","Operations","member"),("u2","Engineering","viewer"),
                ("u3","Engineering","member"),("u4","Engineering","manager")])
        if db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
            t = now()
            seeds = [
              ("w-1001", "Investigate delayed customer refunds", "Several refunds from yesterday are still pending settlement. Confirm provider status and update support.", "Operations", "in_progress", "urgent", "u2", "u1", t,t,1,"Provider support to confirm settlement; update support"),
              ("w-1002", "Review elevated API error rate", "Error rate increased after the latest deployment; identify affected endpoints and decide whether to roll back.", "Engineering", "blocked", "high", "u3", "u4", t,t,1,"On-call to review traces and recommend rollback"),
              ("w-1003", "Approve vendor access renewal", "Compliance review is complete. Manager approval is needed before renewal.", "Operations", "open", "normal", None, "u1", t,t,1,"Taylor Chen to approve renewal"),
              ("w-1004", "Reconcile duplicate invoice reports", "Finance reported two possible duplicates. Compare ledger entries and document the outcome.", "Operations", "open", "high", None, "u2", t,t,1,"Sam Rivera to compare ledger entries"),
              ("w-1005", "Rotate staging credentials", "Rotate the shared staging credentials and notify the engineering team.", "Engineering", "resolved", "low", "u4", "u3", t,t,1,"Completed")]
            db.executemany("INSERT INTO items(id,title,description,team,status,priority,owner_id,created_by,created_at,updated_at,version,next_action) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", seeds)
            db.executemany("INSERT INTO activities(item_id,actor_id,kind,detail,created_at) VALUES(?,?,?,?,?)", [
              (s[0],s[7],"created","Work item created",t) for s in seeds])
        else:
            for ident, action in {"w-1001":"Provider support to confirm settlement; update support","w-1002":"On-call to review traces and recommend rollback","w-1003":"Taylor Chen to approve renewal","w-1004":"Sam Rivera to compare ledger entries","w-1005":"Completed"}.items():
                db.execute("UPDATE items SET next_action=? WHERE id=? AND next_action=''",(action,ident))


class ApiError(Exception):
    def __init__(self, status, message): self.status, self.message = status, message


def item_dict(row):
    x = dict(row)
    x["owner"] = x.pop("owner_name", None)
    x["creator"] = x.pop("creator_name", None)
    return x


class Handler(BaseHTTPRequestHandler):
    server_version = "NewtoniteOps/1.0"

    def send_json(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(raw)

    def body(self):
        try:
            length=int(self.headers.get("Content-Length", "0"))
            if length>1_000_000: raise ApiError(413,"Request body is too large")
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError): raise ApiError(400, "Request body must be valid JSON")

    def user(self, db):
        user_id = self.headers.get("X-User-Id", "u1")
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user: raise ApiError(401, "Choose a valid demo user")
        return user

    def team_role(self, db, user, team):
        if user["role"] == "admin": return "admin"
        row=db.execute("SELECT role FROM team_memberships WHERE user_id=? AND team=?",(user["id"],team)).fetchone()
        return row["role"] if row else None

    def authorize_team(self, db, user, team, write=False):
        role=self.team_role(db,user,team)
        if role is None: raise ApiError(404, "Work item not found")
        if write and role not in ("admin","member","manager"): raise ApiError(403, "Your team role is read-only")
        return role

    def do_GET(self):
        try:
            if urlparse(self.path).path == "/health": return self.send_json(200,{"status":"ok"})
            if not self.path.startswith("/api/"):
                name = urlparse(self.path).path.lstrip("/") or "index.html"
                path = (ROOT / name).resolve()
                if path.parent != ROOT.resolve() or not path.is_file():
                    self.send_error(404); return
                raw=path.read_bytes(); mime="text/html; charset=utf-8" if path.suffix==".html" else "text/css; charset=utf-8" if path.suffix==".css" else "text/javascript; charset=utf-8"
                self.send_response(200); self.send_header("Content-Type",mime); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            with connect() as db:
                user = self.user(db); p = urlparse(self.path); q = parse_qs(p.query)
                if p.path == "/api/me":
                    memberships=[dict(r) for r in db.execute("SELECT team,role FROM team_memberships WHERE user_id=? ORDER BY team",(user["id"],))]
                    teams=[r["team"] for r in memberships]
                    selected=q.get("team",[""])[0]
                    if user["role"]=="admin": selected=selected if selected in ("","Operations","Engineering") else ""
                    elif selected not in teams: selected=teams[0] if teams else ""
                    role="admin" if user["role"]=="admin" else next((r["role"] for r in memberships if r["team"]==selected),"viewer")
                    current_user={**dict(user),"role":role,"team":selected}
                    users=[]
                    for r in db.execute("SELECT * FROM users ORDER BY name"):
                        member_rows=[dict(m) for m in db.execute("SELECT team,role FROM team_memberships WHERE user_id=?",(r["id"],))]
                        effective=self.team_role(db,r,selected) if selected else r["role"]
                        if effective: users.append({**dict(r),"role":effective,"memberships":member_rows})
                    return self.send_json(200, {"user": current_user, "users": users, "teams": memberships, "selectedTeam": selected})
                if p.path == "/api/items":
                    where, args = ["1=1"], []
                    if user["role"] != "admin":
                        memberships=[r[0] for r in db.execute("SELECT team FROM team_memberships WHERE user_id=?",(user["id"],))]
                        if not memberships: return self.send_json(200,{"items":[],"total":0,"limit":30,"offset":0,"counts":{}})
                        where.append("i.team IN ("+",".join("?" for _ in memberships)+")"); args.extend(memberships)
                    for field, allowed in (("status", STATUSES), ("priority", PRIORITIES)):
                        val = q.get(field, [""])[0]
                        if val:
                            if val not in allowed: raise ApiError(400, f"Invalid {field}")
                            where.append(f"i.{field}=?"); args.append(val)
                    team = q.get("team", [""])[0]
                    if team:
                        if self.team_role(db,user,team) is None: raise ApiError(404, "Team not found")
                        where.append("i.team=?"); args.append(team)
                    owner = q.get("owner", [""])[0]
                    if owner:
                        where.append("i.owner_id=?"); args.append(owner)
                    search = q.get("q", [""])[0].strip()
                    if search:
                        where.append("(i.title LIKE ? OR i.description LIKE ? OR i.next_action LIKE ? OR i.id LIKE ?)"); args.extend([f"%{search}%"]*4)
                    try:
                        limit = min(max(int(q.get("limit", ["30"])[0]), 1), 100)
                        offset = max(int(q.get("offset", ["0"])[0]), 0)
                    except ValueError: raise ApiError(400, "Invalid pagination")
                    filt = " AND ".join(where)
                    rows = db.execute(f"SELECT i.*,u.name owner_name,c.name creator_name FROM items i LEFT JOIN users u ON u.id=i.owner_id LEFT JOIN users c ON c.id=i.created_by WHERE {filt} ORDER BY CASE i.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, i.updated_at DESC, i.id LIMIT ? OFFSET ?", (*args,limit,offset)).fetchall()
                    total = db.execute(f"SELECT COUNT(*) FROM items i WHERE {filt}", args).fetchone()[0]
                    counts = {r[0]:r[1] for r in db.execute(f"SELECT status,COUNT(*) FROM items i WHERE {filt} GROUP BY status",args)}
                    return self.send_json(200, {"items":[item_dict(r) for r in rows],"total":total,"limit":limit,"offset":offset,"counts":counts})
                if p.path.startswith("/api/items/") and p.path.endswith("/activity"):
                    item_id = p.path.split("/")[3]; item = db.execute("SELECT * FROM items WHERE id=?",(item_id,)).fetchone()
                    if not item: raise ApiError(404,"Work item not found")
                    self.authorize_team(db,user,item["team"])
                    activities = db.execute("SELECT a.*,u.name actor FROM activities a LEFT JOIN users u ON u.id=a.actor_id WHERE item_id=? ORDER BY a.id DESC LIMIT 100",(item_id,)).fetchall()
                    return self.send_json(200,{"activities":[dict(a) for a in activities]})
                raise ApiError(404,"Route not found")
        except ApiError as e: self.send_json(e.status,{"error":e.message})
        except Exception as e: self.send_json(500,{"error":"Unexpected server error"})

    def do_POST(self):
        try:
            with LOCK, connect() as db:
                db.execute("BEGIN IMMEDIATE")
                user=self.user(db); data=self.body(); p=urlparse(self.path)
                if p.path == "/api/items":
                    title=str(data.get("title","")).strip(); desc=str(data.get("description","")).strip(); next_action=str(data.get("next_action","")).strip(); team=str(data.get("team",self.headers.get("X-Team-Id",user["team"])))
                    priority=data.get("priority","normal")
                    self.authorize_team(db,user,team,True)
                    if not title or len(title)>160: raise ApiError(400,"Title is required (max 160 characters)")
                    if len(desc)>5000 or len(next_action)>500: raise ApiError(400,"Description or next action is too long")
                    if priority not in PRIORITIES: raise ApiError(400,"Invalid priority")
                    ident="w-"+uuid.uuid4().hex[:8]; stamp=now()
                    db.execute("INSERT INTO items(id,title,description,team,status,priority,created_by,created_at,updated_at,next_action) VALUES(?,?,?,?,?,?,?,?,?,?)",(ident,title,desc,team,"open",priority,user["id"],stamp,stamp,next_action))
                    db.execute("INSERT INTO activities(item_id,actor_id,kind,detail,created_at) VALUES(?,?,?,?,?)",(ident,user["id"],"created","Work item created"+(f" · Next: {next_action}" if next_action else ""),stamp))
                    row=db.execute("SELECT i.*,u.name owner_name,c.name creator_name FROM items i LEFT JOIN users u ON u.id=i.owner_id LEFT JOIN users c ON c.id=i.created_by WHERE i.id=?",(ident,)).fetchone()
                    db.commit(); return self.send_json(201,{"item":item_dict(row)})
                if p.path.startswith("/api/items/"):
                    item_id=p.path.split("/")[3]; item=db.execute("SELECT * FROM items WHERE id=?",(item_id,)).fetchone()
                    if not item: raise ApiError(404,"Work item not found")
                    team_role=self.authorize_team(db,user,item["team"],True)
                    action=p.path.rsplit("/",1)[-1]
                    key=self.headers.get("Idempotency-Key","").strip()
                    key=f"{user['id']}:{item_id}:{action}:{key}" if key else ""
                    if key:
                        old=db.execute("SELECT response FROM idempotency WHERE key=?",(key,)).fetchone()
                        if old: db.commit(); return self.send_json(200,json.loads(old[0]))
                    expected=data.get("version")
                    if expected is None or int(expected)!=item["version"]: raise ApiError(409,"This work item changed. Refresh and retry your update.")
                    stamp=now(); changes=[]; fields={}
                    if action=="claim":
                        if item["owner_id"] and item["owner_id"]!=user["id"]: raise ApiError(409,"This item is already owned by someone else")
                        if item["owner_id"]==user["id"]:
                            row=db.execute("SELECT i.*,u.name owner_name,c.name creator_name FROM items i LEFT JOIN users u ON u.id=i.owner_id LEFT JOIN users c ON c.id=i.created_by WHERE i.id=?",(item_id,)).fetchone()
                            response={"item":item_dict(row)}
                            if key: db.execute("INSERT INTO idempotency VALUES(?,?,?)",(key,json.dumps(response),stamp))
                            db.commit(); return self.send_json(200,response)
                        fields["owner_id"]=user["id"]
                        if item["status"]=="open": fields["status"]="in_progress"
                        changes.append(("claimed",f"{user['name']} took ownership"))
                    elif action=="assign":
                        if team_role not in ("manager","admin"): raise ApiError(403,"Only managers and admins can reassign work")
                        owner_id=data.get("owner_id") or None
                        if owner_id and not db.execute("SELECT 1 FROM team_memberships WHERE user_id=? AND team=? AND role IN ('member','manager','admin')",(owner_id,item["team"])).fetchone(): raise ApiError(400,"Assignee must be an eligible member of this team")
                        fields["owner_id"]=owner_id; changes.append(("assigned",f"Assigned to {db.execute('SELECT name FROM users WHERE id=?',(owner_id,)).fetchone()[0] if owner_id else 'nobody'}"))
                    elif action=="update":
                        title=str(data.get("title",item["title"])).strip(); desc=str(data.get("description",item["description"])).strip(); next_action=str(data.get("next_action",item["next_action"])).strip(); status=data.get("status",item["status"]); priority=data.get("priority",item["priority"])
                        if not title or len(title)>160: raise ApiError(400,"Title is required (max 160 characters)")
                        if len(desc)>5000 or len(next_action)>500: raise ApiError(400,"Description or next action is too long")
                        if status not in STATUSES or priority not in PRIORITIES: raise ApiError(400,"Invalid status or priority")
                        if status != item["status"]: changes.append(("status",f"Status changed from {item['status'].replace('_',' ')} to {status.replace('_',' ')}"))
                        if priority != item["priority"]: changes.append(("priority",f"Priority changed from {item['priority']} to {priority}"))
                        if next_action != item["next_action"]: changes.append(("next_action",f"Next action updated: {next_action or 'none'}"))
                        fields.update(title=title,description=desc,next_action=next_action,status=status,priority=priority)
                        comment=str(data.get("comment","")).strip()
                        if comment: changes.append(("comment",comment[:2000]))
                    else: raise ApiError(404,"Route not found")
                    setters=", ".join(f"{k}=?" for k in fields)
                    cur=db.execute(f"UPDATE items SET {setters},version=version+1,updated_at=? WHERE id=? AND version=?",(*fields.values(),stamp,item_id,item["version"]))
                    if cur.rowcount!=1: raise ApiError(409,"This work item changed. Refresh and retry your update.")
                    for kind,detail in changes: db.execute("INSERT INTO activities(item_id,actor_id,kind,detail,created_at) VALUES(?,?,?,?,?)",(item_id,user["id"],kind,detail,stamp))
                    row=db.execute("SELECT i.*,u.name owner_name,c.name creator_name FROM items i LEFT JOIN users u ON u.id=i.owner_id LEFT JOIN users c ON c.id=i.created_by WHERE i.id=?",(item_id,)).fetchone()
                    response={"item":item_dict(row)}
                    if key: db.execute("INSERT INTO idempotency VALUES(?,?,?)",(key,json.dumps(response),stamp))
                    db.commit(); return self.send_json(200,response)
        except ApiError as e:
            try: db.rollback()
            except Exception: pass
            self.send_json(e.status,{"error":e.message})
        except (ValueError, TypeError):
            try: db.rollback()
            except Exception: pass
            self.send_json(400,{"error":"Invalid request data"})
        except Exception:
            try: db.rollback()
            except Exception: pass
            self.send_json(500,{"error":"Unexpected server error"})

    def do_OPTIONS(self): self.send_response(204); self.end_headers()
    def log_message(self, *_): pass


if __name__ == "__main__":
    init_db()
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    print(f"Newtonite Ops running at http://{host}:{port}")
    ThreadingHTTPServer((host,port),Handler).serve_forever()
