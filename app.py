#!/usr/bin/env python3
"""
M1 Share — file sharing over your Wi-Fi / router (LAN).

One window, one port (default 8000). Receivers on the same network open
http://<this-pc-ip>:8000/ in any browser, type their name, enter the login
code (if the sender set one) and download the ticked files — one by one,
never as a ZIP. The sender controls everything from the "M1 Share" window
or from the sender panel at /sender.
"""
import argparse
import copy
import json
import mimetypes
import os
import secrets
import shutil
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_NAME = "M1 Share"
APP_VERSION = "1.0.0"
DEFAULT_PORT = 8000
MAX_PORT_TRIES = 11
SESSION_TTL = 12 * 3600          # 12 hours
UNLOCK_MAX_FAILS = 10            # per window …
UNLOCK_WINDOW = 300              # … per 5 minutes, per IP
UNLOCK_MAX_FAILS_GLOBAL = 30     # … and a global ceiling per window, all IPs combined
JOIN_MAX_PER_WINDOW = 15         # /api/join calls per IP per window
MAX_SESSIONS = 200               # hard cap on live receiver sessions
MAX_LIVE = 200                   # hard cap on live-users entries
DOWNLOAD_KEEPALIVE = 120         # socket idle timeout (s) — long downloads are unaffected (data flows)
TREE_CACHE_TTL = 3.0             # seconds
MAX_EVENTS = 300
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "M1Share")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")

CHAR_DIGITS = "0123456789"
CHAR_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"   # no I or O — unambiguous
CHAR_SPECIAL = "!@#$%&*?"
CHAR_SETS = {"digits": CHAR_DIGITS, "letters": CHAR_LETTERS, "special": CHAR_SPECIAL}
CHAR_NAMES = {"digits": "Digit", "letters": "Alphabet", "special": "Special Character"}


def resource_path(rel):
    """Path to a bundled resource, both when run as a script and frozen by PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def norm_path(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def real_base(p):
    """Realpath with exactly one trailing separator, safe for prefix checks."""
    r = os.path.realpath(p)
    return r if r.endswith(os.sep) else r + os.sep


def real_contained(full, base):
    """True if `full`'s real path is `base` itself or inside it (symlinks resolved)."""
    rp = os.path.realpath(full)
    rb = real_base(base)
    return rp == os.path.realpath(base) or rp.startswith(rb)


