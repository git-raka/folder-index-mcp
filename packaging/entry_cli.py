"""PyInstaller entry point for folder-index.exe (CLI and MCP server)."""
import multiprocessing

from folder_index.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
