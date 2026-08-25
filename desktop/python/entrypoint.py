"""Frozen llms-py entry point used only by the additive desktop build."""

from desktop_runtime import install_desktop_runtime
from llms.main import main


if __name__ == "__main__":
    install_desktop_runtime()
    main()
