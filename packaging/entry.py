"""PyInstaller entry point (a script, not `-m talent`)."""
import multiprocessing

from talent.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
