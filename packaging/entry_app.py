"""PyInstaller entry point for FolderIndex.exe (desktop app)."""
import multiprocessing

from folder_index.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
