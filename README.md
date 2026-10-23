# folder-index-mcp

A local, private full-text index of any folder (your Downloads, a document archive, a shared drive) exposed to Claude Code and other AI agents as an [MCP](https://modelcontextprotocol.io) server.

Without an index, an agent has to run `ls` and `grep` and open PDFs one by one, which is slow and expensive in tokens. With it, the agent asks the index and gets back a handful of ranked lines:

```
> search_files("invoice march hosting")
2 result(s) for 'invoice march hosting':
1. invoices/2026-03 Hosting Invoice.pdf  (98.4 KB, 2026-03-31)
   …[Invoice] No. 0042 … period: [March] 2026 … managed [hosting] plan…
2. ...
```

- **SQLite FTS5**: one file, no server, BM25 ranking, prefix matching, phrase/OR/NOT queries.
- **Reads contents**: PDF, Word, Excel, PowerPoint, OpenDocument, legacy `.doc/.xls/.ppt` (best effort), plain text, code, and the file listing inside `.zip` archives.
- **OCR for scans and images** (optional): scanned PDFs and screenshots/photos are OCR'd in a low-priority background process, newest first, using the OCR engine built into Windows or `tesseract` anywhere else.
- **Incremental**: only new or changed files are re-read, and deleted files are dropped. Text extraction runs in parallel.
- **Safe by default**: `.ssh`, `.git`, `node_modules`, virtualenvs and caches are never walked. Keys, certificates, VPN profiles, `.env` files and shell history are listed by name only, and their content is never read. The index lives outside the indexed folder (`%LOCALAPPDATA%\folder-index\` on Windows, `~/.local/share/folder-index/` elsewhere).
- **Code-aware**: a folder containing `package.json`, `go.mod`, `pom.xml`, a `LICENSE` and so on is treated as a source tree or unpacked distribution. Its files are indexed by name only, except office documents and the top-level README, so extracted repos and SDKs don't drown your documents.
- **Light**: the only dependencies are the MCP SDK and `pypdf`. Office files are parsed straight from their XML.

## Tools exposed to the agent

| Tool | What it does |
|---|---|
| `search_files` | Full-text search over names, paths and contents, with filters for extension, folder, kind and date |
| `find_files` | Metadata listing: name glob, extension, date range, minimum size, sort by date/size/name |
| `read_file_text` | Returns the already-extracted text of a file in pages, so the agent never has to open the PDF |
| `recent_files` | Latest downloads |
| `find_duplicates` | Byte-identical documents, largest waste first |
| `index_status` | Counts, last update, whether a refresh is running |
| `refresh_index` | Starts an incremental re-index in the background |
| `ocr_speed` | Switches OCR between `background` (1 low-priority worker), `turbo` (parallel, all cores but one) and `pause` |

When the server starts, it refreshes the index in a detached background process if the last update is more than 15 minutes old. That keeps the index current without a scheduler.

## Install

Requires Python 3.10+. The easiest way is [uv](https://docs.astral.sh/uv/), which installs the `folder-index` command globally:

```bash
uv tool install git+https://github.com/git-raka/folder-index-mcp
```

Or with pip:

```bash
pip install git+https://github.com/git-raka/folder-index-mcp
```

Build the index once. The first run reads every document; later runs only read new or changed files:

```bash
folder-index --root "C:\Users\me\Downloads" update
```

Try it from the command line:

```bash
folder-index --root "C:\Users\me\Downloads" search "technical specification"
folder-index --root "C:\Users\me\Downloads" find "*invoice*" --ext pdf
folder-index --root "C:\Users\me\Downloads" status
folder-index --root "C:\Users\me\Downloads" dupes
```

On macOS and Linux, use a normal path such as `--root ~/Downloads`.

### OCR (optional)

Install with the `ocr` extra to make scanned PDFs and images searchable:

```bash
uv tool install "folder-index-mcp[ocr] @ git+https://github.com/git-raka/folder-index-mcp"
```

- **Windows 10/11**: uses the OCR engine built into Windows, so nothing else is needed. It recognizes the languages of your installed Windows language packs (*Settings → Time & language → Language*).
- **macOS / Linux**: install [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html), e.g. `brew install tesseract` or `apt install tesseract-ocr`. The `eng` and `ind` language data are used if present.

After every `update`, PDFs with no text layer and images of at least 30 KB outside source trees are queued. A detached, below-normal-priority process then works through the queue, newest first. Searching keeps working meanwhile, and results improve as OCR progresses. OCR'd text is stored after an `[OCR]` marker. You can also run the queue in the foreground:

```bash
folder-index --root "C:\Users\me\Downloads" ocr --limit 50
folder-index --root "C:\Users\me\Downloads" status   # shows OCR progress
```

#### Speed modes

OCR is CPU-bound: each page is rendered to an image and then recognised, and one process uses one core. To finish a big backlog fast, switch to **turbo**. It runs one worker process per CPU core minus one, each with its own OCR engine, at normal priority. The worker count is capped by free RAM at about 350 MB per worker.

| Mode | Workers | Priority | Use it when |
|---|---|---|---|
| `background` | 1 | below normal | default after every update; you won't notice it |
| `turbo` | cores − 1, RAM-capped | normal | a large first OCR backlog, or you're away from the machine |
| `pause` | 0 | | you need the CPU; new updates won't restart OCR until you resume |

Switch from the agent with the `ocr_speed` tool, or from the shell:

```bash
folder-index --root "C:\Users\me\Downloads" ocr-speed turbo
folder-index --root "C:\Users\me\Downloads" ocr-speed turbo --workers 8   # explicit count
folder-index --root "C:\Users\me\Downloads" ocr-speed pause
folder-index --root "C:\Users\me\Downloads" ocr-speed background         # resume quietly
```

Switching modes hands the queue over without redoing anything. The running pass stops after the file(s) in progress, and every finished file is already saved. As a reference point, on a 24-core laptop with Windows OCR, `background` handles about 1 file/s and `turbo` (23 workers) about 25 files/s.

## Use with Claude Code

Register it with **local scope**. The entry is stored in your user config (`~/.claude.json`) for that one project directory on that one machine. It is not committed anywhere, and sessions in other folders or on other machines don't see it.

```bash
cd C:\Users\me\Downloads
claude mcp add folder-index --scope local -- folder-index --root C:\Users\me\Downloads serve
```

Optionally add a `CLAUDE.local.md` in the indexed folder so the agent reaches for the index first. Like the local MCP entry, this file is personal and never shared.

```markdown
To find files in this folder, use the folder-index MCP tools (search_files, find_files,
read_file_text) before ls/grep/Glob or opening documents.
```

## Use with other MCP clients

Any MCP client works the same way (Claude Desktop, Cursor, VS Code): run `folder-index --root <folder> serve` over stdio. For example, in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "folder-index": {
      "command": "folder-index",
      "args": ["--root", "C:\\Users\\me\\Downloads", "serve"]
    }
  }
}
```

## Customising what gets indexed

To skip more, put a `.folderindexignore` file in the indexed root with one glob per line. Each glob is matched against the relative path or the file name:

```
# skip a big extracted SDK and all videos
go/*
*.mp4
Private/*
```

The default limits are in `src/folder_index/config.py`: 60k characters and 60 PDF pages per file, documents up to 80 MB, and the first 2 MB of text files.

Without the `ocr` extra, scanned PDFs without a text layer are found by name only.

## How it works

```
scan (os.scandir, no symlinks)  ->  plan per file: content | metadata | secret
        |                                   |
   unchanged? skip            ProcessPool: extract text + sha1
        |                                   |
        +-------------> SQLite: files table + fts5(name, path, content)
                                   |                      ^
                                   |       background OCR pass (scans, images)
                                   v
                         MCP server (read-only connection) -> compact text results
```

## Development

```bash
git clone https://github.com/git-raka/folder-index-mcp
cd folder-index-mcp
uv sync
uv run pytest
```

## License

[MIT](LICENSE) © Agung Raka Wibawa