def detect_lan_ip():
    """Best-effort LAN IP of this machine (the one the router gave it)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect(("8.8.8.8", 80))          # no traffic actually sent
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


# ---------------------------------------------------------------- state ----
class ShareState:
    def __init__(self):
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()   # GUI thread + HTTP threads can hit _save concurrently
        self.sharing_on = True
        self.download_mode = "single"       # 'single' | 'all'
        self.code_on = True
        self.code_length = "4"              # '3' | '4' | '5' | 'custom'
        self.charsets = {"digits": True, "letters": False, "special": False}
        self.code = ""                      # current effective code (server-side only)
        self.roots = []                     # [{id, path, name}]
        self.selection = {}                  # norm_path -> bool (overrides; default True)
        self.blocked = set()                # lower-cased receiver names
        # runtime only (never persisted):
        self.sessions = {}                   # token -> {name, ip, created, last_seen}
        self.live = {}                       # lower name -> {name, ip, joined, last_seen, doing}
        self.events = []                     # log lines
        self.tree_cache = {}                 # key -> {time, value} — kept small, few entries
        self._sel_version = 0
        self.unlock_fails = {}               # ip -> [timestamps] for the login code
        self.join_fails = {}                 # ip -> [timestamps] for /api/join
        self.unlock_fails_global = []        # timestamps — global ceiling regardless of source IP
        self.ip = detect_lan_ip()
        self.port = DEFAULT_PORT
        self._load()
        if not self.code:
            self.code = self._generate_code()
            self._save()

    # ---------- persistence ----------
    def _load(self):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.sharing_on = bool(d.get("sharing_on", True))
            self.download_mode = d.get("download_mode", "single")
            self.code_on = bool(d.get("code_on", True))
            self.code_length = str(d.get("code_length", "4"))
            cs = d.get("charsets", {})
            self.charsets = {k: bool(cs.get(k, k == "digits")) for k in CHAR_SETS}
            if not any(self.charsets.values()):
                self.charsets["digits"] = True
            self.code = str(d.get("code", ""))
            self.roots = [r for r in d.get("roots", []) if isinstance(r, dict) and os.path.isdir(r.get("path", ""))]
            for r in self.roots:
                r.setdefault("name", os.path.basename(r["path"].rstrip("\\/")) or r["path"])
            # drop selection entries that no longer live under a current root —
            # they can never be looked up again, only re-saved forever
            self.selection = {k: bool(v) for k, v in d.get("selection", {}).items()
                              if any(self._key_in_root(k, r) for r in self.roots)}
            self.blocked = {str(n).strip().lower() for n in d.get("blocked", []) if str(n).strip()}
        except FileNotFoundError:
            pass
        except Exception as e:
            # keep the unreadable file — a fresh start must not destroy the old config/code
            try:
                shutil.copy2(STATE_PATH, STATE_PATH + ".bad")
                self.log(f"Saved a copy of the unreadable state file as state.json.bad")
            except OSError:
                pass
            self.log(f"Could not read saved state: {e} — starting fresh")

    def _save(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            d = {
                "sharing_on": self.sharing_on,
                "download_mode": self.download_mode,
                "code_on": self.code_on,
                "code_length": self.code_length,
                "charsets": self.charsets,
                "code": self.code,
                "roots": self.roots,
                "selection": self.selection,
                "blocked": sorted(self.blocked),
            }
            with self.save_lock:
                tmp = STATE_PATH + f".{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(d, f)
                os.replace(tmp, STATE_PATH)
        except Exception as e:
            self.log(f"Could not save state: {e}")

    # ---------- events / log ----------
    def log(self, msg):
        msg = str(msg).replace("\r", " ").replace("\n", " ").replace("\t", " ")
        stamp = datetime.now().strftime("%H:%M:%S")
        self.events.append(f"{stamp}  {msg}")
        if len(self.events) > MAX_EVENTS:
            del self.events[: len(self.events) - MAX_EVENTS]

    # ---------- code ----------
    def _generate_code(self):
        sets = [CHAR_SETS[k] for k in ("digits", "letters", "special") if self.charsets.get(k)]
        pool = "".join(sets) or CHAR_DIGITS
        if self.code_length == "custom":
            length = 4
        else:
            try:
                length = max(3, min(8, int(self.code_length)))
            except ValueError:
                length = 4
        src = secrets.SystemRandom()
        chars = [sets[i][src.randrange(len(sets[i]))] for i in range(min(length, len(sets)))]
        while len(chars) < length:
            chars.append(pool[src.randrange(len(pool))])
        # shuffle so the one-per-set slots aren't always at the front
        for i in range(len(chars) - 1, 0, -1):
            j = src.randrange(i + 1)
            chars[i], chars[j] = chars[j], chars[i]
        return "".join(chars)

    def set_code_length(self, mode):
        if mode in ("3", "4", "5", "custom"):
            self.code_length = mode
            if mode != "custom":
                self.code = self._generate_code()
            self.tree_cache.clear()
            self._save()
            return self.code
        return self.code

    def set_charset(self, key, on):
        if key not in CHAR_SETS:
            return
        self.charsets[key] = bool(on)
        if not any(self.charsets.values()):
            self.charsets[key] = True   # keep at least one
            return
        if self.code_length != "custom":
            self.code = self._generate_code()
        else:
            self.code = self._filter_custom(self.code)
        self._save()

    def _filter_custom(self, value):
        pool = "".join(CHAR_SETS[k] for k in ("digits", "letters", "special") if self.charsets.get(k)) or CHAR_DIGITS
        return "".join(c for c in (value or "").upper() if c in pool)[:8]

    def set_custom_code(self, value):
        self.code = self._filter_custom(value)
        self._save()
        return self.code

    def generate_code(self):
        if self.code_length == "custom":
            current = self.code_length
            self.code_length = str(max(3, min(8, len(self.code)) or 4))
            self.code = self._generate_code()
            self.code_length = current
        else:
            self.code = self._generate_code()
        self._save()
        return self.code

    def set_code_on(self, on):
        self.code_on = bool(on)
        if self.code_on:
            # turning the code on relocks everyone holding a session —
            # receivers must enter the code again before files open
            self.sessions.clear()
            self.log("Login code ON — everyone was sent back to the login gate")
        self._save()

    # ---------- selection ----------
    @staticmethod
    def _key_in_root(key, root):
        k = norm_path(key)
        base = norm_path(root["path"])
        if base.endswith(os.sep):
            return k == base or k.startswith(base)
        return k == base or k.startswith(base + os.sep)

    def is_selected(self, abs_path):
        return self.selection.get(norm_path(abs_path), True)

    def set_selected(self, node_id, on):
        target, _root = self.resolve_node(node_id)
        if target is None:
            return False
        for p in self._walk_paths(target):
            self.selection[p] = bool(on)
        self._invalidate_trees()
        self._save()
        return True

    def bulk_selection(self, op, folder_id=None, value=0.0, unit="GB", above=True):
        """op: 'all' | 'none' | 'invert' | 'size' applied inside `folder_id` (or all roots)."""
        targets = []
        if folder_id is None:
            targets = [norm_path(r["path"]) for r in self.roots]
        else:
            target, root = self.resolve_node(folder_id)
            if target is not None:
                targets = [norm_path(target)]
        threshold = (value / 1024) if unit == "MB" else value  # normalise everything to GB
        for base in targets:
            for p in self._walk_paths(base):
                if op in ("all", "none"):
                    self.selection[p] = op == "all"
                elif op == "invert":
                    self.selection[p] = not self.selection.get(p, True)
                elif op == "size":
                    try:
                        size_gb = os.stat(p).st_size / (1024 ** 3)
                    except OSError:
                        continue
                    self.selection[p] = (size_gb >= threshold) if above else (size_gb <= threshold)
        self._invalidate_trees()
        self._save()

    def _walk_paths(self, base):
        """Yield normalised absolute paths of every file under `base` (or the file itself)."""
        if os.path.isfile(base):
            yield base
            return
        for dirpath, dirnames, filenames in os.walk(base):
            # prune symlinked dirs that escape the shared tree
            dirnames[:] = [d for d in dirnames
                           if real_contained(os.path.join(dirpath, d), base)]
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if real_contained(p, base):
                    yield norm_path(p)

    # ---------- roots ----------
    def add_root(self, path):
        path = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
        if not os.path.isdir(path):
            return None, "That folder does not exist."
        np_ = norm_path(path)
        for r in self.roots:
            if norm_path(r["path"]) == np_:
                return r, "Already added — showing it now."
        root = {"id": secrets.token_hex(4), "path": path,
                "name": os.path.basename(path.rstrip("\\/")) or path}
        self.roots.append(root)
        self._invalidate_trees()
        self._save()
        self.log(f"Added folder “{root['name']}”")
        return root, None

    def remove_root(self, root_id):
        for i, r in enumerate(self.roots):
            if r["id"] == root_id:
                gone = self.roots.pop(i)
                # drop this root's selection entries too — gone forever, no reason to keep them
                self.selection = {k: v for k, v in self.selection.items() if not self._key_in_root(k, gone)}
                self._invalidate_trees()
                self._save()
                self.log(f"Removed folder “{gone['name']}”")
                return True
        return False

    # ---------- node id resolution ----------
    def _resolve(self, node_id):
        node_id = str(node_id or "")
        for r in self.roots:
            if node_id == r["id"]:
                return r, ""
            prefix = r["id"] + "/"
            if node_id.startswith(prefix):
                rel = node_id[len(prefix):].replace("/", os.sep)
                return r, rel
        return None, None

    def resolve_node(self, node_id):
        node_id = str(node_id or "")
        if ":" in node_id:
            # blocks NTFS ADS tricks (file:Zone.Identifier) — node ids never
            # legitimately contain a colon on Windows
            return None, None
        root, rel = self._resolve(node_id)
        if root is None:
            return None, None
        full = norm_path(os.path.join(root["path"], rel)) if rel else norm_path(root["path"])
        base = norm_path(root["path"])
        if base.endswith(os.sep):                      # drive root (C:\)
            inside = full == base or full.startswith(base)
        else:
            inside = full == base or full.startswith(base + os.sep)
        if not inside:
            return None, None
        # symlink / junction escape: the real path must stay inside the root too
        if not real_contained(full, root["path"]):
            return None, None
        return full, root

    def node_id_for(self, abs_path, root):
        if norm_path(abs_path) == norm_path(root["path"]):
            return root["id"]
        rel = os.path.relpath(abs_path, root["path"]).replace(os.sep, "/")
        return root["id"] + "/" + rel

    def _invalidate_trees(self):
        self._sel_version += 1
        self.tree_cache.clear()

    # ---------- tree ----------
    @staticmethod
    def _mod(ts):
        months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        d = datetime.fromtimestamp(ts)
        return f"{d.day} {months[d.month]} {d.year}", int(d.strftime("%Y%m%d"))

    def build_trees(self, for_receiver=False, force=False):
        key = (tuple(r["id"] for r in self.roots), for_receiver, self._sel_version)
        now = time.time()
        if not force:
            cached = self.tree_cache.get(key)
            if cached and now - cached["time"] < TREE_CACHE_TTL:
                return copy.deepcopy(cached["value"])
        trees = []
        total_files = selected_files = 0
        total_bytes = selected_bytes = 0
        full_top_folders = 0
        for r in self.roots:
            node = self._walk_node(r["path"], r, for_receiver, depth=0,
                                    for_receiver_depth=0)
            if node is not None:
                trees.append(node)
                total_files += node["_files"]
                selected_files += node["_selected_files"]
                total_bytes += node["_bytes"]
                selected_bytes += node["_selected_bytes"]
                # count direct sub-folders that are fully selected
                for child in node["children"]:
                    if child["type"] == "folder" and child["_selected_files"] > 0 and child["_selected_files"] == child["_files"]:
                        full_top_folders += 1
        for t in trees:
            self._strip_private(t)
        value = {
            "roots": trees,
            "sel_version": self._sel_version,
            "summary": {
                "total_files": total_files,
                "selected_files": selected_files,
                "total_bytes": total_bytes,
                "selected_bytes": selected_bytes,
                "full_folders": full_top_folders,
            },
        }
        self.tree_cache[key] = {"time": now, "value": copy.deepcopy(value)}
        # keep the cache tiny — a handful of entries, oldest evicted first
        if len(self.tree_cache) > 4:
            oldest = min(self.tree_cache, key=lambda k: self.tree_cache[k]["time"])
            if oldest != key:
                del self.tree_cache[oldest]
        return value

    def _walk_node(self, path, root, for_receiver, depth, for_receiver_depth):
        # symlink / junction pointing outside the shared root — skip it entirely
        if not real_contained(path, root["path"]):
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        mod, mod_key = self._mod(st.st_mtime)
        name = os.path.basename(path.rstrip("\\/")) or path
        node = {"id": self.node_id_for(path, root), "name": name,
                "mod": mod, "modKey": mod_key,
                "_files": 0, "_selected_files": 0, "_bytes": 0, "_selected_bytes": 0}
        if os.path.isfile(path):
            sel = self.is_selected(path)
            node.update({"type": "file", "size": st.st_size, "selected": sel,
                         "_files": 1, "_selected_files": 1 if sel else 0,
                         "_bytes": st.st_size, "_selected_bytes": st.st_size if sel else 0})
            if for_receiver and not sel:
                return None
            return node
        node["type"] = "folder"
        children = []
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            entries = []
        for entry in entries:
            child = self._walk_node(entry.path, root, for_receiver, depth + 1, for_receiver_depth + 1)
            if child is not None:
                children.append(child)
        node["children"] = children
        for c in children:
            node["_files"] += c["_files"]
            node["_selected_files"] += c["_selected_files"]
            node["_bytes"] += c["_bytes"]
            node["_selected_bytes"] += c["_selected_bytes"]
        node["size"] = node["_selected_bytes"] if for_receiver else node["_bytes"]
        if for_receiver and node["_selected_files"] == 0:
            return None
        return node

    def _strip_private(self, node):
        for k in ("_files", "_selected_files", "_bytes", "_selected_bytes"):
            node.pop(k, None)
        for c in node.get("children", []):
            self._strip_private(c)

    # ---------- sessions & live users ----------
    def purge_expired_sessions(self):
        cutoff = time.time() - SESSION_TTL
        for token in [t for t, s in self.sessions.items() if s["created"] < cutoff]:
            del self.sessions[token]

    def new_session(self, name, ip):
        self.purge_expired_sessions()
        if len(self.sessions) >= MAX_SESSIONS:
            # drop the oldest sessions to make room — never reject a legitimate unlock
            for token in sorted(self.sessions, key=lambda t: self.sessions[t]["created"])[: len(self.sessions) - MAX_SESSIONS + 1]:
                del self.sessions[token]
        token = secrets.token_urlsafe(18)
        self.sessions[token] = {"name": name.strip()[:40], "ip": ip,
                                "created": time.time(), "last_seen": time.time()}
        return token

    def session_status(self, token):
        """(session, None) when valid, else (None, 'blocked' | 'gone')."""
        s = self.sessions.get(str(token or ""))
        if not s:
            return None, "gone"
        if time.time() - s["created"] > SESSION_TTL:
            del self.sessions[token]
            return None, "gone"
        if s["name"].strip().lower() in self.blocked:
            return None, "blocked"
        s["last_seen"] = time.time()
        return s, None

    def drop_sessions_of(self, name_lower):
        for token in [t for t, s in self.sessions.items() if s["name"].strip().lower() == name_lower]:
            del self.sessions[token]

    def join(self, name, ip):
        name = name.strip()[:40]
        key = name.lower()
        blocked = key in self.blocked
        now = time.time()
        # rate limit joins per IP — an unauthenticated flood of names would grow the live table forever
        recent = [t for t in self.join_fails.get(ip, []) if now - t < UNLOCK_WINDOW]
        if len(recent) >= JOIN_MAX_PER_WINDOW:
            return {"ok": False, "rate_limited": True, "sharing_on": self.sharing_on,
                    "blocked": blocked, "code_required": self.code_on}
        recent.append(now)
        self.join_fails[ip] = recent
        if len(self.live) >= MAX_LIVE:
            # drop stale entries first, then the least-recently-seen
            for k, u in list(self.live.items()):
                if now - u["last_seen"] > 120:
                    del self.live[k]
            while len(self.live) >= MAX_LIVE:
                oldest = min(self.live, key=lambda k: self.live[k]["last_seen"])
                del self.live[oldest]
        if key in self.live:
            u = self.live[key]
            u["ip"] = ip
            u["last_seen"] = now
            u["doing"] = u.get("doing") or "Viewing files"
            u["joined"] = now
        else:
            self.live[key] = {"name": name, "ip": ip, "joined": now,
                              "last_seen": now, "doing": "Viewing files"}
        self.log(f"“{name}” saved their name ({ip})")
        return {"ok": True, "blocked": blocked, "sharing_on": self.sharing_on,
                "code_required": self.code_on}

    def heartbeat(self, token, doing=None):
        s, reason = self.session_status(token)
        if s is None:
            return {"ok": False,
                    "blocked": reason == "blocked",
                    "session_closed": reason == "gone",
                    "sharing_on": self.sharing_on}
        if doing is not None:
            doing = str(doing)[:120]
        key = s["name"].strip().lower()
        u = self.live.get(key)
        if u:
            u["last_seen"] = time.time()
            if doing:
                u["doing"] = doing
        blocked = key in self.blocked
        return {"ok": True, "blocked": blocked, "sharing_on": self.sharing_on,
                "name": s["name"], "download_mode": self.download_mode}

    def live_list(self):
        now = time.time()
        out = []
        for key, u in sorted(self.live.items(), key=lambda kv: kv[1]["joined"], reverse=True):
            out.append({
                "name": u["name"], "ip": u["ip"],
                "doing": u.get("doing") or "Viewing files",
                "joined": u["joined"],
                "last_seen": u["last_seen"],
                "stale": now - u["last_seen"] > 60,
                "access": key not in self.blocked,
            })
        return out

    def set_blocked(self, name, block):
        key = str(name or "").strip().lower()
        if not key:
            return False
        if block:
            self.blocked.add(key)
            self.drop_sessions_of(key)
            self.log(f"Blocked “{name}”")
        else:
            self.blocked.discard(key)
            self.log(f"Unblocked “{name}”")
        self._save()
        return True

    # ---------- share link ----------
    def share_url(self):
        return f"http://{self.ip}:{self.port}/"

    def bootstrap(self):
        return {"app": APP_NAME, "version": APP_VERSION,
                "sharing_on": self.sharing_on,
                "code_required": self.code_on,
                "download_mode": self.download_mode,
                "address": f"{self.ip}:{self.port}"}


STATE = ShareState()


def is_loopback(ip):
    return ip in ("127.0.0.1", "::1", "localhost") or (ip or "").startswith("127.")


# ---------------------------------------------------------------- server ---
def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "M1Share/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- helpers ----------
    def _send(self, status, body, content_type="application/json; charset=utf-8",
              extra_headers=None, cache=False):
        data = body if isinstance(body, (bytes, bytearray)) else (
            _json_bytes(body) if content_type.startswith("application/json") else str(body).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, obj, status=200):
        self._send(status, obj)

    def send_error_json(self, status, message, **extra):
        payload = {"error": message}
        payload.update(extra)
        self.send_json(payload, status=status)

    def client_ip(self):
        return (self.client_address[0] if self.client_address else "?")

    def _read_json(self):
        """Returns (body, error) — error is (status, message) when the body is unusable."""
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return {}, (400, "bad_content_length")
        if length > 1_000_000:
            # can't resync the keep-alive socket after an oversize body — drop it
            self.close_connection = True
            return {}, (413, "too_large")
        if length == 0:
            return {}, None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")), None
        except Exception:
            return {}, (400, "bad_json")

    def _session(self, allow_query=False):
        token = self.headers.get("X-Session", "")
        if not token and allow_query:
            qs = parse_qs(urlparse(self.path).query)
            token = (qs.get("t") or [""])[0]
        return STATE.session_status(token)

    def _sender_allowed(self):
        return getattr(self.server, "allow_remote_sender", False) or is_loopback(self.client_ip())

    def log_message(self, fmt, *args):  # silence default stderr logging; GUI shows events
        pass

    # ---------- routing ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                return self._file("web/receiver.html", "text/html; charset=utf-8")
            if path == "/sender" or path.startswith("/api/sender/"):
                if not self._sender_allowed():
                    return self.send_error_json(403, "local_only", local_only=True)
            if path == "/sender":
                return self._file("web/sender.html", "text/html; charset=utf-8")
            if path.startswith("/assets/"):
                name = os.path.basename(path[len("/assets/"):])
                return self._asset(name)
            if path == "/api/bootstrap":
                with STATE.lock:
                    return self.send_json(STATE.bootstrap())
            if path == "/api/tree":
                with STATE.lock:
                    s, reason = self._session()
                    if s is None:
                        return self.send_error_json(403, "blocked", blocked=True,
                                                    session_closed=(reason == "gone"))
                    if not STATE.sharing_on:
                        return self.send_error_json(403, "sharing_off", sharing_on=False)
                    data = STATE.build_trees(for_receiver=True)
                    data["sharing_on"] = True
                    data["download_mode"] = STATE.download_mode
                    return self.send_json(data)
            if path == "/api/sender/state":
                with STATE.lock:
                    return self.send_json(self._sender_state())
            if path == "/api/download":
                return self._download(parsed)
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self.send_error_json(404, "not_found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            STATE.log(f"Unhandled server error: {e}")
            try:
                self.send_error_json(500, "server_error")
            except Exception:
                pass

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            # this server does not decode chunked bodies — refuse them before any
            # state change so the keep-alive socket can't desync either
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                return self.send_error_json(411, "length_required")
            # form-encoded posts (a cross-origin simple request) carry no
            # Content-Type: application/json — reject them outright
            ct = self.headers.get("Content-Type", "")
            if "application/json" not in ct:
                return self.send_error_json(415, "expected_json")
            body, err = self._read_json()
            if err:
                return self.send_error_json(err[0], err[1])
            if path.startswith("/api/sender/") and not self._sender_allowed():
                return self.send_error_json(403, "local_only", local_only=True)
            with STATE.lock:
                if path == "/api/join":
                    name = str(body.get("name", "")).strip()[:40]
                    if not name:
                        return self.send_error_json(400, "name_required")
                    return self.send_json(STATE.join(name, self.client_ip()))
                if path == "/api/open":
                    return self._open_or_unlock(body, require_code=False)
                if path == "/api/unlock":
                    return self._open_or_unlock(body, require_code=True)
                if path == "/api/heartbeat":
                    token = self.headers.get("X-Session", "")
                    s, reason = self._session()
                    if s is None:
                        return self.send_json({"ok": False,
                                               "blocked": reason == "blocked",
                                               "session_closed": reason == "gone",
                                               "sharing_on": STATE.sharing_on})
                    return self.send_json(STATE.heartbeat(token, body.get("doing")))
                if path.startswith("/api/sender/"):
                    return self._sender_post(path, body)
            return self.send_error_json(404, "not_found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            STATE.log(f"Unhandled server error: {e}")
            try:
                self.send_error_json(500, "server_error")
            except Exception:
                pass

    # ---------- shared handlers ----------
    def _file(self, rel, content_type):
        full = resource_path(rel)
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self.send_error_json(500, f"missing_resource: {rel}")
        self._send(200, data, content_type)

    def _asset(self, name):
        allowed = {"m1share-logo-horizontal.jpg", "m1share-logo-stacked.png", "m1share.ico"}
        if name not in allowed:
            return self.send_error_json(404, "not_found")
        full = resource_path(os.path.join("assets", name))
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self.send_error_json(404, "not_found")
        ctype = {"jpg": "image/jpeg", "png": "image/png", "ico": "image/x-icon"}[name.rsplit(".", 1)[1]]
        self._send(200, data, ctype, cache=True)

    def _open_or_unlock(self, body, require_code):
        name = str(body.get("name", "")).strip()[:40]
        if not name:
            return self.send_error_json(400, "name_required")
        ip = self.client_ip()
        key = name.lower()
        if key in STATE.blocked:
            return self.send_json({"status": "blocked"})
        if not STATE.sharing_on:
            return self.send_json({"status": "sharing_off"})
        if STATE.code_on:
            entered = str(body.get("code", "")).strip().upper()
            now = time.time()
            with STATE.lock:
                gfails = [t for t in STATE.unlock_fails_global if now - t < UNLOCK_WINDOW]
                STATE.unlock_fails_global = gfails
                if len(gfails) >= UNLOCK_MAX_FAILS_GLOBAL:
                    STATE.log("Too many wrong codes across devices — unlocking paused for 5 minutes")
                    return self.send_json({"status": "locked", "retry_after": UNLOCK_WINDOW})
                fails = [t for t in STATE.unlock_fails.get(ip, []) if now - t < UNLOCK_WINDOW]
                if len(fails) >= UNLOCK_MAX_FAILS:
                    STATE.unlock_fails[ip] = fails
                    return self.send_json({"status": "locked", "retry_after": UNLOCK_WINDOW})
                if not STATE.code:
                    return self.send_json({"status": "no_code"})
                if entered != STATE.code.upper():
                    fails.append(now)
                    STATE.unlock_fails[ip] = fails
                    STATE.unlock_fails_global.append(now)
                    if entered:
                        STATE.log(f"Wrong code entered by “{name}” ({ip})")
                    return self.send_json({"status": "wrong"})
                STATE.unlock_fails.pop(ip, None)
        token = STATE.new_session(name, ip)
        STATE.log(f"“{name}” opened the share ({ip})")
        return self.send_json({"status": "ok", "token": token})

    def _download(self, parsed):
        qs = parse_qs(parsed.query)
        node_id = (qs.get("id") or [""])[0]
        with STATE.lock:
            s, reason = self._session(allow_query=True)
            if s is None:
                return self.send_error_json(403, "blocked", blocked=True,
                                            session_closed=(reason == "gone"))
            if not STATE.sharing_on:
                return self.send_error_json(403, "sharing_off", sharing_on=False)
            full, root = STATE.resolve_node(node_id)
            if full is None or not os.path.isfile(full):
                return self.send_error_json(404, "not_found")
            if not STATE.is_selected(full):
                return self.send_error_json(403, "not_shared")
            try:
                f = open(full, "rb")
                size = os.fstat(f.fileno()).st_size   # fstat of the open fd — size always matches the stream
            except OSError:
                return self.send_error_json(404, "not_found")
            filename = os.path.basename(full)
            STATE.log(f"“{s['name']}” downloaded {filename}")
            key = s["name"].strip().lower()
            if key in STATE.live:
                STATE.live[key]["doing"] = "Downloading files"
                STATE.live[key]["last_seen"] = time.time()
        try:
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", self._content_disposition(filename))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                try:
                    shutil.copyfileobj(f, self.wfile, length=128 * 1024)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass
        finally:
            f.close()

    @staticmethod
    def _content_disposition(filename):
        # strip CR/LF outright — a filename with newlines would otherwise inject header lines
        filename = str(filename).replace("\r", " ").replace("\n", " ")
        try:
            filename.encode("ascii")
            fallback = filename.replace('"', "'")
            return f'attachment; filename="{fallback}"'
        except UnicodeEncodeError:
            from urllib.parse import quote
            return "attachment; filename*=UTF-8''" + quote(filename)

    # ---------- sender api ----------
    def _sender_state(self):
        data = STATE.build_trees(for_receiver=False)
        data["tree"] = data.pop("roots")   # full ticked/unticked tree nodes
        sel_version = data.pop("sel_version", 0)
        data.update({
            "ip": STATE.ip,
            "port": STATE.port,
            "share_url": STATE.share_url(),
            "sharing_on": STATE.sharing_on,
            "download_mode": STATE.download_mode,
            "code_on": STATE.code_on,
            "code_length": STATE.code_length,
            "charsets": dict(STATE.charsets),
            "code": STATE.code if STATE.code_on else "",
            "live_users": STATE.live_list(),
            "events": list(STATE.events[-60:]),
            "sel_version": sel_version,
            "roots": [{"id": r["id"], "name": r["name"], "path": r["path"]} for r in STATE.roots],
        })
        # replace tree roots' private ids with the full map (trees already carry ids)
        return data

    @staticmethod
    def _bool(body, key):
        v = body.get(key, None)
        return v if isinstance(v, bool) else None

    def _sender_post(self, path, body):
        if path == "/api/sender/root":
            raw = str(body.get("path", "")).strip()
            if not raw:
                return self.send_error_json(400, "path_required")
            root, err = STATE.add_root(raw)
            if root is None:
                return self.send_error_json(400, err or "invalid_folder")
            return self.send_json({"ok": True, "root": {"id": root["id"], "name": root["name"], "path": root["path"]},
                                    "note": err})
        if path == "/api/sender/remove_root":
            ok = STATE.remove_root(str(body.get("id", "")))
            return self.send_json({"ok": ok})
        if path == "/api/sender/toggle":
            on = self._bool(body, "on")
            if on is None:
                return self.send_error_json(400, "invalid_value")
            ok = STATE.set_selected(str(body.get("node", "")), on)
            return self.send_json({"ok": ok})
        if path == "/api/sender/bulk":
            op = str(body.get("op", "all"))
            if op not in ("all", "none", "invert", "size"):
                return self.send_error_json(400, "bad_op")
            try:
                value = float(body.get("value", 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            import math
            if math.isnan(value) or math.isinf(value):
                value = 0.0
            STATE.bulk_selection(op, body.get("folder") or None, value,
                                 str(body.get("unit", "GB")).upper(),
                                 str(body.get("dir", "above")) != "below")
            return self.send_json({"ok": True})
        if path == "/api/sender/sharing":
            on = self._bool(body, "on")
            if on is None:
                return self.send_error_json(400, "invalid_value")
            STATE.sharing_on = on
            STATE.log("Sharing ON" if STATE.sharing_on else "Sharing OFF")
            STATE._save()
            return self.send_json({"ok": True, "sharing_on": STATE.sharing_on})
        if path == "/api/sender/download_mode":
            mode = str(body.get("mode", "single"))
            if mode in ("single", "all"):
                STATE.download_mode = mode
                STATE._save()
            return self.send_json({"ok": True, "download_mode": STATE.download_mode})
        if path == "/api/sender/code_on":
            on = self._bool(body, "on")
            if on is None:
                return self.send_error_json(400, "invalid_value")
            STATE.set_code_on(on)
            STATE.log("Login code ON" if STATE.code_on else "Login code OFF")
            return self.send_json({"ok": True, "code_on": STATE.code_on, "code": STATE.code if STATE.code_on else ""})
        if path == "/api/sender/code_length":
            code = STATE.set_code_length(str(body.get("length", "4")))
            return self.send_json({"ok": True, "length": STATE.code_length, "code": code})
        if path == "/api/sender/code_charset":
            on = self._bool(body, "on")
            if on is None:
                return self.send_error_json(400, "invalid_value")
            STATE.set_charset(str(body.get("key", "")), on)
            return self.send_json({"ok": True, "charsets": dict(STATE.charsets),
                                    "code": STATE.code if STATE.code_on else ""})
        if path == "/api/sender/custom_code":
            code = STATE.set_custom_code(str(body.get("value", "")))
            return self.send_json({"ok": True, "code": code})
        if path == "/api/sender/generate_code":
            code = STATE.generate_code()
            STATE.log("New login code generated")
            return self.send_json({"ok": True, "code": code})
        if path == "/api/sender/block":
            block = self._bool(body, "block")
            if block is None:
                return self.send_error_json(400, "invalid_value")
            ok = STATE.set_blocked(body.get("name", ""), block)
            return self.send_json({"ok": ok})
        return self.send_error_json(404, "not_found")


class M1ThreadingHTTPServer(ThreadingHTTPServer):
    """Threading server with an idle-read timeout and a ceiling on active handler threads."""

    daemon_threads = True
    _slots = threading.BoundedSemaphore(64)

    def setup(self):
        super().setup()
        try:
            self.request.settimeout(DOWNLOAD_KEEPALIVE)
        except OSError:
            pass

    def process_request_thread(self, request, client_address):
        if not self._slots.acquire(timeout=5):
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(port, allow_remote_sender=False):
    for p in range(port, port + MAX_PORT_TRIES):
        try:
            srv = M1ThreadingHTTPServer(("0.0.0.0", p), Handler)
        except OSError:
            continue
        srv.allow_remote_sender = allow_remote_sender
        # socketserver dumps a full traceback for every dropped/ reset
        # connection (browsers closing keep-alive sockets early) — log those
        # as one line, and keep full tracebacks for real handler bugs.
        def handle_error(request, client_address):
            et, ev, _tb = sys.exc_info()
            if et is not None and issubclass(et, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
                print(f"Connection dropped by {client_address[0]} ({ev}) — ignored")
                return
            import traceback
            traceback.print_exc()
        srv.handle_error = handle_error
        STATE.port = p
        return srv
    raise RuntimeError(f"No free port in range {port}-{port + MAX_PORT_TRIES - 1}")


# ------------------------------------------------------------------- gui ---
def run_gui(server):
    import tkinter as tk
    from tkinter import messagebox, scrolledtext
    try:
        from PIL import Image, ImageTk
        PIL_OK = True
    except Exception:
        PIL_OK = False

    BG = "#ffffff"
    CARD = "#f0f0ef"
    INK = "#1d1d1b"
    MUTED = "#6f6f6c"
    GREEN = "#16a34a"
    GREEN_DARK = "#15803d"
    RED = "#d92d20"

    root = tk.Tk()
    root.title(f"{APP_NAME} — File sharing over Wi-Fi")
    root.configure(bg=BG)
    root.minsize(460, 620)
    try:
        root.iconbitmap(resource_path(os.path.join("assets", "m1share.ico")))
    except Exception:
        pass

    outer = tk.Frame(root, bg=BG, padx=22, pady=18)
    outer.pack(fill="both", expand=True)

    # header logo
    logo_label = tk.Label(outer, bg=BG)
    logo_label.pack(anchor="center", pady=(0, 2))
    if PIL_OK:
        try:
            img = Image.open(resource_path(os.path.join("assets", "m1share-logo-horizontal.jpg")))
            w = 250
            h = int(img.height * (w / img.width))
            img = img.resize((w, h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            logo_label.configure(image=photo)
            logo_label.image = photo
        except Exception:
            logo_label.configure(text=APP_NAME, font=("Segoe UI", 22, "bold"), fg=INK)
    else:
        logo_label.configure(text=APP_NAME, font=("Segoe UI", 22, "bold"), fg=INK)

    tk.Label(outer, text="Files on this PC, shared with your Wi-Fi",
             bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(anchor="center", pady=(2, 12))

    # share link card
    card = tk.Frame(outer, bg=CARD, padx=16, pady=14)
    card.pack(fill="x")
    tk.Label(card, text="SEND THIS LINK TO YOUR RECEIVERS", bg=CARD, fg=MUTED,
             font=("Segoe UI", 8, "bold")).pack(anchor="w")
    link_var = tk.StringVar(value=STATE.share_url())
    link_entry = tk.Entry(card, textvariable=link_var, font=("Consolas", 15, "bold"),
                          justify="center", relief="flat", bg="#ffffff", fg=INK, bd=0)
    link_entry.pack(fill="x", pady=(6, 8), ipady=8)
    link_entry.configure(state="readonly")

    btn_row = tk.Frame(card, bg=CARD)
    btn_row.pack(fill="x")

    def copy_link():
        root.clipboard_clear()
        root.clipboard_append(STATE.share_url())
        root.update()
        copy_btn.configure(text="Copied ✓")
        root.after(1600, lambda: copy_btn.configure(text="Copy link"))

    copy_btn = tk.Button(btn_row, text="Copy link", command=copy_link,
                         bg=GREEN, fg="#ffffff", activebackground=GREEN_DARK,
                         activeforeground="#ffffff", relief="flat", font=("Segoe UI", 10, "bold"),
                         padx=14, pady=6, cursor="hand2")
    copy_btn.pack(side="left", fill="x", expand=True)

    tk.Label(card, text=f"Address {STATE.ip}   ·   Port {STATE.port}   ·   Works on the same Wi-Fi / router",
             bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="center", pady=(8, 0))

    # sharing toggle
    toggle_frame = tk.Frame(outer, bg=BG, pady=12)
    toggle_frame.pack(fill="x")
    sharing_var = tk.StringVar(value="Sharing is ON")
    status_dot = tk.Label(toggle_frame, text="●", bg=BG, fg=GREEN, font=("Segoe UI", 16))
    status_dot.pack(side="left")
    tk.Label(toggle_frame, textvariable=sharing_var, bg=BG, fg=INK,
             font=("Segoe UI", 12, "bold")).pack(side="left", padx=8)

    def toggle_sharing():
        STATE.sharing_on = not STATE.sharing_on
        STATE.log("Sharing ON" if STATE.sharing_on else "Sharing OFF")
        STATE._save()
        refresh_sharing()

    toggle_btn = tk.Button(toggle_frame, text="Turn OFF", command=toggle_sharing,
                           bg=RED, fg="#ffffff", relief="flat", font=("Segoe UI", 10, "bold"),
                           padx=16, pady=6, cursor="hand2")
    toggle_btn.pack(side="right")

    def refresh_sharing():
        on = STATE.sharing_on
        sharing_var.set("Sharing is ON" if on else "Sharing is OFF")
        status_dot.configure(fg=GREEN if on else RED)
        toggle_btn.configure(text="Turn OFF" if on else "Turn ON",
                             bg=RED if on else GREEN,
                             activebackground="#b42318" if on else GREEN_DARK)

    # action buttons
    actions = tk.Frame(outer, bg=BG)
    actions.pack(fill="x", pady=(2, 10))

    def open_sender_panel():
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{STATE.port}/sender")

    def open_receiver_page():
        import webbrowser
        webbrowser.open(STATE.share_url())

    def add_folder_dialog():
        from tkinter import filedialog
        path = filedialog.askdirectory(title="Choose a folder to share")
        if path:
            root_obj, err = STATE.add_root(path)
            if root_obj:
                log_refresh(force=True)

    for text, cmd in (("Sender panel (control)", open_sender_panel),
                      ("Open receiver page", open_receiver_page),
                      ("Add folder…", add_folder_dialog)):
        tk.Button(actions, text=text, command=cmd, bg="#e9e9e7", fg=INK, relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=10, pady=8,
                  cursor="hand2", activebackground="#dededb").pack(side="left", fill="x", expand=True, padx=2)

    # log
    tk.Label(outer, text="LIVE EVENTS", bg=BG, fg=MUTED,
             font=("Segoe UI", 8, "bold")).pack(anchor="w")
    log_box = scrolledtext.ScrolledText(outer, height=10, font=("Consolas", 9),
                                        bg="#fafaf9", fg=INK, relief="flat",
                                        state="disabled", wrap="word")
    log_box.pack(fill="both", expand=True, pady=(4, 10))

    tk.Label(outer,
             text="Keep this window open while sharing. Closing it stops sharing.\n"
                  "If receivers can't connect, allow “M1 Share” through Windows Firewall.",
             bg=BG, fg=MUTED, font=("Segoe UI", 8), justify="center").pack(anchor="center")

    shown_events = {"count": 0}

    def log_refresh(force=False):
        events = STATE.events
        if force or len(events) != shown_events["count"]:
            log_box.configure(state="normal")
            log_box.delete("1.0", "end")
            log_box.insert("1.0", "\n".join(events[-120:]) or "No events yet — waiting for receivers…")
            log_box.see("end")
            log_box.configure(state="disabled")
            shown_events["count"] = len(events)
        refresh_sharing()
        root.after(1000, log_refresh)

    def on_close():
        if messagebox.askyesno("Quit M1 Share?",
                               "Closing M1 Share stops sharing for everyone on your Wi-Fi.\n\nQuit now?"):
            root.destroy()
            os._exit(0)   # hard exit — the server thread is a daemon anyway

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_sharing()
    log_refresh()
    root.mainloop()


def _acquire_single_instance():
    """Windows named mutex — refuse to run a second copy (it would split brain over state.json).
    Returns the handle, or None when unavailable/another instance holds it (exits in that case)."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        handle = kernel32.CreateMutexW(None, False, "Local\\M1Share-Singleton")
        if not handle:
            return True     # can't create — don't block startup
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            try:
                import tkinter as tk
                from tkinter import messagebox
                r = tk.Tk(); r.withdraw()
                messagebox.showinfo("M1 Share is already running",
                                    "M1 Share is already running on this PC —\n"
                                    "check the taskbar / hidden icons.\n\n"
                                    "Close it first, then start it again.")
                r.destroy()
            except Exception:
                pass
            return None
        return True
    except Exception:
        return True         # non-Windows or ctypes failing — proceed without the guard


