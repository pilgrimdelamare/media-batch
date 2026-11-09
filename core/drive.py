"""Client Google Drive minimale (solo requests) con refresh token OAuth."""
import json
import os
import time
from pathlib import Path

import requests

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"


class Drive:
    def __init__(self, client_id, client_secret, refresh_token):
        self.cid, self.secret, self.rt = client_id, client_secret, refresh_token
        self._tok, self._exp = None, 0

    @classmethod
    def from_env(cls, prefix="DRIVE"):
        """Legge <PREFIX>_AUTH (JSON con client_id/client_secret/refresh_token)
        da variabile d'ambiente o dal file indicato da <PREFIX>_AUTH_FILE."""
        raw = os.environ.get(f"{prefix}_AUTH")
        if not raw:
            raw = Path(os.environ[f"{prefix}_AUTH_FILE"]).read_text()
        d = json.loads(raw)
        return cls(d["client_id"], d["client_secret"], d["refresh_token"])

    def token(self):
        if not self._tok or time.time() > self._exp - 60:
            r = requests.post("https://oauth2.googleapis.com/token", data={
                "client_id": self.cid, "client_secret": self.secret,
                "refresh_token": self.rt, "grant_type": "refresh_token"}, timeout=30)
            r.raise_for_status()
            j = r.json()
            self._tok, self._exp = j["access_token"], time.time() + j.get("expires_in", 3600)
        return self._tok

    def _h(self):
        return {"Authorization": f"Bearer {self.token()}"}

    def _req(self, method, url, **kw):
        for attempt in range(5):
            r = requests.request(method, url, headers=self._h(), timeout=kw.pop("timeout", 120), **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()

    # ── query ──────────────────────────────────────────────────────────
    def list(self, parent, name=None, mime=None):
        q = [f"'{parent}' in parents", "trashed=false"]
        if name:
            q.append("name='%s'" % name.replace("'", "\\'"))
        if mime:
            q.append(f"mimeType='{mime}'")
        out, page = [], None
        while True:
            params = {"q": " and ".join(q), "pageSize": 1000,
                      "fields": "nextPageToken,files(id,name,mimeType,createdTime,size)",
                      "orderBy": "createdTime"}
            if page:
                params["pageToken"] = page
            j = self._req("GET", f"{API}/files", params=params).json()
            out += j.get("files", [])
            page = j.get("nextPageToken")
            if not page:
                return out

    def folder(self, name, parent="root"):
        """Ritorna l'id della cartella name dentro parent, creandola se manca."""
        f = self.list(parent, name=name, mime=FOLDER)
        if f:
            return f[0]["id"]
        j = self._req("POST", f"{API}/files", json={
            "name": name, "mimeType": FOLDER, "parents": [parent]}).json()
        return j["id"]

    def path(self, *names):
        cur = "root"
        for n in names:
            cur = self.folder(n, cur)
        return cur

    # ── file ───────────────────────────────────────────────────────────
    def upload(self, local, parent, name=None, mime="application/octet-stream"):
        name = name or Path(local).name
        size = Path(local).stat().st_size
        r = self._req("POST", f"{UPLOAD}?uploadType=resumable", json={
            "name": name, "parents": [parent]}, timeout=60)
        url = r.headers["Location"]
        chunk = 32 * 1024 * 1024
        with open(local, "rb") as fh:
            pos = 0
            while True:
                data = fh.read(chunk)
                end = pos + len(data) - 1
                hdr = {**self._h(), "Content-Length": str(len(data)),
                       "Content-Range": f"bytes {pos}-{end}/{size}" if data else f"bytes */{size}"}
                rr = requests.put(url, headers=hdr, data=data, timeout=600)
                if rr.status_code in (200, 201):
                    return rr.json()["id"]
                if rr.status_code != 308:
                    rr.raise_for_status()
                pos = end + 1

    def put_json(self, obj, parent, name, file_id=None):
        body = json.dumps(obj, ensure_ascii=False, indent=1).encode()
        if file_id:
            requests.patch(f"{UPLOAD}/{file_id}?uploadType=media", data=body,
                           headers={**self._h(), "Content-Type": "application/json"},
                           timeout=60).raise_for_status()
            return file_id
        meta = {"name": name, "parents": [parent]}
        files = {"metadata": ("m", json.dumps(meta), "application/json"),
                 "file": ("f", body, "application/json")}
        r = requests.post(f"{UPLOAD}?uploadType=multipart", headers=self._h(), files=files, timeout=60)
        r.raise_for_status()
        return r.json()["id"]

    def get_json(self, file_id):
        return json.loads(self._req("GET", f"{API}/files/{file_id}", params={"alt": "media"}).content)

    def download(self, file_id, local):
        with requests.get(f"{API}/files/{file_id}", params={"alt": "media"},
                          headers=self._h(), stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(local, "wb") as fh:
                for c in r.iter_content(1 << 20):
                    fh.write(c)
        return local

    def move(self, file_id, new_parent, old_parent):
        self._req("PATCH", f"{API}/files/{file_id}",
                  params={"addParents": new_parent, "removeParents": old_parent})

    def delete(self, file_id):
        self._req("DELETE", f"{API}/files/{file_id}")
