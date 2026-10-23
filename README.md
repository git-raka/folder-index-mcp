# folder-index-mcp

A local, private full-text index of your folders (Downloads, Documents, a document archive, a shared drive) for Claude Code, Claude Desktop, Gemini CLI, Cursor, VS Code and any other [MCP](https://modelcontextprotocol.io) client. It comes with a desktop app that shows indexing progress and which AI apps are connected.

Without an index, an agent has to run `ls` and `grep` and open PDFs one by one, which is slow and expensive in tokens. With it, the agent asks the index and gets back a handful of ranked lines:

```
> search_files("invoice march hosting")
2 result(s) for 'invoice march hosting':
1. Downloads/invoices/2026-03 Hosting Invoice.pdf  (98.4 KB, 2026-03-31)
   …[Invoice] No. 0042 … period: [March] 2026 … managed [hosting] plan…
2. ...
```

- **SQLite FTS5**: one file per folder, no server, BM25 ranking, prefix matching, phrase/OR/NOT queries.
- **Several folders**: each folder has its own index, and searches run across all of them with merged, ranked results.
- **Reads contents**: PDF, Word, Excel, PowerPoint, OpenDocument, legacy `.doc/.xls/.ppt` (best effort), plain text, code, and the file listing inside `.zip` archives.
- **OCR for scans and images** (optional): scanned PDFs and screenshots/photos are OCR'd in the background, newest first, using the OCR engine built into Windows or `tesseract` anywhere else. A turbo mode uses every CPU core.
- **Incremental**: only new or changed files are re-read, and deleted files are dropped. Text extraction runs in parallel.
- **Safe by default**: `.ssh`, `.git`, `node_modules`, virtualenvs and caches are never walked. Keys, certificates, VPN profiles, `.env` files and shell history are listed by name only, and their content is never read. Indexes live outside the indexed folders, in `~/.folder-index` (`C:\Users\<you>\.folder-index` on Windows), and nothing leaves your machine.
- **Code-aware**: a folder containing `package.json`, `go.mod`, `pom.xml`, a `LICENSE` and so on is treated as a source tree or unpacked distribution. Its files are indexed by name only, except office documents and the top-level README, so extracted repos and SDKs don't drown your documents.

## Desktop app (Windows)

`FolderIndex.exe` opens a window and puts an icon in the system tray. Closing the window keeps it running in the tray; use **Quit** in the tray menu to exit.

- **Overview**: files indexed, readable content, OCR'd scans, index size, live progress of indexing and OCR with speed and time left, and the AI apps connected right now.
- **Folders**: add or remove folders, pause one, or update it now.
- **Search**: the same index your AI apps use, with highlighted matches. Double-click a result to open it.
- **AI apps**: detects Claude Code, Claude Desktop, Gemini CLI, Cursor, VS Code, VS Code Insiders and Windsurf, and connects or disconnects each with one click. Before changing another app's config file, it makes a one-time `.bak-folder-index` copy, and it refuses to edit files it cannot parse.
- **Settings**: start with Windows (minimized to the tray), OCR turbo capacity, the MCP server command for other clients, and the data folder.

While the app runs, it refreshes stale indexes every 15 minutes.

ChatGPT is listed but not supported. ChatGPT only connects to remote MCP servers over HTTPS, and exposing a private file index to the internet is deliberately not offered.

### Download

Get `FolderIndex-<version>-windows.zip` from the [Releases](https://github.com/git-raka/folder-index-mcp/releases) page, unzip it anywhere, and run `FolderIndex.exe`. The folder also contains `folder-index.exe`, which is the CLI and MCP server the AI apps start. The build is not code-signed, so Windows SmartScreen may warn on first launch: choose *More info → Run anyway*.

## Install with Python (any OS)

Requires Python 3.10+. With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install "folder-index-mcp[app] @ git+https://github.com/git-raka/folder-index-mcp"   # CLI + OCR + desktop app
uv tool install "folder-index-mcp[ocr] @ git+https://github.com/git-raka/folder-index-mcp"   # CLI + OCR
uv tool install git+https://github.com/git-raka/folder-index-mcp                             # CLI only
```

`pip install` works the same way. Then add folders and build their indexes:

```bash
folder-index add "C:\Users\me\Downloads"
folder-index add "C:\Users\me\Documents"
folder-index update          # first run reads everything; later runs only new/changed files
folder-index search "technical specification"
folder-index find "*invoice*" --ext pdf
folder-index status
folder-index dupes
folder-index-app             # the desktop app, if installed with [app]
```

Every command also accepts `--root <folder>` to work on just that folder without adding it. On macOS and Linux, use normal paths such as `~/Downloads`.

## Connect an AI app

The easiest way is the **AI apps** page of the desktop app, or the CLI:

```bash
folder-index connect                # list detected apps and their status
folder-index connect gemini         # claude-code | claude-desktop | gemini | cursor | vscode | vscode-insiders | windsurf
folder-index connect cursor --remove
```

Restart the app or start a new session afterwards. To do it by hand, any MCP client runs `folder-index serve` over stdio. For example, in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "folder-index": { "command": "folder-index", "args": ["serve"] }
  }
}
```

### Claude Code: one project only

To make the index available only when Claude Code runs in one folder on this machine, register it with local scope instead:

```bash
cd C:\Users\me\Downloads
claude mcp add folder-index --scope local -- folder-index --root C:\Users\me\Downloads serve
```

Optionally add a `CLAUDE.local.md` there (personal, never shared) so the agent reaches for the index first:

```markdown
To find files in this folder, use the folder-index MCP tools (search_files, find_files,
read_file_text) before ls/grep/Glob or opening documents.
```

## Tools exposed to the agent

| Tool | What it does |
|---|---|
| `search_files` | Full-text search over names, paths and contents across all folders, with filters for extension, folder, kind and date |
| `find_files` | Metadata listing: name glob, extension, date range, minimum size, sort by date/size/name |
| `read_file_text` | Returns the already-extracted text of a file in pages, so the agent never has to open the PDF |
| `recent_files` | Latest downloads |
| `find_duplicates` | Byte-identical documents (also across folders), largest waste first |
| `index_status` | Folders, counts, last update, update and OCR progress |
| `refresh_index` | Starts an incremental re-index of every folder in the background |
| `ocr_speed` | Switches OCR between `background`, `turbo` and `pause` |

With several folders, paths are shown as `<folder name>/<relative path>`. Pass `folder="Documents"` to search one folder only. When the server starts, it refreshes any index older than 15 minutes in a detached background process.

The desktop app shows which AI app is connected because each running server records the client name it receives during the MCP handshake.

## OCR

Included in the `[app]` and `[ocr]` extras and in the Windows download.

- **Windows 10/11**: uses the OCR engine built into Windows, so nothing else is needed. It recognizes the languages of your installed Windows language packs (*Settings → Time & language → Language*). English works well for any Latin-script text.
- **macOS / Linux**: install [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html), e.g. `brew install tesseract` or `apt install tesseract-ocr`. The `eng` and `ind` language data are used if present.

After every `update`, PDFs with no text layer and images of at least 30 KB outside source trees are queued. A detached process then works through the queue, newest first. Searching keeps working meanwhile, and results improve as OCR progresses. OCR'd text is stored after an `[OCR]` marker.

### Speed modes

OCR is CPU-bound: each page is rendered to an image and then recognised, and one process uses one core. **Turbo** runs one worker process per CPU core minus one, each with its own OCR engine, at normal priority. The worker count is capped by free RAM at about 350 MB per worker.

| Mode | Workers | Priority | Use it when |
|---|---|---|---|
| `background` | 1 | below normal | default after every update; you won't notice it |
| `turbo` | cores − 1, RAM-capped | normal | a large first OCR backlog, or you're away from the machine |
| `pause` | 0 | | you need the CPU; new updates won't restart OCR until you resume |

Switch modes from the desktop app, the agent's `ocr_speed` tool, the tray menu, or the shell:

```bash
folder-index ocr-speed turbo
folder-index ocr-speed turbo --workers 8   # explicit count
folder-index ocr-speed pause
folder-index ocr-speed background         # resume quietly
```

Switching modes hands the queue over without redoing anything. The running pass stops after the file(s) in progress, and every finished file is already saved. As a reference point, on a 24-core laptop with Windows OCR, `background` handles about 1 file/s and `turbo` (23 workers) about 25 files/s.

## Where data lives

Everything (the folder list, one index per folder, logs, and which AI apps are connected) is kept in `~/.folder-index`. Set `FOLDER_INDEX_HOME` to use another location.

It is deliberately not under `AppData`. AI apps installed as MSIX packages, such as Claude Desktop from the Microsoft Store, have their `AppData` writes redirected into a private per-package folder. The MCP server they start would then see different data from the desktop app. Version 0.3.0 kept data in `%LOCALAPPDATA%\folder-index`; newer versions copy it over on first start and remove the old copy afterwards.

A folder that is missing or offline, such as an unplugged drive or a disconnected share, is skipped and its index kept until it is back. It is never treated as empty.

A file whose extraction or OCR crashes its worker process (a damaged PDF, for example) is marked as unreadable on its own; the rest of the batch carries on.

## Customising what gets indexed

To skip more, put a `.folderindexignore` file in an indexed folder with one glob per line. Each glob is matched against the relative path or the file name:

```
# skip a big extracted SDK and all videos
go/*
*.mp4
Private/*
```

The default limits are in `src/folder_index/config.py`: 60k characters and 60 PDF pages per file, documents up to 80 MB, and the first 2 MB of text files.

## How it works

```
scan (os.scandir, no symlinks)  ->  plan per file: content | metadata | secret
        |                                   |
   unchanged? skip            ProcessPool: extract text + sha1
        |                                   |
        +-------------> SQLite per folder: files table + fts5(name, path, content)
                                   |                      ^
                                   |       background OCR pass (scans, images)
                                   v
        Library (all folders, merged ranking) -> MCP server / CLI / desktop app
```

Updates and OCR passes always run in detached `folder-index` processes guarded by per-folder lock files, so the MCP server and the desktop app never block. Each lock records its owner's process ID, so a pass that gets killed doesn't block the next one.

## Development

```bash
git clone https://github.com/git-raka/folder-index-mcp
cd folder-index-mcp
uv sync --extra app
uv run pytest
uv run folder-index-app
```

Build the Windows bundle (`packaging/dist/FolderIndex/`):

```bash
uv run pyinstaller packaging/folder-index.spec --noconfirm --distpath packaging/dist --workpath packaging/build
```

## License

[MIT](LICENSE) © Agung Raka Wibawa