def main():
    parser = argparse.ArgumentParser(description="M1 Share — LAN file sharing")
    parser.add_argument("--serve-only", action="store_true",
                        help="Run the server without the GUI (for testing)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--allow-remote-sender", action="store_true",
                        help="Let other devices on the Wi-Fi open the sender panel/API "
                             "(default: this PC only — NOT recommended)")
    args = parser.parse_args()
    if not (1 <= args.port <= 65535):
        parser.error(f"--port must be between 1 and 65535 (got {args.port})")
    if not _acquire_single_instance():
        return

    try:
        server = make_server(args.port, allow_remote_sender=args.allow_remote_sender)
    except Exception as e:
        # windowed exe has no console — show the failure in a message box instead
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(
                "M1 Share could not start",
                f"{e}\n\nPorts {args.port}-{args.port + MAX_PORT_TRIES - 1} all seem taken or blocked.\n"
                "Close other programs using those ports, or pick another one:\n"
                "M1 Share.exe --port 8181")
            r.destroy()
        except Exception:
            try:
                os.makedirs(CONFIG_DIR, exist_ok=True)
                with open(os.path.join(CONFIG_DIR, "error.log"), "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now().isoformat()} startup failed: {e}\n")
            except OSError:
                pass
        return
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="m1share-server")
    thread.start()
    print(f"{APP_NAME} {APP_VERSION} — serving on {STATE.share_url()} (sharing {'ON' if STATE.sharing_on else 'OFF'})  PID {os.getpid()}")

    if args.serve_only:
        print("Headless test mode — kill this process via Task Manager (find 'python' / 'M1 Share') to stop it.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return

    run_gui(server)


if __name__ == "__main__":
    main()
