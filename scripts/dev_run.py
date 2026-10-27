"""Run the API and live feed processor against configured Azure resources."""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="reload the API process when source files change",
    )
    arguments = parser.parse_args()

    required_settings = (
        "EVENTHUB_NAMESPACE",
        "COSMOS_ENDPOINT",
        "FOUNDRY_PROJECT_ENDPOINT",
        "FOUNDRY_MODEL",
    )
    missing = [name for name in required_settings if not os.getenv(name)]
    if missing:
        parser.error(
            "set the Azure resource settings before starting: "
            + ", ".join(missing)
        )

    print("Starting the API and live Event Hubs processor against Azure resources.")
    uvicorn.run(
        "api.main:app",
        host=os.getenv("DEV_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=arguments.reload,
    )


if __name__ == "__main__":
    main()
