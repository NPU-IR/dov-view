"""
Doc Preview Service for AscendNPU-IR
"""
import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
REPO_DIR = BASE_DIR / "AscendNPU-IR"
REPO_REMOTE = "https://gitcode.com/Ascend/AscendNPU-IR.git"
BUILDS_DIR = BASE_DIR / "builds"
WORKTREE_BASE = Path("/tmp")

COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
JOB_ID_RE = re.compile(r"^(pr-\d+|[0-9a-f]{7,40})$")

app = FastAPI(title="Doc Preview")
BUILDS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# In-memory build state
# ---------------------------------------------------------------------------
builds: dict[str, dict] = {}
build_locks: dict[str, asyncio.Lock] = {}


def _get_lock(commit_id: str) -> asyncio.Lock:
    if commit_id not in build_locks:
        build_locks[commit_id] = asyncio.Lock()
    return build_locks[commit_id]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class BuildRequest(BaseModel):
    commit: str | None = None   # hex commit SHA
    pr: int | None = None       # PR number
    force: bool = False         # force rebuild even if cached


# ---------------------------------------------------------------------------
# Build worker
# ---------------------------------------------------------------------------
async def _stream_subprocess(
    cmd: list[str],
    cwd: str,
    env: dict,
    log_lines: list[str],
) -> int:
    """Run a subprocess, append output lines to log_lines. Return exit code."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        log_lines.append(line)
    await proc.wait()
    return proc.returncode


async def _do_build(job_id: str, pr_number: int | None = None) -> None:
    state = builds[job_id]
    state["status"] = "running"
    logs: list[str] = state["logs"]
    build_dest = BUILDS_DIR / job_id

    env = os.environ.copy()

    # worktree path — resolved after we know the commit SHA
    wt_path: Path | None = None

    try:
        # 1. Clone repo if missing
        if not REPO_DIR.exists():
            logs.append(f"[info] Cloning {REPO_REMOTE} into {REPO_DIR}")
            rc = await _stream_subprocess(
                ["git", "clone", "--no-checkout", REPO_REMOTE, str(REPO_DIR)],
                cwd=str(BASE_DIR),
                env=env,
                log_lines=logs,
            )
            if rc != 0:
                raise RuntimeError(f"git clone failed (exit {rc})")

        # 2. Fetch: PR ref or all branches
        if pr_number is not None:
            logs.append(f"[info] Fetching PR #{pr_number} from remote")
            rc = await _stream_subprocess(
                ["git", "-C", str(REPO_DIR), "fetch", REPO_REMOTE,
                 f"+refs/merge-requests/{pr_number}/head"],
                cwd=str(REPO_DIR),
                env=env,
                log_lines=logs,
            )
            if rc != 0:
                raise RuntimeError(f"git fetch PR #{pr_number} failed (exit {rc})")

            # Resolve FETCH_HEAD to commit SHA
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(REPO_DIR), "rev-parse", "FETCH_HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            out, _ = await proc.communicate()
            resolved_commit = out.decode().strip()
            state["resolved_commit"] = resolved_commit
            logs.append(f"[info] PR #{pr_number} → commit {resolved_commit}")
            worktree_ref = resolved_commit
        else:
            logs.append("[info] Fetching latest from remote")
            rc = await _stream_subprocess(
                ["git", "-C", str(REPO_DIR), "fetch", "--all"],
                cwd=str(REPO_DIR),
                env=env,
                log_lines=logs,
            )
            if rc != 0:
                raise RuntimeError(f"git fetch failed (exit {rc})")
            worktree_ref = job_id  # job_id is the commit SHA

        # 3. Worktree add
        wt_path = WORKTREE_BASE / f"wt-{job_id}"
        logs.append(f"[info] Creating worktree at {wt_path}")
        rc = await _stream_subprocess(
            ["git", "-C", str(REPO_DIR), "worktree", "add",
             "--detach", str(wt_path), worktree_ref],
            cwd=str(REPO_DIR),
            env=env,
            log_lines=logs,
        )
        if rc != 0:
            raise RuntimeError(f"git worktree add failed (exit {rc})")

        # 4. Build docs
        docs_dir = wt_path / "docs"
        logs.append("[info] Running: make html-all")
        rc = await _stream_subprocess(
            ["make", "html-all"],
            cwd=str(docs_dir),
            env=env,
            log_lines=logs,
        )
        if rc != 0:
            raise RuntimeError(f"make html-all failed (exit {rc})")

        # 5. Copy entire _build output to builds/{job_id}/
        src = docs_dir / "_build"
        if not src.exists():
            raise RuntimeError("_build directory not found after make html-all")
        if build_dest.exists():
            shutil.rmtree(build_dest)
        shutil.copytree(src, build_dest)
        logs.append(f"[info] Copied _build to {build_dest}")

        state["status"] = "success"
        logs.append("[info] Build completed successfully.")

    except Exception as exc:
        state["status"] = "failed"
        logs.append(f"[error] {exc}")

    finally:
        state["completed_at"] = time.time()
        # Clean up worktree
        if wt_path and wt_path.exists():
            try:
                rc = await _stream_subprocess(
                    ["git", "-C", str(REPO_DIR), "worktree", "remove",
                     "--force", str(wt_path)],
                    cwd=str(REPO_DIR),
                    env=env,
                    log_lines=logs,
                )
                if rc != 0:
                    shutil.rmtree(wt_path, ignore_errors=True)
            except Exception:
                shutil.rmtree(wt_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(FRONTEND_HTML)


class BuildResponse(BaseModel):
    job_id: str
    status: str
    cached: bool
    resolved_commit: str | None = None


@app.post("/api/build", response_model=BuildResponse)
async def start_build(req: BuildRequest):
    if req.pr is not None:
        if req.pr <= 0:
            raise HTTPException(400, "PR number must be a positive integer")
        job_id = f"pr-{req.pr}"
        pr_number = req.pr
    elif req.commit is not None:
        commit_id = req.commit.strip().lower()
        if not COMMIT_RE.match(commit_id):
            raise HTTPException(400, "Invalid commit ID (must be 7–40 hex chars)")
        job_id = commit_id
        pr_number = None
    else:
        raise HTTPException(400, "Provide either 'commit' or 'pr'")

    state = builds.get(job_id)

    # Cache hit (skip if force rebuild)
    if not req.force and state and state["status"] == "success":
        return BuildResponse(
            job_id=job_id, status="success", cached=True,
            resolved_commit=state.get("resolved_commit"),
        )

    # Already running
    if state and state["status"] in ("pending", "running"):
        return BuildResponse(
            job_id=job_id, status=state["status"], cached=False,
            resolved_commit=state.get("resolved_commit"),
        )

    # Start fresh build
    builds[job_id] = {
        "status": "pending",
        "logs": [],
        "started_at": time.time(),
        "completed_at": None,
        "resolved_commit": None,
    }
    asyncio.create_task(_do_build(job_id, pr_number=pr_number))
    return BuildResponse(job_id=job_id, status="pending", cached=False)


@app.get("/api/build/{job_id}/status")
async def build_status(job_id: str):
    if job_id not in builds:
        raise HTTPException(404, "Build not found")
    state = builds[job_id]
    return {
        "job_id": job_id,
        "status": state["status"],
        "started_at": state["started_at"],
        "completed_at": state["completed_at"],
        "resolved_commit": state.get("resolved_commit"),
        "log_count": len(state["logs"]),
    }


async def _sse_log_generator(job_id: str) -> AsyncIterator[str]:
    if job_id not in builds:
        yield "data: [error] Build not found\n\n"
        return

    sent = 0
    while True:
        state = builds[job_id]
        logs = state["logs"]

        while sent < len(logs):
            line = logs[sent].replace("\n", " ")
            yield f"data: {line}\n\n"
            sent += 1

        if state["status"] in ("success", "failed"):
            yield f"data: __STATUS__{state['status']}\n\n"
            break

        await asyncio.sleep(0.3)


@app.get("/api/build/{job_id}/stream")
async def stream_logs(job_id: str):
    return StreamingResponse(
        _sse_log_generator(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/preview/{job_id}/{path:path}")
async def preview_file(job_id: str, path: str):
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(400, "Invalid job ID")

    base = BUILDS_DIR / job_id
    if not base.exists():
        raise HTTPException(404, "Build not found or not complete")

    if not path or path == "/":
        path = "index.html"

    file_path = (base / path).resolve()
    # Security: ensure resolved path is still inside the build directory
    if not str(file_path).startswith(str(base.resolve())):
        raise HTTPException(403, "Forbidden")

    if not file_path.exists():
        raise HTTPException(404, f"File not found: {path}")

    return FileResponse(file_path)


@app.get("/preview/{job_id}")
async def preview_root(job_id: str):
    return await preview_file(job_id, "index.html")


# ---------------------------------------------------------------------------
# Frontend HTML
# ---------------------------------------------------------------------------
FRONTEND_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>AscendNPU-IR Doc Preview</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #e6edf3; min-height: 100vh; }
  .header { background: #161b22; border-bottom: 1px solid #30363d;
            padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 18px; font-weight: 600; }
  .header span { color: #8b949e; font-size: 14px; }
  .header-links { margin-left: auto; display: flex; gap: 12px; }
  .header-links a { color: #8b949e; font-size: 13px; text-decoration: none;
    display: inline-flex; align-items: center; gap: 5px; }
  .header-links a:hover { color: #58a6ff; }
  .main { max-width: 1400px; margin: 40px auto; padding: 0 24px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 24px; margin-bottom: 24px; }
  .card h2 { font-size: 16px; margin-bottom: 16px; color: #58a6ff; }
  .input-row { display: flex; gap: 10px; }
  input[type=text] { flex: 1; background: #0d1117; border: 1px solid #30363d;
    border-radius: 6px; padding: 8px 12px; color: #e6edf3; font-size: 14px;
    font-family: 'SFMono-Regular', Consolas, monospace; }
  input[type=text]:focus { outline: none; border-color: #58a6ff; }
  button { background: #238636; color: #fff; border: none; border-radius: 6px;
           padding: 8px 18px; font-size: 14px; cursor: pointer; white-space: nowrap; }
  button:hover { background: #2ea043; }
  button:disabled { background: #21262d; color: #484f58; cursor: not-allowed; }
  .status-badge { display: inline-block; padding: 3px 10px; border-radius: 12px;
                  font-size: 12px; font-weight: 600; margin-left: 10px; }
  .badge-pending  { background: #6e40c9; }
  .badge-running  { background: #e3b341; color: #0d1117; }
  .badge-success  { background: #238636; }
  .badge-failed   { background: #da3633; }
  #log-box { background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
             padding: 12px; height: 280px; overflow-y: auto; font-family: monospace;
             font-size: 12px; line-height: 1.6; display: none; margin-top: 16px; }
  #log-box .err { color: #f85149; }
  #log-box .warn { color: #e3b341; }
  #log-box .info { color: #58a6ff; }
  .preview-links { display: flex; gap: 12px; flex-wrap: wrap; }
  .preview-links a { background: #21262d; color: #58a6ff; text-decoration: none;
    border: 1px solid #30363d; border-radius: 6px; padding: 8px 16px;
    font-size: 14px; display: inline-flex; align-items: center; gap: 6px; }
  .preview-links a:hover { background: #30363d; }
  #preview-frame-wrap { display: none; margin-top: 24px; }
  #preview-frame-wrap .frame-bar { display: flex; gap: 8px; margin-bottom: 8px;
    align-items: center; }
  #preview-frame-wrap .frame-bar span { font-size: 13px; color: #8b949e; }
  .lang-btn { background: #21262d; color: #e6edf3; border: 1px solid #30363d;
    border-radius: 6px; padding: 4px 12px; font-size: 13px; cursor: pointer; }
  .lang-btn.active { border-color: #58a6ff; color: #58a6ff; }
  .rebuild-btn { background: #21262d; color: #e3b341; border: 1px solid #e3b341;
    border-radius: 6px; padding: 3px 10px; font-size: 12px; cursor: pointer; margin-left: 8px; }
  .rebuild-btn:hover { background: #2d2a1f; }
  iframe { width: 100%; height: 700px; border: 1px solid #30363d;
           border-radius: 6px; background: #fff; }
</style>
</head>
<body>
<div class="header">
  <h1>AscendNPU-IR Doc Preview</h1>
  <span>输入 PR 号或 commit SHA 构建并预览 Sphinx 文档</span>
  <div class="header-links">
    <a href="https://gitcode.com/Ascend/AscendNPU-IR" target="_blank">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M2 2.5A2.5 2.5 0 0 1 4.5 0h8.75a.75.75 0 0 1 .75.75v12.5a.75.75 0 0 1-.75.75h-2.5a.75.75 0 0 1 0-1.5h1.75v-2h-8a1 1 0 0 0-.714 1.7.75.75 0 1 1-1.072 1.05A2.495 2.495 0 0 1 2 11.5Zm10.5-1h-8a1 1 0 0 0-1 1v6.708A2.486 2.486 0 0 1 4.5 9h8Z"/></svg>
      AscendNPU-IR
    </a>
    <a href="https://gitcode.com/NPU-IR/doc-preview" target="_blank">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M2 2.5A2.5 2.5 0 0 1 4.5 0h8.75a.75.75 0 0 1 .75.75v12.5a.75.75 0 0 1-.75.75h-2.5a.75.75 0 0 1 0-1.5h1.75v-2h-8a1 1 0 0 0-.714 1.7.75.75 0 1 1-1.072 1.05A2.495 2.495 0 0 1 2 11.5Zm10.5-1h-8a1 1 0 0 0-1 1v6.708A2.486 2.486 0 0 1 4.5 9h8Z"/></svg>
      doc-preview
    </a>
  </div>
</div>
<div class="main">
  <div class="card">
    <h2>构建文档</h2>
    <div class="input-row">
      <input type="text" id="ref-input" placeholder="PR 号（如 42）或 commit SHA（如 abc1234）"
             maxlength="40" spellcheck="false"/>
      <button id="build-btn" onclick="startBuild()">构建</button>
    </div>
    <div id="status-line" style="margin-top:12px;font-size:14px;display:none">
      <span id="status-text"></span>
      <span id="status-badge" class="status-badge"></span>
    </div>
    <div id="log-box"></div>
  </div>

  <div class="card" id="preview-card" style="display:none">
    <h2>预览</h2>
    <div class="preview-links" id="preview-links"></div>
    <div id="preview-frame-wrap">
      <div class="frame-bar">
        <span>预览：</span>
        <button class="lang-btn active" id="btn-en" onclick="switchLang('en')">English</button>
        <button class="lang-btn" id="btn-zh" onclick="switchLang('zh_cn')">中文</button>
      </div>
      <iframe id="preview-iframe" src="about:blank"></iframe>
    </div>
  </div>
</div>

<script>
let currentCommit = '';
let currentLang = 'en';
let evtSource = null;

function setStatus(text, badge) {
  const sl = document.getElementById('status-line');
  sl.style.display = 'block';
  document.getElementById('status-text').textContent = text;
  const b = document.getElementById('status-badge');
  b.textContent = badge;
  b.className = 'status-badge badge-' + badge.toLowerCase();
}

function appendLog(line) {
  const box = document.getElementById('log-box');
  box.style.display = 'block';
  const div = document.createElement('div');
  if (line.startsWith('[error]')) div.className = 'err';
  else if (line.startsWith('[warn]')) div.className = 'warn';
  else if (line.startsWith('[info]')) div.className = 'info';
  div.textContent = line;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function showCachedActions(jobId, label, body) {
  showPreview(jobId);
  const sl = document.getElementById('status-line');
  const btn = document.createElement('button');
  btn.textContent = '重新构建';
  btn.className = 'rebuild-btn';
  btn.onclick = () => { btn.remove(); startBuildRequest(label, body, jobId, true); };
  sl.appendChild(btn);
}

function showPreview(commitId) {
  const card = document.getElementById('preview-card');
  card.style.display = 'block';
  const links = document.getElementById('preview-links');
  links.innerHTML =
    `<a href="/preview/${commitId}/en/index.html" target="_blank">&#127760; English</a>` +
    `<a href="/preview/${commitId}/zh_cn/index.html" target="_blank">&#127758; 中文</a>` +
    `<button class="lang-btn" onclick="openFrame('${commitId}')">在页面内预览</button>`;
}

function openFrame(commitId) {
  const wrap = document.getElementById('preview-frame-wrap');
  wrap.style.display = 'block';
  switchLang(currentLang, commitId);
  wrap.scrollIntoView({ behavior: 'smooth' });
}

function switchLang(lang, cid) {
  cid = cid || currentCommit;
  currentLang = lang;
  document.getElementById('btn-en').className = 'lang-btn' + (lang === 'en' ? ' active' : '');
  document.getElementById('btn-zh').className = 'lang-btn' + (lang === 'zh_cn' ? ' active' : '');
  document.getElementById('preview-iframe').src = `/preview/${cid}/${lang}/index.html`;
}

async function startBuild() {
  const val = document.getElementById('ref-input').value.trim();
  let body, label;
  if (/^\d+$/.test(val)) {
    body = { pr: parseInt(val, 10) };
    label = `PR #${val}`;
  } else if (/^[0-9a-f]{7,40}$/i.test(val)) {
    body = { commit: val.toLowerCase() };
    label = `commit ${val.toLowerCase()}`;
  } else {
    alert('请输入 PR 号（纯数字）或 commit SHA（7~40 位十六进制）');
    return;
  }

  document.getElementById('build-btn').disabled = true;
  document.getElementById('log-box').innerHTML = '';
  document.getElementById('log-box').style.display = 'none';
  document.getElementById('preview-card').style.display = 'none';

  setStatus(label, 'pending');
  await startBuildRequest(label, body);
}

async function startBuildRequest(label, body, jobIdHint, force) {
  const resp = await fetch('/api/build', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(force ? { ...body, force: true } : body),
  });

  if (!resp.ok) {
    const err = await resp.json();
    setStatus('请求失败: ' + (err.detail || resp.status), 'failed');
    document.getElementById('build-btn').disabled = false;
    return;
  }

  const data = await resp.json();
  const jobId = data.job_id;
  currentCommit = jobId;

  if (data.cached) {
    setStatus(`${label} (已缓存)`, 'success');
    showCachedActions(jobId, label, body);
    document.getElementById('build-btn').disabled = false;
    return;
  }

  // Stream logs
  document.getElementById('log-box').innerHTML = '';
  document.getElementById('log-box').style.display = 'none';
  document.getElementById('preview-card').style.display = 'none';
  setStatus(label, 'running');
  if (evtSource) evtSource.close();
  evtSource = new EventSource(`/api/build/${jobId}/stream`);
  evtSource.onmessage = (e) => {
    if (e.data.startsWith('__STATUS__')) {
      const finalStatus = e.data.replace('__STATUS__', '');
      setStatus(label, finalStatus);
      if (finalStatus === 'success') showPreview(jobId);
      evtSource.close();
      document.getElementById('build-btn').disabled = false;
    } else {
      appendLog(e.data);
    }
  };
  evtSource.onerror = () => {
    evtSource.close();
    document.getElementById('build-btn').disabled = false;
  };
}

// Allow Enter key
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('ref-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') startBuild();
  });
});
</script>
</body>
</html>
"""
